"""Archive the actual rule trace; distinguish gates from research context."""

from copy import deepcopy

from .entry_quality import CHECK_NAMES as QUALITY_CHECK_NAMES
from .rules import component_usable

VERSION = "decision-explanation-v1"
CHECK_NAMES = {
    **{"quality:" + k: v for k, v in QUALITY_CHECK_NAMES.items()},
    "market_resonance": "本币与覆盖市场买盘共振",
    "spot_demand_confirmed": "融合规则的实际现货买入确认",
    "momentum": "价格与放量",
    "spot_demand": "现货主动买入",
    "ema_trend": "EMA 趋势",
    "relative_strength": "相对 BTC 强度（BTC/ETH 豁免）",
    "btc_regime": "BTC 风险状态",
    "derivatives_cool": "Funding/OI 未过热",
    "liquidity": "盘口价差",
    "time_alignment": "现货/衍生品时间对齐",
    "no_competing_signal": "无其他基础规则信号（含挤压观察）",
    "evidence_groups": "市场证据组数量（背景不计）",
    "asset_context": "催化与代币经济风险",
}
DIMENSION_NAMES = {
    "spot_demand": "BTC 现货需求",
    "leverage_risk": "BTC 杠杆",
    "macro_liquidity": "宏观流动性代理",
    "etf_flow": "ETF 资金流",
    "stablecoin_supply": "稳定币供应",
    "onchain_valuation_percentile": "MVRV 分位",
    "options_iv": "期权 IV",
    "research_bias": "已审阅研究",
}
CONTEXT_NAMES = {"catalyst": "已核实催化", "tokenomics": "代币经济", "fundamental": "基本面"}
INPUT_NAMES = {
    "net_liquidity_change_usd": "流动性代理变化（美元）",
    "five_observation_etf_flow_usd": "五次 ETF 观测净流（美元）",
    "mvrv_percentile": "MVRV 历史分位（%）",
    "threshold_percentile": "观察阈值（%）",
    "unique_daily_points": "不同日样本数",
    "sopr": "历史 SOPR",
    "sth_realized_price_usd": "历史短持者成本（美元）",
    "current_btc_price_usd": "当前 BTC 价格（美元）",
    "median_atm_iv_pct": "平值 IV 中位数（%）",
    "threshold_iv_pct": "观察阈值（%）",
    "contracts": "有效合约样本数",
}
COMPONENTS = {
    "funding_rate_pct": "funding",
    "oi_change_5m_pct": "oi_history",
    "spread_bps": "book",
    "ask_depth_1pct_usd": "book",
    "spot_taker_buy_ratio": "candles",
}
NOT_INTEGRATED = [
    "社交/KOL、钱包行为、DEX/Intent/Solver、完整清算及宏观事件反应未参与本候选决策；"
    "本次未检查这些数据是否已采集",
    "实验模型未进入生产信号；机会排行只用于排序，不生成候选或代表盈利概率",
]


