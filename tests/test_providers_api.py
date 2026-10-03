from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from muse_btc.api import create_app
from muse_btc.models import utc_now
from muse_btc.providers import Providers


def public_api_fixture(request: httpx.Request) -> httpx.Response:
    """Contract-shaped fixtures for tests only; never loaded by runtime providers."""
    now = utc_now()
    stamp = int(now.timestamp() * 1000)
    path = request.url.path
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "USDCUSDT"]
    address = "0x1234567890123456789012345678901234567890"
    if path.endswith("/ticker/24hr"):
        data = [
            {
                "symbol": s,
                "lastPrice": "121.36",
                "quoteVolume": str(100000000 - i * 1000),
                "closeTime": stamp,
            }
            for i, s in enumerate(symbols)
        ]
    elif path.endswith("/exchangeInfo"):
        data = {
            "symbols": [
                {
                    "symbol": s,
                    "baseAsset": s[:-4],
                    "quoteAsset": "USDT",
                    "status": "TRADING",
                    "isSpotTradingAllowed": True,
                }
                for s in symbols
            ]
        }
    elif path.endswith("/klines"):
        count = int(request.url.params.get("limit", "180"))
        minute = now.replace(second=0, microsecond=0)
        data = []
        for i in range(count):
            begin = minute - timedelta(minutes=count - i - 1)
            start = int(begin.timestamp() * 1000)
            close = 100 + i * 0.12
            volume = 5000 if i == count - 2 else 1000
            data.append(
                [
                    start,
                    str(close - 0.05),
                    str(close + 0.1),
                    str(close - 0.1),
                    str(close),
                    "10",
                    start + 59999,
                    str(volume),
                    30,
                    "6.5",
                    str(volume * 0.65),
                    "0",
                ]
            )
    elif path.endswith("/depth"):
        data = {"bids": [["121.35", "10"]], "asks": [["121.37", "10"]]}
    elif path.endswith("/premiumIndex"):
        data = {
            "lastFundingRate": "0.0001",
            "markPrice": "121.36",
            "indexPrice": "121.35",
            "time": stamp,
        }
    elif path.endswith("/openInterestHist"):
        data = [
            {"timestamp": stamp - 300000, "sumOpenInterest": "100"},
            {"timestamp": stamp, "sumOpenInterest": "101"},
        ]
    elif path.endswith("/public/funding-rate"):
        inst = request.url.params.get("instId", "")
        data = {"code": "0", "msg": "", "data": [{"instId": inst, "fundingRate": "0.0001"}]}
    elif path.endswith("/public/open-interest"):
        inst = request.url.params.get("instId", "")
        data = {"code": "0", "msg": "", "data": [{"instId": inst, "oiUsd": "101"}]}
    elif path.endswith("/market/ticker"):
        inst = request.url.params.get("instId", "")
        data = {"code": "0", "msg": "", "data": [{"instId": inst, "last": "121.36"}]}
    elif path == "/token-profiles/latest/v1":
        data = [{"chainId": "ethereum", "tokenAddress": address}]
    elif "/token-pairs/v1/" in path:
        data = [
            {
                "chainId": "ethereum",
                "pairAddress": "0xpool",
                "baseToken": {"address": address, "symbol": "TEST_MEME"},
                "priceUsd": "0.01",
                "liquidity": {"usd": 100000},
                "volume": {"m5": 10000, "h1": 30000, "h24": 100000},
                "priceChange": {"m5": 5, "h1": 10},
                "txns": {"m5": {"buys": 50, "sells": 10}},
                "pairCreatedAt": stamp - 3600000,
            }
        ]
    elif "/token_security/" in path:
        data = {"code": 1, "result": {address: {"is_honeypot": "1"}}}
    else:
        return httpx.Response(404)
    return httpx.Response(200, json=data)


