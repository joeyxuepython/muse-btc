from datetime import timedelta

import httpx
import pytest

from muse_btc.models import utc_now
from muse_btc.providers import Providers, okx_inst_id


def test_okx_inst_id_mapping():
    assert okx_inst_id("BTCUSDT") == "BTC-USDT-SWAP"
    assert okx_inst_id("ETHUSDT") == "ETH-USDT-SWAP"
    assert okx_inst_id("1000PEPEUSDT") == "1000PEPE-USDT-SWAP"
    assert okx_inst_id("BTCUSD") is None
    assert okx_inst_id("USDT") is None
    assert okx_inst_id("") is None
    assert okx_inst_id("BTC-USD-SWAP") is None


def test_futures_source_rejects_unknown_value(settings):
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        settings.__class__(database_path=settings.database_path, futures_source="cme")


def okx_fixture(request: httpx.Request) -> httpx.Response:
    from test_providers_api import public_api_fixture

    path = request.url.path
    if path.endswith("/public/funding-rate"):
        inst = request.url.params.get("instId", "")
        return httpx.Response(
            200, json={"code": "0", "msg": "", "data": [{"instId": inst, "fundingRate": "0.0002"}]}
        )
    if path.endswith("/public/open-interest"):
        inst = request.url.params.get("instId", "")
        return httpx.Response(
            200, json={"code": "0", "msg": "", "data": [{"instId": inst, "oiUsd": "105"}]}
        )
    if path.endswith("/market/ticker"):
        inst = request.url.params.get("instId", "")
        return httpx.Response(
            200, json={"code": "0", "msg": "", "data": [{"instId": inst, "last": "122.0"}]}
        )
    return public_api_fixture(request)


def seed_oi(store, minutes_ago=5, oi=100.0, points=1):
    now = utc_now()
    for base in ("BTC", "ETH", "SOL"):
        for i in range(points):
            at = now - timedelta(minutes=minutes_ago + i * 2)
            store.save_oi(f"okx:{base}-USDT-SWAP", at, oi + i)


@pytest.mark.asyncio
async def test_okx_derivatives_populate_funding_basis_and_oi(settings, store):
    settings.futures_source = "okx"
    seed_oi(store)
    # 旧代码残留的 Binance 期货状态行应被清理
    from muse_btc.models import ProviderState, ProviderStatus

    store.save_status(
        ProviderStatus(
            name="Binance Futures", state=ProviderState.UNAVAILABLE, message="old", coverage="old"
        )
    )
    providers = Providers(settings, store, transport=httpx.MockTransport(okx_fixture))
    try:
        snapshots = await providers.binance()
        assert len(snapshots) == 3
        btc = snapshots[0]
        assert btc.features.funding_rate_pct == pytest.approx(0.02)
        assert btc.features.oi_change_5m_pct == pytest.approx(5)
        # 跨所基差：OKX 永续 122.0 相对 Binance 现货 121.36
        assert btc.features.basis_pct == pytest.approx((122.0 / 121.36 - 1) * 100)
        assert "BASIS_CROSS_EXCHANGE" in btc.quality_issues
        assert "DERIVATIVES_UNAVAILABLE" not in btc.quality_issues
        assert providers.futures_source_used == {"OKX"}
        statuses = {s.name: s.state for s in store.statuses()}
        assert statuses["OKX Futures"] == "READY"
        assert "Binance Futures" not in statuses
        for snap in snapshots:
            store.save_snapshot(snap)
    finally:
        await providers.close()


@pytest.mark.asyncio
async def test_okx_oi_zscore_needs_history(settings, store):
    settings.futures_source = "okx"
    seed_oi(store, minutes_ago=2, oi=100.0, points=20)
    providers = Providers(settings, store, transport=httpx.MockTransport(okx_fixture))
    try:
        snapshots = await providers.binance()
        assert snapshots[0].features.oi_zscore is not None
    finally:
        await providers.close()


@pytest.mark.asyncio
async def test_okx_unavailable_keeps_spot_monitoring(settings, store):
    settings.futures_source = "okx"

    def handler(request):
        if request.url.host == "www.okx.com":
            return httpx.Response(404)
        return okx_fixture(request)

    providers = Providers(settings, store, transport=httpx.MockTransport(handler))
    try:
        snapshots = await providers.binance()
        assert len(snapshots) == 3
        assert all(s.features.funding_rate_pct is None for s in snapshots)
        assert all("DERIVATIVES_UNAVAILABLE" in s.quality_issues for s in snapshots)
        statuses = {s.name: s.state for s in store.statuses()}
        assert statuses["Binance Spot"] == "READY"
        assert statuses["OKX Futures"] == "UNAVAILABLE"
    finally:
        await providers.close()


@pytest.mark.asyncio
async def test_auto_falls_back_to_okx_when_binance_blocked(settings, store):
    settings.futures_source = "auto"
    seed_oi(store)

    def handler(request):
        if request.url.host == "fapi.binance.com":
            return httpx.Response(451)
        return okx_fixture(request)

    providers = Providers(settings, store, transport=httpx.MockTransport(handler))
    try:
        snapshots = await providers.binance()
        assert len(snapshots) == 3
        btc = snapshots[0]
        assert btc.features.funding_rate_pct == pytest.approx(0.02)
        assert btc.features.oi_change_5m_pct == pytest.approx(5)
        assert "DERIVATIVES_UNAVAILABLE" not in btc.quality_issues
        assert providers.futures_source_used == {"OKX"}
        statuses = {s.name: s.state for s in store.statuses()}
        assert statuses["OKX Futures"] == "READY"
    finally:
        await providers.close()


@pytest.mark.asyncio
async def test_auto_prefers_binance_when_available(settings, store):
    settings.futures_source = "auto"
    providers = Providers(settings, store, transport=httpx.MockTransport(okx_fixture))
    try:
        snapshots = await providers.binance()
        # Binance fixture 提供 premiumIndex/openInterestHist，走 Binance 路径
        assert providers.futures_source_used == {"Binance"}
        assert snapshots[0].features.funding_rate_pct == pytest.approx(0.01)
        statuses = {s.name: s.state for s in store.statuses()}
        assert statuses["Binance Futures"] == "READY"
    finally:
        await providers.close()


def test_oi_history_roundtrip_and_pruning(settings, store):
    now = utc_now()
    store.save_oi("okx:BTC-USDT-SWAP", now - timedelta(hours=7), 90.0)
    store.save_oi("okx:BTC-USDT-SWAP", now - timedelta(minutes=10), 100.0)
    store.save_oi("okx:BTC-USDT-SWAP", now, 101.0)
    history = store.oi_history("okx:BTC-USDT-SWAP", now - timedelta(hours=8))
    # 7 小时前的旧点已被裁剪（保留 6 小时窗口）
    assert [oi for _, oi in history] == [100.0, 101.0]
    assert store.oi_history("okx:ETH-USDT-SWAP", now - timedelta(hours=1)) == []
