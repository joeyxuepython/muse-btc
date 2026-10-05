"""Phase 3 cross-sectional dynamics and configurable pre-pump hypotheses."""

from datetime import datetime, timedelta

from .context import asset_context
from .intelligence import IntelligenceStore, digest
from .models import Module, SignalKind
from .rules import _signal, component_usable, usable
from .strategy_audit import record


def enrich_rankings(rows, snapshots, store, now, settings):
    by_id = {s.asset_id: s for s in snapshots}
    archive = IntelligenceStore(store)
    factors = {
        "relative_volume": "relative_volume",
        "oi_acceleration": "oi_acceleration_pct",
        "funding": "funding_rate_pct",
        "relative_strength": "relative_strength_15m_pct",
        "spot_flow": "spot_taker_buy_ratio",
        "taker_flow": "perp_taker_buy_ratio",
        "liquidity": "bid_depth_1pct_usd",
        "order_book": "depth_imbalance",
        "volatility": "realized_volatility_pct",
    }
    comparison_basis = digest(
        {
            "members": sorted(
                (
                    r["asset_id"],
                    r["data_ready"],
                    sorted(r.get("missing", [])),
                    r.get("coverage_pct"),
                )
                for r in rows
            ),
            "weights": settings.ranking_weights,
        }
    )
    for row in rows:
        row["rank_comparison_basis"] = comparison_basis
        row["rank_change_comparable"] = False
        context = asset_context(archive, row["asset_id"], now)
        row["context"] = context
        row["component_ranks"] = {}
        row["component_percentiles"] = {}
        row["rank_velocity_per_hour"] = None
        with store.connect() as db:
            old = db.execute(
                "SELECT payload,as_of FROM ranking_history WHERE asset_id=? "
                "AND as_of<? ORDER BY as_of DESC LIMIT 1",
                (row["canonical_asset_id"], now.isoformat()),
            ).fetchone()
        if old:
            import json

            prior = json.loads(old[0])
            hours = (now - datetime.fromisoformat(old[1])).total_seconds() / 3600
            comparable = comparison_basis == prior.get("rank_comparison_basis")
            row["rank_change_comparable"] = comparable
            if not comparable:
                row["rank_change"] = None
                row["score_delta"] = None
            if hours > 0 and row["data_ready"] and prior["data_ready"] and comparable:
                row["rank_velocity_per_hour"] = (prior["rank"] - row["rank"]) / hours
        if not row["rank_change_comparable"]:
            row["rank_change"] = None
            row["score_delta"] = None
        row["version"] = "rank-v4-4"
        row["threshold_version"] = settings.threshold_version
    for name, field in factors.items():
        values = []
        for row in rows:
            snapshot = by_id[row["asset_id"]]
            value = getattr(snapshot.features, field)
            component = {
                "funding": "funding",
                "oi_acceleration": "oi_history",
                "taker_flow": "taker",
                "order_book": "book",
                "liquidity": "book",
            }.get(name)
            if (
                value is not None
                and row["data_ready"]
                and (not component or component_usable(snapshot, component, now, settings))
            ):
                values.append((row, -abs(value) if name == "funding" else value))
        for row, value in values:
            rank = 1 + sum(v > value for _, v in values)
            row["component_ranks"][name] = rank
            row["component_percentiles"][name] = (len(values) - rank + 1) / len(values) * 100
    return rows