@pytest.mark.asyncio
async def test_public_adapters_normalize_all_three_modules_and_keep_lineage(settings, store):
    providers = Providers(settings, store, transport=httpx.MockTransport(public_api_fixture))
    try:
        # OKX 无公开 OI 历史接口，持仓量变化依赖本地累积：预置 5 分钟前的读数。
        seed_at = utc_now() - timedelta(minutes=5)
        for base in ("BTC", "ETH", "SOL"):
            store.save_oi(f"okx:{base}-USDT-SWAP", seed_at, 100.0)
        cex = await providers.binance()
        memes = await providers.memes()
        assert len(cex) == 3
        assert {s.module for s in cex + memes} == {"BTC", "ALT"}
        assert not any(s.symbol == "USDCUSDT" for s in cex)
        assert cex[0].features.funding_rate_pct == pytest.approx(0.01)
        assert cex[0].features.oi_change_5m_pct == pytest.approx(1)
        assert cex[0].features.basis_pct == pytest.approx(0)
        assert "BASIS_CROSS_EXCHANGE" in cex[0].quality_issues
        assert "OKX" in providers.futures_source_used
        statuses = {s.name: s.state for s in store.statuses()}
        assert statuses["OKX Futures"] == "READY"
        assert memes == []
        for snap in cex + memes:
            assert snap.raw_ids
            assert all(store.raw(raw_id) for raw_id in snap.raw_ids)
            assert all(c.close_time <= snap.available_at for c in snap.candles)
            store.save_snapshot(snap)
    finally:
        await providers.close()


@pytest.mark.asyncio
async def test_missing_futures_does_not_destroy_spot_monitoring(settings, store):
    settings.futures_source = "binance"

    def handler(request):
        if request.url.host == "fapi.binance.com":
            return httpx.Response(451)
        return public_api_fixture(request)

    providers = Providers(settings, store, transport=httpx.MockTransport(handler))
    try:
        snapshots = await providers.binance()
        assert len(snapshots) == 3
        assert all(s.features.funding_rate_pct is None for s in snapshots)
        assert all("DERIVATIVES_UNAVAILABLE" in s.quality_issues for s in snapshots)
        statuses = {s.name: s.state for s in store.statuses()}
        assert statuses["Binance Spot"] == "READY"
        assert statuses["Binance Futures"] == "UNAVAILABLE"
    finally:
        await providers.close()


@pytest.mark.asyncio
async def test_network_failure_produces_status_not_fake_data(settings, store):
    providers = Providers(
        settings, store, transport=httpx.MockTransport(lambda r: httpx.Response(403))
    )
    try:
        assert await providers.binance() == []
        assert await providers.memes() == []
        assert store.counts()["snapshots"] == 0
        assert all(s.state == "UNAVAILABLE" for s in store.statuses())
    finally:
        await providers.close()


@pytest.mark.asyncio
async def test_rate_limit_backoff_stops_repeated_requests(settings, store):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "120"})

    providers = Providers(settings, store, transport=httpx.MockTransport(handler))
    try:
        from muse_btc.providers import ProviderError

        with pytest.raises(ProviderError, match="限流"):
            await providers.get("TEST", "https://example.com", "/")
        with pytest.raises(ProviderError, match="退避"):
            await providers.get("TEST", "https://example.com", "/")
        assert calls == 1
    finally:
        await providers.close()


def test_api_collection_dashboard_details_export_and_validation(settings):
    def factory(config, store):
        return Providers(config, store, transport=httpx.MockTransport(public_api_fixture))

    app = create_app(settings, providers_factory=factory)
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/ready").status_code == 503
        assert "MUSE" in client.get("/").text
        assert client.get("/static/app.js").status_code == 200
        result = client.post("/api/collect").json()
        assert result["status"] == "COMPLETE"
        assert result["snapshots"] == 3
        overview = client.get("/api/overview").json()
        assert {a["module"] for a in overview["assets"]} == {"BTC", "ALT"}
        assert overview["coverage"]["quotes"] == 3
        assert overview["signals"]
        signal = overview["signals"][0]
        detail = client.get(f"/api/signals/{signal['id']}").json()
        assert detail["signal"]["events"]
        assert detail["snapshot"]["raw_ids"]
        raw_id = detail["snapshot"]["raw_ids"][0]
        assert client.get(f"/api/raw/{raw_id}").json()["payload_hash"]
        assert client.get("/api/validation").json()["outcome_count"] == 0
        assert client.get("/api/export").json()["signals"]
        assert client.get("/ready").status_code == 200
        assert client.get("/api/assets/missing").status_code == 404
        assert client.get("/api/raw/missing").status_code == 404
        assert (
            client.post("/api/collect", headers={"Origin": "https://foreign.example"}).status_code
            == 403
        )


def test_api_token_is_enforced_and_never_returned(settings):
    from pydantic import SecretStr

    settings.api_token = SecretStr("local-test-token")
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/overview").status_code == 401
        assert client.post("/api/collect").status_code == 401
        response = client.get("/api/overview", headers={"Authorization": "Bearer local-test-token"})
        assert response.status_code == 200
        assert "local-test-token" not in response.text
