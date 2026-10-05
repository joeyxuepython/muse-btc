import json

import httpx
import pytest
from conftest import snapshot
from test_providers_api import public_api_fixture

from muse_btc.models import Module
from muse_btc.providers import Providers
from muse_btc.rules import evaluate, market_regime
from muse_btc.service import Collector, signal_view


@pytest.mark.asyncio
async def test_102_quotes_rotation_meme_eligibility_and_detail_failure(settings, store):
    settings.max_altcoins = 100
    settings.detail_batch_size = 20
    calls = []
    symbols = ["BTCUSDT", "ETHUSDT", "PEPEUSDT"] + [f"COIN{i}USDT" for i in range(104)]

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/exchangeInfo"):
            return httpx.Response(
                200,
                json={
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
                },
            )
        if request.url.path.endswith("/ticker/24hr"):
            template = public_api_fixture(request).json()[0]
            requested = (
                json.loads(request.url.params["symbols"])
                if "symbols" in request.url.params
                else symbols
            )
            return httpx.Response(
                200,
                json=[
                    dict(
                        template,
                        symbol=s,
                        quoteVolume=str(1000000 - symbols.index(s)),
                        lastPrice=str(100 + len(calls)),
                    )
                    for s in requested
                ],
            )
        if request.url.path.endswith("/depth") and request.url.params["symbol"] == "PEPEUSDT":
            return httpx.Response(500)
        return public_api_fixture(request)

    providers = Providers(settings, store, transport=httpx.MockTransport(handler))
    try:
        detailed = set()
        prices = []
        for _ in range(10):
            result = await providers.binance()
            assert len(result) == 102
            assert result[0].symbol == "BTCUSDT"
            assert "PEPEUSDT" in {s.symbol for s in result}
            assert result[0].detail_updated_at is not None
            detailed.update(s.symbol for s in result if s.detail_updated_at)
            prices.append(result[0].price)
            for asset in result:
                store.save_snapshot(asset)
                if asset.detail_updated_at is None:
                    assert asset.features.ema20 is None
                    assert (
                        evaluate(
                            asset,
                            market_regime(result[0], asset.available_at, settings),
                            asset.available_at,
                            settings,
                        )
                        == []
                    )
        assert len(detailed) == 102  # All candles sampled; failed PEPE book stays missing.
        assert len(set(prices)) == 10
        assert providers.coverage["quotes"] == providers.coverage["selected"] == 102
        assert providers.coverage["estimated_detail_refresh_seconds"] == 600
        assert not providers.coverage["confirmation_cadence_feasible"]
        assert any(s.symbol == "PEPEUSDT" and s.features.spread_bps is None for s in result)
        assert sum(r.url.path.endswith("/depth") for r in calls) == 220
        assert not any("dexscreener" in r.url.host or "goplus" in r.url.host for r in calls)
    finally:
        await providers.close()


@pytest.mark.asyncio
async def test_retired_sources_do_not_run_even_with_old_config(settings, store, now):
    settings.enable_goplus = True
    requests = []
    providers = Providers(
        settings,
        store,
        transport=httpx.MockTransport(lambda r: requests.append(r) or httpx.Response(500)),
    )
    collector = Collector(settings, store, providers)
    legacy = snapshot(now, Module.MEME, asset_id="dex:base:old:pool")
    store.save_snapshot(legacy)
    old = evaluate(snapshot(now), market_regime(snapshot(now), now, settings), now, settings)[0]
    old.module = Module.MEME
    old.asset_id = legacy.asset_id
    old.snapshot_id = legacy.id
    old.btc_snapshot_id = None
    store.save_signal(old)
    before = store.counts()
    try:
        collector.initialise_statuses()
        assert await providers.memes() == []
        assert await providers._meme_asset("base", "oldaddress", "raw") is None
        assert not requests
        assert collector._process_signals([], market_regime(None, now, settings), now) == 0
        assert store.counts() == before
        assert signal_view(store, old, now, settings)["state"] == "ARCHIVED"
        assert {s.name: s.state for s in store.statuses()}["GoPlus"] == "DISABLED"
    finally:
        await providers.close()
