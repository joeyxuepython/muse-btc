from datetime import timedelta

import pytest
from conftest import candles

from muse_btc.features import candle_features, depth_features, derivatives_features
from muse_btc.models import Features


def test_open_future_candle_cannot_change_features(now):
    history = candles(now)
    baseline, _ = candle_features(history, now)
    future = history[-1].model_copy(
        update={
            "open_time": now + timedelta(seconds=1),
            "close_time": now + timedelta(minutes=1),
            "close": 10000,
            "quote_volume": 1000000,
        }
    )
    actual, issues = candle_features(history + [future], now)
    assert actual == baseline
    assert issues == []


def test_gaps_and_duplicates_are_detected(now):
    history = candles(now)
    _, issues = candle_features(history[:40] + history[41:] + [history[-1]], now)
    assert "CANDLE_GAPS" in issues
    assert "DUPLICATE_CANDLES" in issues


def test_cvd_is_windowed_quote_volume_and_rsi_has_valid_extremes(now):
    f, issues = candle_features(candles(now), now)
    assert not issues
    assert f.spot_taker_buy_ratio == pytest.approx(0.65)
    assert f.spot_cvd_window == 4500
    assert f.rsi14 == 100
    assert f.relative_volume == 1
    assert f.atr14 > 0
    assert f.return_5m_pct > 0


def test_zero_volume_is_unknown_not_fake_neutral(now):
    history = [
        c.model_copy(update={"quote_volume": 0, "taker_buy_quote_volume": 0}) for c in candles(now)
    ]
    f, _ = candle_features(history, now)
    assert f.relative_volume is None
    assert f.spot_taker_buy_ratio is None
    assert f.spot_cvd_window is None


def test_funding_units_oi_time_gap_and_future_points(now):
    mark = {"lastFundingRate": "0.0001", "markPrice": "101", "indexPrice": "100"}
    rows = [
        {
            "timestamp": int((now - timedelta(minutes=5)).timestamp() * 1000),
            "sumOpenInterest": "100",
        },
        {"timestamp": int(now.timestamp() * 1000), "sumOpenInterest": "102"},
        {
            "timestamp": int((now + timedelta(minutes=5)).timestamp() * 1000),
            "sumOpenInterest": "1000",
        },
    ]
    f = Features()
    derivatives_features(mark, rows, candles(now), f, now)
    assert f.funding_rate_pct == pytest.approx(0.01)
    assert f.oi_change_5m_pct == pytest.approx(2)
    assert f.basis_pct == pytest.approx(1)
    assert f.perp_taker_buy_ratio == pytest.approx(0.65)
    rows[0]["timestamp"] -= 300000
    other = Features()
    derivatives_features(mark, rows, [], other, now)
    assert other.oi_change_5m_pct is None


def test_depth_band_and_spread_use_notional():
    f = Features()
    depth_features(
        {"bids": [["99.9", "10"], ["97", "100"]], "asks": [["100.1", "20"], ["103", "100"]]}, f
    )
    assert f.spread_bps == pytest.approx(20)
    assert f.bid_depth_1pct_usd == pytest.approx(999)
    assert f.ask_depth_1pct_usd == pytest.approx(2002)
