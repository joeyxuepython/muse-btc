"""Binance-only proxy routing: SG node for Binance hosts, default egress otherwise."""

import httpx

from muse_btc.config import Settings
from muse_btc.providers import Providers
from muse_btc.storage import Store

SG_PROXY = "http://user:pass@sg-proxy.example:3128"


def _providers(tmp_path, **overrides):
    settings = Settings(database_path=tmp_path / "proxy.db", **overrides)
    store = Store(settings.database_path)
    return Providers(settings, store)


def _route(client, url):
    return client._transport_for_url(httpx.URL(url))


def _proxy_host(transport):
    pool = getattr(transport, "_pool", None)
    proxy_url = getattr(pool, "_proxy_url", None)
    host = proxy_url.host if proxy_url is not None else None
    return host.decode() if isinstance(host, bytes) else host


def test_binance_hosts_use_configured_proxy(tmp_path):
    providers = _providers(tmp_path, binance_proxy=SG_PROXY)
    try:
        for url in (
            "https://data-api.binance.vision/api/v3/ticker/24hr",
            "https://fapi.binance.com/fapi/v1/premiumIndex",
        ):
            transport = _route(providers.client, url)
            assert isinstance(transport, httpx.AsyncHTTPTransport)
            assert _proxy_host(transport) == "sg-proxy.example"
    finally:
        import asyncio

        asyncio.run(providers.close())


def test_other_hosts_keep_default_egress(tmp_path):
    providers = _providers(tmp_path, binance_proxy=SG_PROXY)
    try:
        transport = _route(providers.client, "https://api.dexscreener.com/token-profiles/latest/v1")
        assert _proxy_host(transport) != "sg-proxy.example"
    finally:
        import asyncio

        asyncio.run(providers.close())


def test_no_proxy_configured_means_no_mounts(tmp_path):
    providers = _providers(tmp_path)
    try:
        assert Providers._proxy_mounts(providers.settings, None) is None
    finally:
        import asyncio

        asyncio.run(providers.close())
