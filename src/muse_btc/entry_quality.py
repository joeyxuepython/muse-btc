"""Point-in-time entry checks and an explicitly experimental market confirmation.

Only archived public observations are used. The basket is equally weighted and
excludes the target; it is not CoinKarma LIQ or a claim about the whole market.
"""

import hashlib
import json
import math
from collections import defaultdict
from datetime import timedelta
from statistics import median

from .models import Module, SignalKind
from .rules import component_usable, usable

VERSION = "entry-quality-v1"
ENTRY_RULES = {"spot-led-momentum", "pre-pump-fusion"}
POLICY_FIELDS = (
    "enable_entry_quality",
    "entry_confirmation_seconds",
    "entry_min_depth_usdt",
    "entry_max_chase_pct",
    "market_confirmation_mode",
    "market_confirmation_min_assets",
    "market_confirmation_min_coverage",
    "stale_seconds",
    "time_alignment_seconds",
    "fee_bps_each_way",
    "slippage_bps_each_way",
    "rule_thresholds",
)
CHECK_NAMES = {
    "fresh_components": "盘口、成交与报价新鲜且对齐",
    "depth_floor": "双边已观测近端盘口金额满足下限",
    "persistent_demand": "连续新观测支持现货买入",
    "not_extended": "短期涨幅未超过追涨上限",
}


def inspect(snapshot, now, settings):
    f = snapshot.features
    times = [snapshot.component_times.get(k) for k in ("book", "candles")]
    fresh = (
        usable(snapshot, now, settings)
        and all(t is not None for t in times)
        and all(component_usable(snapshot, k, now, settings) for k in ("book", "candles"))
        and (max(*times, snapshot.market_time) - min(*times, snapshot.market_time)).total_seconds()
        <= settings.time_alignment_seconds
    )
    band = f.spot_depth_bands.get("0.1", {})
    bid, ask = band.get("observed_bid_usd"), band.get("observed_ask_usd")
    valid = all(
        isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v) and v > 0
        for v in (bid, ask)
    )
    complete = band.get("complete_band") is True and valid
    return {
        "fresh_components": bool(fresh),
        "complete_depth": complete,
        # A partial band is a lower bound: sufficient observed notional proves
        # this floor, but cannot support an imbalance or a market comparison.
        "depth_floor": bool(valid and min(bid, ask) >= settings.entry_min_depth_usdt),
        "bid_usdt": bid if valid else None,
        "ask_usdt": ask if valid else None,
        "imbalance": (bid - ask) / (bid + ask) if complete else None,
    }


def observation_window(snapshot, history, settings):
    """Latest consecutive observations, including intervening adverse observations.

    A cached book/candle cannot confirm itself. Choose the most recent anchor
    spanning the minimum time; never search past a failed observation for a win.
    """
    book = snapshot.component_times.get("book")
    candle = snapshot.component_times.get("candles")
    if not book or not candle:
        return []
    window = [snapshot]
    for old in reversed(history):
        if (
            old.id == snapshot.id
            or old.available_at >= snapshot.available_at
            or old.source != snapshot.source
        ):
            continue
        window.append(old)
        old_book, old_candle = (old.component_times.get(k) for k in ("book", "candles"))
        if (
            old_book is not None
            and old_candle is not None
            and settings.entry_confirmation_seconds
            <= (book - old_book).total_seconds()
            <= settings.stale_seconds
            and settings.entry_confirmation_seconds
            <= (candle - old_candle).total_seconds()
            <= settings.stale_seconds
        ):
            return window
    return []


def asset_quality(snapshot, history, now, settings):
    current = inspect(snapshot, now, settings)
    window = observation_window(snapshot, history, settings)
    ready = bool(window) and all(inspect(s, now, settings)["fresh_components"] for s in window)
    persistent = ready and all(
        inspect(s, now, settings)["depth_floor"]
        and s.features.spot_taker_buy_ratio is not None
        and s.features.spot_taker_buy_ratio >= settings.rule_thresholds["spot_buy_ratio"]
        for s in window
    )
    withdrawn = ready and all(
        s.features.spot_taker_buy_ratio is not None and s.features.spot_taker_buy_ratio < 0.45
        for s in window
    )
    returns = (snapshot.features.return_5m_pct, snapshot.features.return_15m_pct)
    checks = {k: current[k] for k in ("fresh_components", "depth_floor")}
    checks.update(
        persistent_demand=bool(persistent),
        not_extended=all(v is not None and v <= settings.entry_max_chase_pct for v in returns),
    )
    # The basket needs comparable complete books and actual buy flow, not just fewer asks.
    comparable = ready and all(inspect(s, now, settings)["complete_depth"] for s in window)
    previous = inspect(window[-1], now, settings) if comparable else None
    supported = bool(
        comparable
        and persistent
        and current["imbalance"] >= 0.1
        and current["imbalance"] > previous["imbalance"]
        and current["bid_usdt"] > previous["bid_usdt"]
    )
    return {
        "version": VERSION,
        "policy_id": hashlib.sha256(
            json.dumps({k: getattr(settings, k) for k in POLICY_FIELDS}, sort_keys=True).encode()
        ).hexdigest()[:12],
        "enabled": settings.enable_entry_quality,
        "as_of": now.isoformat(),
        "status": "PASS" if all(checks.values()) else "WAIT",
        "checks": checks,
        "reasons": [CHECK_NAMES[k] + "未满足" for k, passed in checks.items() if not passed],
        "snapshot_ids": [s.id for s in window] or [snapshot.id],
        "window_start": window[-1].available_at.isoformat() if window else None,
        "book_time": snapshot.component_times.get("book").isoformat()
        if snapshot.component_times.get("book")
        else None,
        "bid_depth_0_1pct_usdt": current["bid_usdt"],
        "ask_depth_0_1pct_usdt": current["ask_usdt"],
        "minimum_depth_usdt": settings.entry_min_depth_usdt,
        "complete_depth_band": current["complete_depth"],
        "depth_interpretation": "COMPLETE_BAND"
        if current["complete_depth"]
        else "OBSERVED_LOWER_BOUND",
        "round_trip_cost_bps": 2 * (settings.fee_bps_each_way + settings.slippage_bps_each_way),
        "max_chase_pct": settings.entry_max_chase_pct,
        "support_withdrawn": bool(withdrawn),
        "market_comparable": bool(comparable),
        "buy_support_improving": supported,
        "imbalance": current["imbalance"],
    }


