import json

import httpx
import pytest
from test_providers_api import public_api_fixture

from muse_btc.providers import Providers


@pytest.mark.asyncio
async def test_market_universe_is_cached_and_followup_tickers_are_scoped(settings, store):
    requests = []

    def handler(request):
        requests.append(request)
        return public_api_fixture(request)

    providers = Providers(settings, store, transport=httpx.MockTransport(handler))
    try:
        first = await providers.binance()
        second = await providers.binance()
        assert len(first) == len(second) == 3
        info = [r for r in requests if r.url.path.endswith("/exchangeInfo")]
        tickers = [r for r in requests if r.url.path.endswith("/ticker/24hr")]
        assert len(info) == 1
        assert len(tickers) == 2
        assert "symbols" not in tickers[0].url.params
        assert set(json.loads(tickers[1].url.params["symbols"])) == {
            "BTCUSDT",
            "ETHUSDT",
            "SOLUSDT",
        }
        for snap in second:
            store.save_snapshot(snap)
            assert first[0].raw_ids[1] in snap.raw_ids
    finally:
        await providers.close()
