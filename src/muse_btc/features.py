import math
import statistics
from datetime import datetime

from .models import Candle, Features


def percent_change(current: float, previous: float) -> float | None:
    return (current / previous - 1) * 100 if previous > 0 else None


def ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    result = statistics.mean(values[:period])
    alpha = 2 / (period + 1)
    for value in values[period:]:
        result = alpha * value + (1 - alpha) * result
    return result


def candle_features(candles: list[Candle], as_of: datetime) -> tuple[Features, list[str]]:
    # Open candles and observations from the future never enter feature computation.
    closed = sorted((c for c in candles if c.close_time <= as_of), key=lambda c: c.open_time)
    issues: list[str] = []
    if len(closed) < 61:
        issues.append("INSUFFICIENT_CANDLE_HISTORY")
    if len({c.open_time for c in closed}) != len(closed):
        issues.append("DUPLICATE_CANDLES")
    if any(
        c.close_time <= c.open_time
        or c.low > min(c.open, c.close)
        or c.high < max(c.open, c.close)
        or c.taker_buy_quote_volume > c.quote_volume
        for c in closed
    ):
        issues.append("INVALID_CANDLE_VALUES")
    if any(
        (right.open_time - left.open_time).total_seconds() != 60
        for left, right in zip(closed, closed[1:], strict=False)
    ):
        issues.append("CANDLE_GAPS")
    result = Features()
    if not closed:
        return result, issues
    closes = [c.close for c in closed]
    for minutes, field in [(5, "return_5m_pct"), (15, "return_15m_pct"), (60, "return_1h_pct")]:
        if len(closes) > minutes:
            setattr(result, field, percent_change(closes[-1], closes[-1 - minutes]))
    result.ema20 = ema(closes, 20)
    result.ema50 = ema(closes, 50)
    if len(closed) >= 61:
        baseline = statistics.mean(c.quote_volume for c in closed[-61:-1])
        result.relative_volume = closed[-1].quote_volume / baseline if baseline > 0 else None
    if len(closed) >= 10:
        previous = sum(c.quote_volume for c in closed[-10:-5])
        result.volume_acceleration = (
            sum(c.quote_volume for c in closed[-5:]) / previous if previous > 0 else None
        )
    flow_window = closed[-15:]
    volume = sum(c.quote_volume for c in flow_window)
    taker_buy = sum(c.taker_buy_quote_volume for c in flow_window)
    if volume > 0 and 0 <= taker_buy <= volume:
        result.spot_taker_buy_ratio = taker_buy / volume
        result.spot_cvd_window = 2 * taker_buy - volume
    elif volume > 0:
        issues.append("INVALID_TAKER_VOLUME")
    if len(closed) >= 15:
        window = closed[-15:]
        result.atr14 = statistics.mean(
            max(c.high - c.low, abs(c.high - prior.close), abs(c.low - prior.close))
            for prior, c in zip(window, window[1:], strict=False)
        )
        changes = [
            right.close - left.close for left, right in zip(window, window[1:], strict=False)
        ]
        gain = statistics.mean(max(0, value) for value in changes)
        loss = statistics.mean(max(0, -value) for value in changes)
        result.rsi14 = 100 - 100 / (1 + gain / loss) if loss else (100 if gain else 50)
    if len(closes) >= 31:
        returns = [
            math.log(right / left) for left, right in zip(closes[-31:], closes[-30:], strict=False)
        ]
        result.realized_volatility_pct = statistics.pstdev(returns) * 100
    return result, issues