def pre_pump_signals(snapshot, regime, now, settings, store, trace=None, *, context=None):
    if snapshot.module != Module.ALT or not usable(snapshot, now, settings):
        record(
            trace,
            snapshot,
            "pre-pump-fusion",
            status="NOT_APPLICABLE" if snapshot.module != Module.ALT else "MISSING_DATA",
            reasons=["仅适用于行情有效的 ALT"],
        )
        return []
    f, t = snapshot.features, settings.rule_thresholds
    flat = f.return_15m_pct is not None and abs(f.return_15m_pct) <= t["flat_price_pct"]
    volume = f.relative_volume is not None and f.relative_volume >= t["rvol"]
    spot = (
        component_usable(snapshot, "candles", now, settings)
        and f.spot_taker_buy_ratio is not None
        and f.spot_taker_buy_ratio >= t["spot_buy_ratio"]
    )
    oi = (
        component_usable(snapshot, "oi_history", now, settings)
        and f.oi_change_5m_pct is not None
        and f.oi_change_5m_pct >= t["oi_build_pct"]
    )
    funding_ready = (
        component_usable(snapshot, "funding", now, settings) and f.funding_rate_pct is not None
    )
    cool = funding_ready and abs(f.funding_rate_pct) < t["funding_hot_pct"]
    strength = (
        f.relative_strength_15m_pct is not None
        and f.relative_strength_15m_pct >= t["relative_strength_pct"]
    )
    patterns, groups, evidence = [], set(), []
    if flat and volume:
        patterns.append("A: 平价放量")
        groups.update(("price", "spot_volume"))
    if flat and oi and cool:
        patterns.append("B: 温和价格 / 头寸建立")
        groups.update(("price", "derivatives"))
    if (
        spot
        and component_usable(snapshot, "taker", now, settings)
        and f.spot_perp_structure == "SPOT_LED_HYPOTHESIS"
    ):
        patterns.append("C: 现货先行")
        groups.update(("spot_flow", "cross_venue"))
    if oi and funding_ready and f.funding_rate_pct < 0 and (f.return_5m_pct or 0) > 0:
        patterns.append("D: 空头挤压候选")
        groups.update(("price", "derivatives"))
    btc = store.snapshot(regime.btc_snapshot_id) if regime.btc_snapshot_id else None
    btc_flat = (
        btc
        and usable(btc, now, settings)
        and btc.features.return_15m_pct is not None
        and abs(btc.features.return_15m_pct) <= t["flat_price_pct"]
    )
    if btc_flat and strength and volume and cool:
        patterns.append("E: 相对 BTC 轮动")
        groups.update(("relative_strength", "spot_volume", "derivatives"))
    old = [
        s
        for s in store.snapshot_range(now - timedelta(hours=1), now, snapshot.asset_id)
        if s.id != snapshot.id
        and s.component_times.get("book")
        and component_usable(s, "book", now, settings)
        and s.available_at < snapshot.available_at
    ]
    if old:
        previous = old[-1].features
        if (
            flat
            and spot
            and component_usable(snapshot, "book", now, settings)
            and f.spot_depth_bands.get("1", {}).get("complete_band") is True
            and previous.spot_depth_bands.get("1", {}).get("complete_band") is True
            and f.spot_cvd_window is not None
            and previous.spot_cvd_window is not None
            and f.spot_cvd_window > previous.spot_cvd_window
            and f.ask_depth_1pct_usd is not None
            and previous.ask_depth_1pct_usd is not None
            and f.ask_depth_1pct_usd < previous.ask_depth_1pct_usd
        ):
            patterns.append("F: 吸筹假设 / 卖方深度收缩")
            groups.update(("price", "spot_flow", "order_book"))
    if trace is not None:
        requirements = {
            "A": ("return_15m_pct", "relative_volume"),
            "B": ("return_15m_pct", "oi_change_5m_pct", "funding_rate_pct"),
            "C": ("spot_taker_buy_ratio", "spot_perp_structure"),
            "D": ("oi_change_5m_pct", "funding_rate_pct", "return_5m_pct"),
            "E": ("relative_strength_15m_pct", "relative_volume", "funding_rate_pct"),
            "F": (
                "return_15m_pct",
                "spot_taker_buy_ratio",
                "spot_cvd_window",
                "ask_depth_1pct_usd",
            ),
        }
        for pattern, names in requirements.items():
            inputs = {name: getattr(f, name) for name in names}
            for field, component in (
                ("oi_change_5m_pct", "oi_history"),
                ("funding_rate_pct", "funding"),
                ("ask_depth_1pct_usd", "book"),
                ("spot_taker_buy_ratio", "candles"),
            ):
                if field in inputs and not component_usable(snapshot, component, now, settings):
                    inputs[field] = None
            if pattern == "E":
                inputs["btc_return_15m_pct"] = (
                    btc.features.return_15m_pct if btc and usable(btc, now, settings) else None
                )
            if pattern == "C" and not component_usable(snapshot, "taker", now, settings):
                inputs["spot_perp_structure"] = None
            if pattern == "F":
                inputs["previous_cvd"] = old[-1].features.spot_cvd_window if old else None
                if f.spot_depth_bands.get("1", {}).get("complete_band") is not True:
                    inputs["ask_depth_1pct_usd"] = None
                inputs["previous_ask_depth"] = (
                    old[-1].features.ask_depth_1pct_usd
                    if old
                    and old[-1].features.spot_depth_bands.get("1", {}).get("complete_band") is True
                    else None
                )
            record(
                trace,
                snapshot,
                "pre-pump:" + pattern,
                inputs=inputs,
                role="PATTERN",
                status="MATCHED" if any(p.startswith(pattern + ":") for p in patterns) else None,
            )
    if not patterns:
        missing = bool(trace) and all(
            r["status"] == "MISSING_DATA" for r in trace if r["role"] == "PATTERN"
        )
        record(
            trace,
            snapshot,
            "pre-pump-fusion",
            status="MISSING_DATA" if missing else "NOT_TRIGGERED",
            reasons=["A–F 无已确认匹配；逐项缺失见子模式"],
        )
        return []
    if spot:
        groups.add("spot_flow")
    if strength:
        groups.add("relative_strength")
    if context is None:
        context = asset_context(IntelligenceStore(store), snapshot.asset_id, now)
    market_groups = set(groups)
    groups.update(context["evidence_groups"])
    evidence.extend(patterns + context["supporting"])
    contradictions = list(context["risks"])
    if not cool:
        contradictions.append("Funding 缺失或已过热")
    if regime.risk_mode != "NORMAL":
        contradictions.append("BTC 风险背景尚未允许强信号")
    liquid = (
        component_usable(snapshot, "book", now, settings)
        and f.spread_bps is not None
        and f.spread_bps <= t["max_spread_bps"]
    )
    aligned = (
        all(
            component_usable(snapshot, k, now, settings)
            and abs((snapshot.component_times[k] - snapshot.market_time).total_seconds())
            <= settings.time_alignment_seconds
            for k in ("funding", "oi", "mark")
        )
        if snapshot.component_times
        else True
    )
    if not spot:
        contradictions.append("尚无实际现货主动买入确认")
    if not aligned:
        contradictions.append("现货与衍生品时间不对齐")
    strong = (
        len(market_groups) >= t["strong_groups"]
        and spot
        and aligned
        and cool
        and liquid
        and regime.risk_mode == "NORMAL"
        and not context["risks"]
    )
    signal = _signal(
        snapshot,
        regime,
        now,
        SignalKind.ENTRY_CANDIDATE if strong else SignalKind.WATCH,
        "pre-pump-fusion",
        "启动前证据融合",
        evidence,
        sorted(groups),
        min(95, 15 * len(groups)),
        contradictions,
    )
    signal.rule_version = "fusion-v4-4:" + settings.threshold_version
    signal.signal_version = "signals-v4-3"
    signal.patterns = patterns
    signal.context = regime.research_context
    record(
        trace,
        snapshot,
        "pre-pump-fusion",
        signal=signal,
        inputs={
            "patterns": patterns,
            "evidence_groups": sorted(groups),
            "market_evidence_groups": sorted(market_groups),
            "funding_ready": funding_ready,
            "liquid": liquid,
            "btc_risk_mode": regime.risk_mode,
            "funding_rate_pct": f.funding_rate_pct if funding_ready else None,
            "oi_change_5m_pct": f.oi_change_5m_pct
            if component_usable(snapshot, "oi_history", now, settings)
            else None,
            "spread_bps": f.spread_bps
            if component_usable(snapshot, "book", now, settings)
            else None,
            "relative_strength_15m_pct": f.relative_strength_15m_pct,
        },
        checks={
            "evidence_groups": len(market_groups) >= t["strong_groups"],
            "spot_demand_confirmed": spot,
            "time_alignment": aligned,
            "derivatives_cool": cool,
            "liquidity": liquid,
            "btc_regime": regime.risk_mode == "NORMAL",
            "asset_context": not context["risks"],
        },
    )
    return [signal]
