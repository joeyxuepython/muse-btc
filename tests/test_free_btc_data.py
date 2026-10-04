"""Offline source fixtures and regression checks; no scheduled research or live collection."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from muse_btc.api import create_app
from muse_btc.btc_data import BTCDataEngine
from muse_btc.intelligence import IntelligenceStore
from muse_btc.liquidations import collect_liquidations, liquidation_event
from muse_btc.providers import Providers
from muse_btc.providers.common import ProviderError
from muse_btc.providers.free_btc import BG_METRICS, FreeBTCProvider, daily_points


@pytest.fixture
def clocked(monkeypatch, now):
    clock = [now]
    for module in (
        "btc_data",
        "liquidations",
        "providers.free_btc",
        "providers.public_intelligence",
    ):
        monkeypatch.setattr("muse_btc." + module + ".utc_now", lambda: clock[0])
    return clock


def source_handler(now, calls, *, missing_supply=False, ticker_error=False):
    chain_day = (now - timedelta(days=9)).replace(hour=0)
    names = ["BTC-31JAN25-100000-C", "BTC-31JAN25-100000-P"]

    def handler(request):
        calls.append(str(request.url))
        assert "authorization" not in request.headers
        if request.url.host == "community-api.coinmetrics.io":
            row = {
                "asset": "btc",
                "time": chain_day.isoformat(),
                "CapMVRVCur": "2.5",
                "CapMrktCurUSD": "2000000000",
                "AdrActCnt": "700000",
                "TxCnt": "500000",
                "FlowInExNtv": "25000",
                "FlowOutExNtv": "30000",
                "SplyExNtv": "2500000",
            }
            if not missing_supply:
                row["SplyCur"] = "19000000"
            return httpx.Response(200, json={"data": [row]})
        if request.url.host == "bitcoin-data.com":
            metric = request.url.path.rsplit("/", 1)[-1]
            field = BG_METRICS[metric][0][0]
            assert request.url.params["endday"] == (now - timedelta(days=7)).date().isoformat()
            return httpx.Response(
                200,
                json=[
                    {
                        "d": chain_day.date().isoformat(),
                        "unixTs": int(chain_day.timestamp()),
                        field: "1.05" if "sopr" in metric else "50000",
                    }
                ],
            )
        if request.url.host == "www.deribit.com":
            if request.url.path.endswith("get_instruments"):
                result = [
                    {
                        "instrument_name": name,
                        "kind": "option",
                        "base_currency": "BTC",
                        "quote_currency": "BTC",
                        "option_type": "call" if name.endswith("C") else "put",
                        "strike": 100000,
                        "expiration_timestamp": int((now + timedelta(days=30)).timestamp() * 1000),
                    }
                    for name in names
                ]
            elif request.url.path.endswith("get_book_summary_by_currency"):
                result = [
                    {
                        "instrument_name": name,
                        "mark_iv": 50,
                        "open_interest": 4,
                        "volume": 1,
                        "underlying_price": 100000,
                        "mark_price": 0.03,
                        "bid_price": None,
                        "ask_price": 0.035,
                    }
                    for name in names
                ]
            else:
                if ticker_error:
                    return httpx.Response(
                        200, json={"error": {"code": 10028, "message": "too_many_requests"}}
                    )
                result = {
                    "instrument_name": request.url.params["instrument_name"],
                    "timestamp": int(now.timestamp() * 1000),
                    "greeks": {"delta": 0.5, "gamma": 0.01, "vega": 1, "theta": -1, "rho": 2},
                }
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": result})
        raise AssertionError("Unexpected source: " + str(request.url))

    return handler


async def test_onchain_archive_delay_cache_and_no_research(store, settings, now, clocked):
    calls = []
    providers = Providers(settings, store, httpx.MockTransport(source_handler(now, calls)))
    engine = BTCDataEngine(store, settings, providers.public)
    try:
        first = await engine.collect("onchain")
        assert first["status"] == "COMPLETE"
        assert len(first["sources"]) == 14
        assert len(calls) == 7
        assert not first["research_checks_performed"] and not first["scheduled"]
        summary = engine.summary(now)
        assert summary["onchain"][0]["status"] == "STALE"  # Old fixture observation, fresh fetch.
        bg = next(r for r in summary["onchain"] if r["metric"] == "sopr")
        assert bg["status"] == "DELAYED" and bg["free_recency_limit_days"] == 7
        record = engine.series(now, "Coin Metrics", "mvrv")[0]
        assert record.available_at == now and record.market_time < now
        assert store.raw(record.raw_ids[0])["source"] == "Coin Metrics"
        assert not engine.series(now - timedelta(seconds=1), "Coin Metrics", "mvrv")
        second = await engine.collect("onchain")
        assert all(r["status"] == "CACHED" for r in second["sources"])
        assert len(calls) == 7
        assert len(IntelligenceStore(store).records("onchain", now, latest=False)) == 14
        assert IntelligenceStore(store).checks() == []
        with store.connect() as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 4
    finally:
        await providers.close()


async def test_missing_coinmetrics_metric_is_individual_failure(store, settings, now, clocked):
    providers = Providers(
        settings, store, httpx.MockTransport(source_handler(now, [], missing_supply=True))
    )
    try:
        result = await BTCDataEngine(store, settings, providers.public).collect("onchain")
        assert result["status"] == "DEGRADED"
        missing = next(r for r in result["sources"] if r["metric"] == "supply")
        assert missing["status"] == "UNAVAILABLE" and missing["reason"]
        assert len(IntelligenceStore(store).records("onchain", now)) == 13
    finally:
        await providers.close()


def test_daily_points_reject_future_nan_identity_and_conflicting_dates(now):
    rows = [
        {"asset": "btc", "time": "2025-01-01T00:00:00Z", "value": "2"},
        {"asset": "btc", "time": "2025-01-02T00:00:00Z", "value": "9"},
        {"asset": "btc", "time": "2024-12-31T00:00:00Z", "value": "NaN"},
    ]
    points, rejected = daily_points(rows, ("value",), now, coinmetrics=True)
    assert rejected == 2 and len(points) == 1
    assert points[0][0].tzinfo == UTC
    with pytest.raises(ProviderError):
        daily_points([rows[0], rows[0] | {"value": 3}], ("value",), now, coinmetrics=True)
    with pytest.raises(ProviderError):
        daily_points([rows[0] | {"asset": "eth"}], ("value",), now, coinmetrics=True)
    with pytest.raises(ProviderError):
        daily_points([{"d": "2025-01-01", "unixTs": 0, "value": 1}], ("value",), now)


def test_bgeometrics_budget_persists_across_provider_instances(store, settings, now):
    providers = Providers(settings, store)
    first, second = FreeBTCProvider(providers.public), FreeBTCProvider(providers.public)
    for _ in range(8):
        first.reserve_bgeometrics_request(now)
    with pytest.raises(ProviderError, match="额度"):
        second.reserve_bgeometrics_request(now)
    for _ in range(7):
        second.reserve_bgeometrics_request(now + timedelta(hours=2))
    with pytest.raises(ProviderError, match="额度"):
        first.reserve_bgeometrics_request(now + timedelta(hours=4))
    first.reserve_bgeometrics_request(now + timedelta(days=1, seconds=1))


def test_concurrent_bgeometrics_reservations_cannot_exceed_hour_limit(store, settings, now):
    provider = FreeBTCProvider(type("Public", (), {"settings": settings, "store": store})())

    def reserve(_):
        try:
            provider.reserve_bgeometrics_request(now)
            return True
        except ProviderError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(16))) == 8
    assert len(store.state("bgeometrics_budget")) == 8


async def test_failed_tickers_are_not_visible_before_failure_was_observed(
    store, settings, now, clocked
):
    base_handler = source_handler(now, [], ticker_error=True)

    def handler(request):
        if request.url.path.endswith("ticker"):
            clocked[0] += timedelta(seconds=1)
        return base_handler(request)

    providers = Providers(settings, store, httpx.MockTransport(handler))
    try:
        await BTCDataEngine(store, settings, providers.public).collect("options")
        assert IntelligenceStore(store).records("options", now) == []
        record = IntelligenceStore(store).records("options", clocked[0])[0]
        assert record.available_at == now + timedelta(seconds=2)
    finally:
        await providers.close()


async def test_option_units_missing_bid_and_bounded_greeks(store, settings, now, clocked):
    calls = []
    settings.deribit_greeks_limit = 1
    providers = Providers(settings, store, httpx.MockTransport(source_handler(now, calls)))
    engine = BTCDataEngine(store, settings, providers.public)
    try:
        result = await engine.collect("options")
        assert result["status"] == "COMPLETE" and len(calls) == 3
        option = engine.summary(now)["options"]["data"]
        assert option["greeks_observed"] == 1 and not option["full_greeks_coverage"]
        assert option["summary_time_basis"] == "RECEIVED_AT_NOT_EXCHANGE_QUOTE_TIME"
        assert option["chain"][0]["price_unit"] == "BTC"
        assert option["chain"][0]["open_interest_btc"] == 4
        assert option["chain"][0]["bid_price"] is None
        assert sum(r["greeks"] is None for r in option["chain"]) == 1
        stale = engine.summary(now + timedelta(minutes=20))["options"]
        assert stale["status"] == "STALE"
        await engine.collect("options")
        assert len(calls) == 3
    finally:
        await providers.close()


async def test_rpc_error_keeps_option_chain_and_reports_missing_greeks(
    store, settings, now, clocked
):
    providers = Providers(
        settings, store, httpx.MockTransport(source_handler(now, [], ticker_error=True))
    )
    engine = BTCDataEngine(store, settings, providers.public)
    try:
        assert (await engine.collect("options"))["status"] == "DEGRADED"
        option = engine.summary(now)["options"]["data"]
        assert len(option["greeks_errors"]) == 2
        assert all(r["greeks"] is None for r in option["chain"])
    finally:
        await providers.close()


def force_order(now):
    ms = int(now.timestamp() * 1000)
    return {
        "e": "forceOrder",
        "E": ms,
        "st": 1,
        "o": {
            "s": "BTCUSDT",
            "S": "SELL",
            "T": ms,
            "q": "9",
            "z": "0.2",
            "ap": "100000",
            "X": "PARTIALLY_FILLED",
        },
    }


def test_liquidation_uses_filled_qty_and_filters_cm_future_and_bad_direction(now):
    payload = force_order(now)
    _, data = liquidation_event({"data": payload}, now)
    assert data["snapshot_filled_notional_quote"] == 20000
    assert data["liquidated_position"] == "LONG" and not data["full_market"]
    assert liquidation_event(payload | {"st": 2}, now) is None
    with pytest.raises(ProviderError):
        liquidation_event(payload | {"o": payload["o"] | {"S": "UNKNOWN"}}, now)
    with pytest.raises(ProviderError):
        liquidation_event(payload, now - timedelta(seconds=1))


class Socket:
    def __init__(self, messages, fail=False):
        self.messages, self.fail = iter(messages), fail

    async def __aenter__(self):
        if self.fail:
            raise OSError("proxy http://DO_NOT_LEAK:password@proxy.invalid")
        return self

    async def __aexit__(self, *args):
        return False

    async def recv(self):
        try:
            return json.dumps(next(self.messages))
        except StopIteration:
            raise TimeoutError from None


async def test_liquidation_dedup_and_window_and_no_false_global_total(
    store, settings, now, clocked
):
    event = force_order(now)
    result = await collect_liquidations(store, settings, 1, lambda *a, **k: Socket([event, event]))
    assert result["status"] == "SAMPLED" and result["new_events"] == 1
    assert result["full_market_total_usd"] is None
    archive = IntelligenceStore(store)
    assert len(archive.records("liquidation", now)) == 1
    assert len(archive.records("liquidation_window", now)) == 1
    quiet = await collect_liquidations(store, settings, 1, lambda *a, **k: Socket([]))
    assert quiet["status"] == "OBSERVED_NO_EVENTS" and quiet["full_market_total_usd"] is None
    failed = await collect_liquidations(store, settings, 1, lambda *a, **k: Socket([], True))
    assert failed["status"] == "UNAVAILABLE" and failed["connected_at"] is None
    assert "password" not in json.dumps(failed)


async def test_cancelled_liquidation_window_is_degraded(store, settings, now, clocked):
    class Cancelled(Socket):
        async def recv(self):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await collect_liquidations(store, settings, 1, lambda *a, **k: Cancelled([]))
    record = IntelligenceStore(store).records("liquidation_window", now)[0]
    assert record.data["status"] == "DEGRADED" and record.data["error"] == "CancelledError"


def test_btc_api_empty_start_auth_validation_and_onchain_collection(settings, now, clocked):
    calls = []
    settings.api_token = "FREE_DATA_TEST_TOKEN"

    def factory(cfg, store):
        return Providers(cfg, store, httpx.MockTransport(source_handler(now, calls)))

    with TestClient(create_app(settings, factory)) as client:
        assert client.get("/api/btc").status_code == 401
        headers = {"Authorization": "Bearer FREE_DATA_TEST_TOKEN"}
        empty = client.get("/api/btc", headers=headers).json()
        assert len(empty["onchain"]) == 14 and all(r["value"] is None for r in empty["onchain"])
        assert empty["options"] is None and not calls
        assert (
            client.post(
                "/api/btc/collect", headers=headers, json={"liquidation_seconds": 61}
            ).status_code
            == 422
        )
        result = client.post("/api/btc/collect", headers=headers, json={"scope": "onchain"})
        assert result.status_code == 200 and result.json()["status"] == "COMPLETE"
        rows = client.get("/api/btc/onchain/mvrv?source=Coin%20Metrics", headers=headers).json()
        assert len(rows) == 1 and rows[0]["data"]["value"] == 2.5
        overview = client.get("/api/intelligence", headers=headers).json()
        assert not overview["scheduled_research"] and overview["research_checks"] == []
