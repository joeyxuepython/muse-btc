from datetime import timedelta

import httpx
import pytest
from test_providers_api import public_api_fixture

from muse_btc.models import utc_now
from muse_btc.providers import Providers, okx_inst_id


def test_okx_inst_id_is_only_syntactic_mapping():
    assert okx_inst_id("BTCUSDT") == "BTC-USDT-SWAP"
    assert okx_inst_id("ETHUSDT") == "ETH-USDT-SWAP"
    assert okx_inst_id("BTCUSD") is None


@pytest.mark.asyncio
async def test_real_history_features_do_not_depend_on_local_warmup(settings, store):
    provider = Providers(settings, store, transport=httpx.MockTransport(public_api_fixture))
    try:
        snapshots = await provider.binance()
        assert len(snapshots) == 3
        btc = snapshots[0]
        assert btc.features.oi_change_5m_pct == pytest.approx(1)
        assert btc.features.funding_rate_pct == pytest.approx(0.01)
        assert btc.features.funding_mean_pct is not None
        assert btc.features.oi_zscore is not None
        assert btc.features.perp_taker_buy_ratio == pytest.approx(0.65)
        assert btc.features.long_short_account_ratio == pytest.approx(1.5)
        assert btc.features.elite_position_ratio == pytest.approx(1.5)
        assert btc.features.perp_spread_bps is not None
        assert btc.features.basis_pct == pytest.approx(0)
        assert btc.features.cross_venue_premium_pct == pytest.approx(0)
        assert btc.component_times["funding"] <= btc.available_at
        for snapshot in snapshots:
            store.save_snapshot(snapshot)
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_source", ["binance", "auto", "okx"])
async def test_v4_never_calls_binance_futures_for_legacy_config(settings, store, legacy_source):
    settings.futures_source = legacy_source
    requests = []

    def handler(request):
        requests.append(request)
        return public_api_fixture(request)

    provider = Providers(settings, store, transport=httpx.MockTransport(handler))
    try:
        assert len(await provider.binance()) == 3
        assert provider.futures_source_used == {"OKX"}
        assert not any(r.url.host == "fapi.binance.com" for r in requests)
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_okx_failure_keeps_spot_and_marks_metrics_missing(settings, store):
    def handler(request):
        if request.url.host == "www.okx.com":
            return httpx.Response(404)
        return public_api_fixture(request)

    provider = Providers(settings, store, transport=httpx.MockTransport(handler))
    try:
        snapshots = await provider.binance()
        assert len(snapshots) == 3
        assert all(s.features.funding_rate_pct is None for s in snapshots)
        assert all("DERIVATIVES_UNAVAILABLE" in s.quality_issues for s in snapshots)
        assert {s.name: s.state for s in store.statuses()}["OKX Futures"] == "UNAVAILABLE"
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_future_or_stale_statistics_never_become_current_features(settings, store):
    def handler(request):
        if request.url.path.endswith("/open-interest-history"):
            stamp = int((utc_now() + timedelta(minutes=5)).timestamp() * 1000)
            return httpx.Response(
                200, json={"code": "0", "data": [[str(stamp), "100", "100", "10000"]]}
            )
        return public_api_fixture(request)

    provider = Providers(settings, store, transport=httpx.MockTransport(handler))
    try:
        snapshots = await provider.binance()
        assert snapshots[0].features.oi_change_5m_pct is None
        assert "OI_HISTORY_STALE_OR_FUTURE" in snapshots[0].quality_issues
    finally:
        await provider.close()


def test_oi_history_roundtrip_and_pruning(settings, store):
    now = utc_now()
    store.save_oi("okx:BTC-USDT-SWAP", now - timedelta(hours=7), 90)
    store.save_oi("okx:BTC-USDT-SWAP", now, 100)
    assert len(store.oi_history("okx:BTC-USDT-SWAP", now - timedelta(hours=8))) == 1