def quality_contexts(store, now, settings, snapshots=()):
    membership = store.universe(now)
    allowed = (
        {"binance:" + row["binance_symbol"] for row in membership["entries"]}
        if membership
        else None
    )
    requested = sorted(allowed | {s.asset_id for s in snapshots}) if allowed is not None else None
    latest = {s.asset_id: s for s in store.latest_snapshots(now, asset_ids=requested)}
    latest.update({s.asset_id: s for s in snapshots if s.available_at <= now})
    latest = {
        k: s
        for k, s in latest.items()
        if s.module != Module.MEME and (s.decision_at is None or s.decision_at <= now)
    }
    history = defaultdict(list)
    for s in store.snapshot_range(now - timedelta(seconds=settings.stale_seconds), now):
        if s.decision_at is None or s.decision_at <= now:
            history[s.asset_id].append(s)
    qualities = {k: asset_quality(s, history[k], now, settings) for k, s in latest.items()}
    expected = allowed if allowed is not None else set(latest)
    results = {}
    for target, quality in qualities.items():
        peers = expected - {target}
        target_book = latest[target].component_times.get("book")
        valid = {
            k: qualities[k]
            for k in peers
            if k in qualities
            and qualities[k]["market_comparable"]
            and target_book is not None
            and abs((latest[k].component_times["book"] - target_book).total_seconds())
            <= settings.time_alignment_seconds
        }
        count = len(valid)
        coverage = count / len(peers) if peers else 0
        breadth = sum(q["buy_support_improving"] for q in valid.values()) / count if count else None
        imbalance = median(q["imbalance"] for q in valid.values()) if count else None
        covered = (
            count >= settings.market_confirmation_min_assets
            and coverage >= settings.market_confirmation_min_coverage
        )
        state = (
            "UNKNOWN"
            if not covered
            else "SUPPORTIVE"
            if breadth >= 0.6 and imbalance >= 0.1
            else "WEAK"
            if breadth <= 0.3 and imbalance < 0
            else "MIXED"
        )
        market = {
            "version": "market-confirmation-v1",
            "mode": settings.market_confirmation_mode,
            "scope": "TRACKED_BINANCE_SPOT_EQUAL_WEIGHT_EX_TARGET"
            if membership
            else "ARCHIVED_SPOT_SAMPLE_EQUAL_WEIGHT_EX_TARGET",
            "membership_known": membership is not None,
            "as_of": now.isoformat(),
            "status": state,
            "resonance": state == "SUPPORTIVE" and quality["buy_support_improving"],
            "eligible_assets": count,
            "expected_assets": len(peers),
            "coverage_pct": coverage * 100,
            "support_breadth_pct": breadth * 100 if breadth is not None else None,
            "median_imbalance": imbalance,
            "excluded_target": target,
            "missing_assets": sorted(peers - valid.keys()),
            "peer_snapshot_ids": {k: v["snapshot_ids"] for k, v in valid.items()},
            "limitations": [
                "等权覆盖样本，非全市场、非市值加权 LIQ；阈值尚待样本外验证",
                "盘口可撤单，成交窗口重叠；共振不代表独立证据或盈利概率",
                "价格移动会改变范围内档位；盘口金额变化不能等同净资金流入",
            ],
        }
        results[target] = {"entry_quality": quality, "market_confirmation": market}
    return results


def apply_quality(signals, assessment, trace, settings):
    for signal in signals:
        if signal.rule_id not in ENTRY_RULES:
            continue
        quality, market = assessment["entry_quality"], assessment["market_confirmation"]
        blocked = settings.enable_entry_quality and quality["status"] != "PASS"
        market_blocked = settings.market_confirmation_mode == "require" and not market["resonance"]
        if blocked or market_blocked:
            if signal.kind == SignalKind.ENTRY_CANDIDATE:
                signal.kind = SignalKind.WATCH
                signal.entry_zone = None
                signal.evidence_score = min(signal.evidence_score, 48)
                signal.title = "入场质量待确认：继续观察"
            signal.contradictions += quality["reasons"] if blocked else []
            if market_blocked:
                signal.contradictions.append("本币与覆盖市场买盘共振尚未确认")
        if settings.enable_entry_quality or settings.market_confirmation_mode == "require":
            signal.rule_version += "+" + VERSION + ":" + quality["policy_id"]
            signal.invalidation_conditions.append("连续新观测显示现货买入支撑消失")
        for row in trace:
            if row["rule_id"] == signal.rule_id:
                row.update(
                    status="TRIGGERED_" + signal.kind,
                    reasons=signal.contradictions,
                    rule_version=signal.rule_version,
                )
                if settings.enable_entry_quality:
                    row["checks"].update({"quality:" + k: v for k, v in quality["checks"].items()})
                if settings.market_confirmation_mode == "require":
                    row["checks"]["market_resonance"] = market["resonance"]
