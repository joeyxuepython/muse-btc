from datetime import UTC, datetime, timedelta

import pytest

from muse_btc.config import Settings
from muse_btc.models import Candle, Features, Module, Snapshot
from muse_btc.storage import Store


@pytest.fixture
def now():
    return datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_path=tmp_path / "test.db", enable_collector=False, max_altcoins=2, max_memes=2
    )


@pytest.fixture
def store(settings):
    return Store(settings.database_path)


def snapshot(at, module=Module.BTC, **overrides):
    data = dict(
        asset_id="binance:BTCUSDT" if module == Module.BTC else "binance:ETHUSDT",
        symbol="BTCUSDT" if module == Module.BTC else "ETHUSDT",
        module=module,
        source="TEST_FIXTURE",
        market_time=at,
        available_at=at,
        price=100,
        features=Features(
            return_5m_pct=0.7,
            return_15m_pct=1,
            relative_volume=2.5,
            ema20=102,
            ema50=100,
            atr14=1,
            spot_taker_buy_ratio=0.65,
            funding_rate_pct=0.01,
            oi_change_5m_pct=0.5,
            spread_bps=2,
            relative_strength_15m_pct=0.5,
        ),
    )
    data.update(overrides)
    return Snapshot(**data)


def candles(at, count=100):
    result = []
    for i in range(count):
        end = at - timedelta(minutes=count - i - 1)
        close = 100 + i * 0.1
        result.append(
            Candle(
                open_time=end - timedelta(seconds=59.999),
                close_time=end,
                open=close - 0.05,
                high=close + 0.1,
                low=close - 0.1,
                close=close,
                volume=10,
                quote_volume=1000,
                taker_buy_quote_volume=650,
            )
        )
    return result