def explain(signal, snapshot, regime, trace, context, now, settings):
    rows = deepcopy(trace)
    for row in rows:
        for field in ("signal_id", "alert_id", "notification_id", "publication", "delivery"):
            row.pop(field, None)  # These are determined later, at publication/receipt time.
    selected = next((r for r in rows if r["rule_id"] == signal.rule_id), {})
    relevant = [selected] if selected else []
    if signal.rule_id == "pre-pump-fusion":
        relevant += [r for r in rows if r["role"] == "PATTERN" and r["status"] == "MATCHED"]
    gates = [
        {"id": key, "name": CHECK_NAMES.get(key, key), "status": "PASS" if value else "BLOCKED"}
        for key, value in selected.get("checks", {}).items()
    ]
    if signal.rule_id in ("spot-led-momentum", "pre-pump-fusion"):
        gates = [g for g in gates if g["id"] != "asset_context"]
        gates.append(
            {
                "id": "asset_context",
                "name": CHECK_NAMES["asset_context"],
                "status": "BLOCKED"
                if context.get("risks")
                else "DISABLED"
                if not settings.enable_intelligence
                else "UNKNOWN"
                if set(context.get("missing", [])) & {"catalyst", "tokenomics"}
                else "NO_KNOWN_BLOCKER",
                "reasons": context.get("risks", []),
            }
        )
    gaps = []
    for row in rows:
        for field in row.get("missing", []):
            component = COMPONENTS.get(field)
            raw = getattr(snapshot.features, field, None)
            state = "MISSING"
            if (
                raw is not None
                and component
                and not component_usable(snapshot, component, now, settings)
            ):
                stamp = snapshot.component_times.get(component)
                state = (
                    "MISSING_TIMESTAMP" if stamp is None else "FUTURE" if stamp > now else "STALE"
                )
            gaps.append(
                {
                    "rule_id": row["rule_id"],
                    "field": field,
                    "status": state,
                    "scope": "THIS_RULE" if row in relevant else "OTHER_RULE",
                    "component": component,
                    "market_time": snapshot.component_times[component].isoformat()
                    if component in snapshot.component_times
                    else None,
                }
            )
    if settings.enable_intelligence:
        gaps += [
            {"field": k, "status": "MISSING_OR_UNUSABLE", "scope": "ASSET_CONTEXT"}
            for k in context.get("missing", [])
        ]
        gaps += [
            {"field": f"tokenomics.{k}", "status": "MISSING", "scope": "ASSET_CONTEXT"}
            for item in context.get("evaluations", [])
            for k in item.get("missing_fields", [])
        ]
    background = []
    observations = {r["rule_id"]: r for r in regime.research_context.get("observations", [])}
    for key, item in regime.research_context.get("dimensions", {}).items():
        role = (
            "BTC_GATE_INPUT"
            if signal.rule_id in ("spot-led-momentum", "pre-pump-fusion")
            and key in ("spot_demand", "leverage_risk", "macro_liquidity", "etf_flow")
            else "BACKGROUND_ONLY"
        )
        background.append({"id": key, "name": DIMENSION_NAMES[key], "role": role, **deepcopy(item)})
        if item["status"] == "MISSING":
            gaps.append(
                {
                    "field": DIMENSION_NAMES[key],
                    "status": "MISSING_OR_STALE",
                    "scope": "BTC_CONTEXT",
                }
            )
    for item in observations.values():
        background.append(
            {
                "id": item["rule_id"],
                "name": item["title"],
                "role": "THIS_OBSERVATION"
                if item["rule_id"] == signal.rule_id
                else "MACRO_GATE_CONTEXT"
                if item["rule_id"] == "btc-macro-stress"
                and signal.rule_id in ("spot-led-momentum", "pre-pump-fusion")
                else "BACKGROUND_ONLY",
                **deepcopy(item),
            }
        )
    for item in context.get("evaluations", []):
        if item["kind"] == "fundamental":
            background.append({"id": "fundamental", "name": "基本面", **deepcopy(item)})
    summaries = {
        "patterns",
        "evidence_groups",
        "market_evidence_groups",
        "funding_ready",
        "liquid",
        "btc_risk_mode",
    }
    inputs = {
        f"{r['rule_id']}:{k}": v
        for r in relevant
        for k, v in r.get("inputs", {}).items()
        if k not in summaries
    }
    return {
        "version": VERSION,
        "as_of": now.isoformat(),
        "snapshot_id": snapshot.id,
        "role": "RESEARCH_OBSERVATION"
        if signal.model_version == "context-observation"
        else "RULE_DECISION",
        "rule_id": signal.rule_id,
        "signal_kind": signal.kind,
        "market_time": snapshot.market_time.isoformat(),
        "available_at": snapshot.available_at.isoformat(),
        "raw_ids": list(snapshot.raw_ids),
        "component_times": {k: v.isoformat() for k, v in snapshot.component_times.items()},
        "triggered": relevant,
        "evaluations": rows,
        "gates": gates,
        "data_gaps": gaps,
        "asset_context": deepcopy(context),
        "background": background,
        "btc_regime": {
            "risk_mode": regime.risk_mode,
            "snapshot_id": regime.btc_snapshot_id,
            "as_of": regime.as_of.isoformat(),
            "evidence": list(regime.evidence),
            "contradictions": list(regime.contradictions),
            "macro": regime.macro,
            "context_as_of": regime.research_context.get("as_of"),
        },
        "coverage": {
            "usable_inputs": sum(v is not None for v in inputs.values()),
            "required_inputs": len(inputs),
            "scope": "THIS_RULE_AND_MATCHED_PATTERNS",
        },
        "not_integrated": list(NOT_INTEGRATED),
        "limitations": [
            "规则、A–F 子模式共享数据，不是独立策略投票；输入覆盖不等于胜率",
            "缺失资产背景不会自动否决；通过仅表示未发现已知阻断，不等于风险已全面核实",
        ]
        + (
            []
            if snapshot.component_times
            else ["旧式快照无组件时点，沿用整体行情时间；无法证明组件独立新鲜"]
        ),
        "conditions": {
            "invalidation": list(signal.invalidation_conditions),
            "expires_at": signal.expires_at.isoformat(),
            "thresholds": deepcopy(settings.rule_thresholds)
            if signal.rule_id == "pre-pump-fusion"
            else {},
        },
    }


def lifecycle_decision(old, reference, regime, reason, now, *, context=None):
    return {
        "version": VERSION,
        "role": "LIFECYCLE",
        "as_of": now.isoformat(),
        "snapshot_id": reference.id,
        "market_time": reference.market_time.isoformat(),
        "parent_signal_id": old.id,
        "parent_decision_as_of": old.decision.get("as_of"),
        "reason": reason,
        "btc_risk_mode": regime.risk_mode,
        "original_invalidation_price": old.invalidation_price,
        "asset_context": deepcopy(context or {}),
    }


def semantic_signature(decision):
    """Ignore prices, timestamps and background updates to preserve quiet delivery."""
    return tuple((g["id"], g["status"]) for g in decision.get("gates", []))


