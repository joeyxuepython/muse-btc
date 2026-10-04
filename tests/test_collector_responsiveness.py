"""Offline stalls reproduce loop starvation without accessing the Muse cloud database."""

import asyncio
import threading
from datetime import timedelta

import httpx
import pytest
from conftest import snapshot

from muse_btc.api import create_app
from muse_btc.async_io import run_sync
from muse_btc.intelligence import IntelligenceStore
from muse_btc.microstructure import archive_snapshot_trades, cvd_summary
from muse_btc.models import utc_now
from muse_btc.providers import Providers


@pytest.mark.asyncio
@pytest.mark.parametrize("public_text", [False, True])
async def test_health_responds_while_raw_archive_is_blocked(settings, monkeypatch, public_text):
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    instances = []

    def factory(config, store):
        provider = Providers(
            config,
            store,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"rows": ["test"] * 5000})
            ),
        )
        instances.append(provider)
        return provider

    app = create_app(settings, providers_factory=factory)
    original = app.state.store.save_raw

    def slow_archive(*args):
        assert threading.get_ident() != loop_thread
        entered.set()
        if not release.wait(3):
            raise AssertionError("Health request could not release the simulated database stall")
        return original(*args)

    monkeypatch.setattr(app.state.store, "save_raw", slow_archive)
    async with app.router.lifespan_context(app):
        provider = instances[0]
        operation = (
            provider.public.fetch("TEST", "https://example.org/data", {"example.org"})
            if public_text
            else provider.get("TEST", "https://example.org", "/data")
        )
        task = asyncio.create_task(operation)
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await asyncio.wait_for(client.get("/health"), 1)
            assert response.status_code == 200
            assert response.json()["scope"] == "PROCESS_LIVENESS"
            assert response.json()["database"] == "NOT_CHECKED"
            assert not task.done()
        finally:
            release.set()
            _, raw, _ = await task
        assert app.state.store.raw(raw)


@pytest.mark.asyncio
async def test_cancellation_joins_postprocessing_before_releasing_collector_lease(
    store, settings, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()

    async def empty():
        return []

    app = create_app(settings)
    collector = app.state.collector
    monkeypatch.setattr(collector.providers, "binance", empty)

    def slow_batch(*args):
        assert threading.get_ident() != loop_thread
        entered.set()
        if not release.wait(3):
            raise AssertionError("Postprocessing was not released")
        store.set_state("thread_finished", True)

    monkeypatch.setattr(collector, "_process_batch", slow_batch)
    task = asyncio.create_task(collector.collect_once())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert collector.progress()["phase"] == "PERSISTING"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await asyncio.wait_for(client.get("/health"), 1)
        assert response.json()["collector"]["phase"] == "PERSISTING"
        assert not task.done()
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        assert (
            await run_sync(IntelligenceStore(store).acquire, "market-collector", utc_now(), 120)
            is None
        )
        assert (await collector.collect_once())["status"] == "BUSY"
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await collector.providers.close()
    assert store.state("thread_finished") is True
    assert store.state("market_last_result")["status"] == "INTERRUPTED"
    assert collector.progress()["phase"] == "IDLE"
    assert IntelligenceStore(store).acquire("market-collector", utc_now(), 120)


@pytest.mark.asyncio
async def test_joined_sync_exception_propagates():
    def fail():
        raise ValueError("fixture")

    with pytest.raises(ValueError, match="fixture"):
        await run_sync(fail)


def test_trade_archive_skips_unrelated_compressed_raw_before_decoding(store, now, monkeypatch):
    unrelated = store.save_raw(
        "TEST", "https://example.org/api/v3/exchangeInfo", {"symbols": ["large"] * 10000}, now
    )
    trades = store.save_raw(
        "TEST",
        "https://example.org/api/v3/aggTrades?symbol=BTCUSDT",
        [{"a": 1, "T": int(now.timestamp() * 1000), "p": "100", "q": "2", "m": False}],
        now,
    )
    snap = snapshot(now, raw_ids=[unrelated, trades])

    # A compressed non-trade record must never be opened by the trade archiver.
    def forbid_decompression(*args):
        raise AssertionError("Unrelated metadata was decompressed")

    monkeypatch.setattr("muse_btc.storage.gzip.decompress", forbid_decompression)
    archive_snapshot_trades(store, snap, None)
    archive_snapshot_trades(store, snap, None)
    result = cvd_summary(store, snap.asset_id, now)
    assert result[0]["observed_trades"] == 1
    assert result[0]["observed_cvd_usdt"] == 200


def test_latest_snapshot_query_keeps_point_in_time_and_same_time_order(store, now):
    first = snapshot(now, price=100)
    second = snapshot(now, price=101)
    future = snapshot(now + timedelta(minutes=1), price=999)
    for item in (first, second, future):
        store.save_snapshot(item)
    assert store.latest_snapshots(now)[0].id == second.id
    assert store.latest_snapshots(now + timedelta(minutes=1))[0].id == future.id
    assert not store.latest_snapshots(now - timedelta(seconds=1))


@pytest.mark.asyncio
async def test_network_concurrency_is_configurable_and_bounded(settings, store):
    settings.request_concurrency = 2
    entered, release = asyncio.Event(), asyncio.Event()
    active = peak = 0

    async def respond(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            entered.set()
        try:
            await release.wait()
            return httpx.Response(200, json={"ok": True})
        finally:
            active -= 1

    providers = Providers(settings, store, transport=httpx.MockTransport(respond))
    tasks = [
        asyncio.create_task(providers.get("TEST", "https://example.org", f"/data/{i}"))
        for i in range(6)
    ]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert peak == 2
    finally:
        release.set()
        await asyncio.gather(*tasks)
        await providers.close()
    assert peak == 2


@pytest.mark.asyncio
async def test_full_refresh_bounds_lineage_but_keeps_used_derivative_cache(settings, store):
    from test_providers_api import public_api_fixture

    provider = Providers(settings, store, transport=httpx.MockTransport(public_api_fixture))
    try:
        first = next(s for s in await provider.binance() if s.symbol == "BTCUSDT")
        old_trades = [
            raw for raw in first.raw_ids if "/api/v3/aggTrades?" in store.raw(raw)["endpoint"]
        ]
        cached_history = [
            raw
            for raw in first.raw_ids
            if "/api/v5/public/funding-rate-history?" in store.raw(raw)["endpoint"]
        ]
        assert old_trades and cached_history
        second = next(s for s in await provider.binance() if s.symbol == "BTCUSDT")
        assert not set(old_trades) & set(second.raw_ids)
        assert set(cached_history) <= set(second.raw_ids)
        assert len(second.raw_ids) <= len(first.raw_ids) + 1
        # Dropped references remain archived and readable for historical replay.
        assert all(store.raw(raw) for raw in old_trades)
    finally:
        await provider.close()
