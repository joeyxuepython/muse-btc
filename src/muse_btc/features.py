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
    bids = [(float(price), float(qty)) for price, qty in book.get("bids", [])]
    asks = [(float(price), float(qty)) for price, qty in book.get("asks", [])]
    if not bids or not asks:
        return
    best_bid, best_ask = max(p for p, _ in bids), min(p for p, _ in asks)
    if best_bid <= 0 or best_ask < best_bid:
        return
    mid = (best_bid + best_ask) / 2
    features.spread_bps = (best_ask - best_bid) / mid * 10000
    bid_depth = sum(price * qty for price, qty in bids if price >= mid * 0.99)
    ask_depth = sum(price * qty for price, qty in asks if price <= mid * 1.01)
    features.bid_depth_1pct_usd = bid_depth
    features.ask_depth_1pct_usd = ask_depth
    total = bid_depth + ask_depth
    features.depth_imbalance = (bid_depth - ask_depth) / total if total > 0 else None


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