def explanation_lines(rows):
    lines, seen = [], set()

    def add(line):
        if line not in seen:
            lines.append(line)
            seen.add(line)

    for row in rows:
        d = row.get("decision", {})
        if not d:
            add("决策链：旧记录未保存完整检查结果，无法确认全部规则的使用情况。")
            continue
        if d.get("role") == "LIFECYCLE":
            continue
        gates = d.get("gates", [])
        passed = [g["name"] for g in gates if g["status"] == "PASS"]
        blocked = [g["name"] for g in gates if g["status"] == "BLOCKED"]
        unknown = [g["name"] for g in gates if g["status"] in ("UNKNOWN", "DISABLED")]
        if passed:
            add(f"{row['rule_id']} 已通过：" + "、".join(passed) + "。")
        trigger = next((r for r in d.get("triggered", []) if r["rule_id"] == row["rule_id"]), {})
        metrics = trigger.get("inputs", {})
        facts = []
        for key, label, suffix in (
            ("funding_rate_pct", "Funding", "%"),
            ("oi_change_5m_pct", "OI 5 分钟变化", "%"),
            ("spread_bps", "价差", " bps"),
            ("relative_strength_15m_pct", "15 分钟相对 BTC 强度", "%"),
        ):
            value = metrics.get(key)
            if value is not None:
                facts.append(f"{label} {value:.3g}{suffix}")
        if facts:
            add("参与判断的数据：" + "；".join(facts) + "。")
        if blocked:
            add(f"{row['rule_id']} 被限制：" + "、".join(blocked) + "。")
        if unknown:
            add("未全面核实：" + "、".join(unknown) + "；没有已知阻断不能视作完整通过。")
        quality = d.get("entry_quality", {})
        if quality.get("enabled"):
            add(
                "入场质量："
                + quality["status"]
                + f"；往返成本假设 {quality['round_trip_cost_bps']:g} bps"
                + f"；短期追涨上限 {quality['max_chase_pct']:g}%。"
            )
        market = d.get("market_confirmation", {})
        if market:
            breadth = market.get("support_breadth_pct")
            add(
                "市场买盘确认："
                + market["status"]
                + f"；有效覆盖 {market['eligible_assets']}/{market['expected_assets']} 个其他币种"
                + (f"，买盘改善占比 {breadth:.0f}%" if breadth is not None else "")
                + (
                    "；当前用于研究对照。"
                    if market["mode"] == "observe"
                    else "；当前参与入场门控。"
                )
            )
        for item in d.get("asset_context", {}).get("evaluations", []):
            if item["kind"] == "tokenomics":
                add("代币经济检查使用已录入观测；尚无自动过期策略，记录时点见决策详情。")
        own = [g for g in d.get("data_gaps", []) if g["scope"] != "OTHER_RULE"]
        if own:
            add(
                "缺失/不可用："
                + "、".join(
                    dict.fromkeys(
                        CONTEXT_NAMES.get(g["field"], g["field"]) + "（" + g["status"] + "）"
                        for g in own
                    )
                )
                + "。"
            )
        other = sorted({g["rule_id"] for g in d.get("data_gaps", []) if g["scope"] == "OTHER_RULE"})
        if other:
            add("其他规则未能完整核实：" + "、".join(other) + "；不计作确认。")
        btc = d.get("btc_regime", {})
        if gates:
            add(
                "BTC 背景："
                + btc.get("risk_mode", "UNKNOWN")
                + "，宏观/ETF 联合压力检查="
                + btc.get("macro", "UNKNOWN")
                + "；"
                + "；".join(btc.get("evidence", []))
                + "。"
            )
        for item in d.get("background", []):
            if (
                item.get("role") == "BACKGROUND_ONLY"
                and "inputs" in item
                and item["status"] != "MISSING_DATA"
            ):
                add(
                    "仅作研究背景："
                    + item["name"]
                    + "（"
                    + "；".join(f"{INPUT_NAMES.get(k, k)}={v}" for k, v in item["inputs"].items())
                    + "）。"
                )
        summary = [
            f"{item['name']}={item['status']}"
            for item in d.get("background", [])
            if item.get("role") in ("BACKGROUND_ONLY", "BACKGROUND") and "inputs" not in item
        ]
        if summary:
            add("背景覆盖（未用于短期触发）：" + "、".join(summary) + "。")
        if not d.get("background"):
            add("研究背景：未传入可用评估或未启用，不计作确认。")
        publication = d.get("publication", {})
        if publication.get("reason"):
            add("发布检查：" + publication["reason"] + "。")
        elif publication.get("confirmation_status") == "CONFIRMED":
            add(
                "pre-pump 确认检查：通过，首次发现后涨幅 "
                f"{publication['gain_since_first_seen_pct']:.2f}%"
                f"，追涨上限 {publication['max_chase_pct']:.2f}%；发送时仍需复核时限与价格。"
            )
        coverage = d.get("coverage", {})
        add(
            f"{row['rule_id']} 输入覆盖：{coverage.get('usable_inputs', 0)}"
            f"/{coverage.get('required_inputs', 0)}；非胜率，子模式共享数据。"
        )
        for limitation in d.get("limitations", []):
            if "旧式快照" in limitation:
                add(limitation + "。")
        for item in d.get("not_integrated", []):
            add("未参与：" + item + "。")
    return lines