def depth_features(book: dict, features: Features) -> None:
    bids = [(float(price), float(qty)) for price, qty in (row[:2] for row in book.get("bids", []))]
    asks = [(float(price), float(qty)) for price, qty in (row[:2] for row in book.get("asks", []))]
    if any(not math.isfinite(p) or not math.isfinite(q) or p <= 0 or q < 0 for p, q in bids + asks):
        raise ValueError("Invalid order book price or quantity")
    bids, asks = ([(p, q) for p, q in side if q > 0] for side in (bids, asks))
    if not bids or not asks:
        raise ValueError("Empty order book")
    if len({p for p, _ in bids}) != len(bids) or len({p for p, _ in asks}) != len(asks):
        raise ValueError("Duplicate order book levels")
    best_bid, best_ask = max(p for p, _ in bids), min(p for p, _ in asks)
    if best_ask <= best_bid:
        raise ValueError("Crossed or locked order book")
    mid = (best_bid + best_ask) / 2
    features.spread_bps = (best_ask - best_bid) / mid * 10000
    bid_depth = sum(price * qty for price, qty in bids if price >= mid * 0.99)
    ask_depth = sum(price * qty for price, qty in asks if price <= mid * 1.01)
    features.bid_depth_1pct_usd = bid_depth
    features.ask_depth_1pct_usd = ask_depth
    total = bid_depth + ask_depth
    features.depth_imbalance = (bid_depth - ask_depth) / total if total > 0 else None
    for band in (0.1, 0.5, 1, 2, 5):
        fraction = band / 100
        bid = sum(p * q for p, q in bids if p >= mid * (1 - fraction))
        ask = sum(p * q for p, q in asks if p <= mid * (1 + fraction))
        features.spot_depth_bands[str(band)] = {
            "observed_bid_usd": bid,
            "observed_ask_usd": ask,
            "complete_band": min(p for p, _ in bids) <= mid * (1 - fraction)
            and max(p for p, _ in asks) >= mid * (1 + fraction),
        }


def cross_venue_features(features, price, times, now, settings):
    """Independent normalized flow hypotheses; never estimates actual liquidations."""
    features.spot_perp_structure = features.deleveraging_signal = None
    features.basis_pct = features.cross_venue_premium_pct = None

    def aligned(*names):
        stamps = [times.get(name) for name in names]
        return (
            all(
                t is not None and 0 <= (now - t).total_seconds() <= settings.stale_seconds
                for t in stamps
            )
            and (max(stamps) - min(stamps)).total_seconds() <= settings.time_alignment_seconds
        )

    if features.mark_price and price > 0 and aligned("mark", "quote"):
        features.cross_venue_premium_pct = (features.mark_price / price - 1) * 100
    if features.mark_price and features.index_price and aligned("mark", "index"):
        features.basis_pct = (features.mark_price / features.index_price - 1) * 100
    spot, perp = features.spot_taker_buy_ratio, features.perp_taker_buy_ratio
    if spot is not None and perp is not None and aligned("candles", "taker"):
        if spot >= 0.58 and perp < 0.55:
            features.spot_perp_structure = "SPOT_LED_HYPOTHESIS"
        elif perp >= 0.58 and spot < 0.55:
            features.spot_perp_structure = "PERP_LED_HYPOTHESIS"
        elif spot >= 0.58 and perp >= 0.58:
            features.spot_perp_structure = "JOINT_DEMAND_HYPOTHESIS"
        else:
            features.spot_perp_structure = "MIXED"
    if (
        features.return_5m_pct is not None
        and features.oi_change_5m_pct is not None
        and perp is not None
        and aligned("candles", "oi_history", "taker")
    ):
        features.deleveraging_signal = (
            features.return_5m_pct < -0.5 and features.oi_change_5m_pct < -1 and perp < 0.45
        )


def derivatives_features(
    mark: dict, oi_history: list[dict], candles: list[Candle], features: Features, as_of: datetime
) -> None:
    features.funding_rate_pct = float(mark["lastFundingRate"]) * 100
    features.basis_pct = percent_change(float(mark["markPrice"]), float(mark["indexPrice"]))
    history = sorted(
        (point for point in oi_history if int(point["timestamp"]) <= as_of.timestamp() * 1000),
        key=lambda point: int(point["timestamp"]),
    )
    # Do not call an arbitrary gap a five-minute OI change.
    if len(history) >= 2:
        gap = int(history[-1]["timestamp"]) - int(history[-2]["timestamp"])
        age = as_of.timestamp() * 1000 - int(history[-1]["timestamp"])
        if gap == 300000 and 0 <= age <= 600000:
            features.oi_change_5m_pct = percent_change(
                float(history[-1]["sumOpenInterest"]), float(history[-2]["sumOpenInterest"])
            )
    if len(history) >= 20:
        values = [float(p["sumOpenInterest"]) for p in history]
        deviation = statistics.pstdev(values[:-1])
        features.oi_zscore = (
            (values[-1] - statistics.mean(values[:-1])) / deviation if deviation > 0 else None
        )
    flow = [c for c in candles if c.close_time <= as_of][-15:]
    volume = sum(c.quote_volume for c in flow)
    buy_volume = sum(c.taker_buy_quote_volume for c in flow)
    if volume > 0 and 0 <= buy_volume <= volume:
        features.perp_taker_buy_ratio = buy_volume / volume
        features.perp_cvd_window = 2 * buy_volume - volume
