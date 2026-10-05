"""Explainable BTC evidence context; scores are uncalibrated, never probabilities."""

from datetime import datetime, timedelta

from .context_observations import mvrv_window, observations, option_sample
from .intelligence import IntelligenceStore
from .macro import MacroEngine
from .rules import component_usable, usable


def btc_assessment(store, settings, now, btc=None):
    archive = IntelligenceStore(store)
    if btc is None:
        btc = next(
            (s for s in store.latest_snapshots(now) if s.asset_id == "binance:BTCUSDT"), None
        )
    dimensions = {}

    def add(name, score, status, evidence, ids=()):
        dimensions[name] = {
            "score": score,
            "status": status,
            "evidence": evidence,
            "record_ids": list(ids),
        }

    f = btc.features if btc and usable(btc, now, settings) else None
    spot = f.spot_taker_buy_ratio if f else None
    add(
        "spot_demand",
        round(spot * 100, 1) if spot is not None else None,
        "AVAILABLE" if spot is not None else "MISSING",
        ["15 分钟主动买入占比"],
        [btc.id] if f else [],
    )
    lev = None
    if (
        f
        and component_usable(btc, "funding", now, settings)
        and component_usable(btc, "oi_history", now, settings)
        and f.funding_rate_pct is not None
        and f.oi_change_5m_pct is not None
    ):
        lev = min(
            100, max(0, abs(f.funding_rate_pct) / 0.05) * 50 + max(0, f.oi_change_5m_pct / 2) * 50
        )
    add(
        "leverage_risk",
        round(lev, 1) if lev is not None else None,
        "AVAILABLE" if lev is not None else "MISSING",
        ["绝对资金费率及 5 分钟 OI 增长，不能单独判断多空"],
        [btc.id] if lev is not None else [],
    )
    macro = MacroEngine(store, settings, None).summary(now)
    series = {r["key"]: r for r in macro["series"]}
    components, references = [], []
    for key, direction in (("WALCL", 1), ("WTREGEN", -1), ("RRPONTSYD", -1)):
        row = series.get(key)
        if row and key not in macro["stale"] and row["data"].get("previous") is not None:
            components.append(
                direction
                * (row["data"]["value"] - row["data"]["previous"])
                * (1000 if key == "RRPONTSYD" else 1)
            )
            references.append(row["id"])
    change = (
        sum(components)
        if len(components) == 3 and macro["net_liquidity_proxy_usd"] is not None
        else None
    )
    add(
        "macro_liquidity",
        75
        if change is not None and change > 0
        else 25
        if change is not None and change < 0
        else 50
        if change == 0
        else None,
        "PROXY" if change is not None else "MISSING",
        ["美联储资产减 TGA/RRP 的最近变化方向；发布频率不同，仅作代理"],
        references,
    )
    flows = [r for r in macro["etf"] if (now - datetime.fromisoformat(r["market_time"])).days <= 10]
    value = (
        sum(r["data"]["net_flow_usd"] for r in flows[-5:])
        if len(flows) >= 5 and (now - datetime.fromisoformat(flows[-1]["market_time"])).days <= 4
        else None
    )
    add(
        "etf_flow",
        75
        if value is not None and value > 0
        else 25
        if value is not None and value < 0
        else 50
        if value == 0
        else None,
        "AVAILABLE" if value is not None else "MISSING",
        [f"最近 5 个已公布观测净流量：{value if value is not None else '缺失'} 美元；存在发布滞后"],
        [r["id"] for r in flows[-5:]],
    )
    stable = macro["crypto_liquidity_index"]
    add(
        "stablecoin_supply",
        75
        if stable is not None and stable > 0
        else 25
        if stable is not None and stable < 0
        else 50
        if stable == 0
        else None,
        "PROXY" if stable is not None else "MISSING",
        ["四种稳定币 7 日供应变化；供应不等于买盘"],
        [r["id"] for r in macro["stablecoins"]],
    )
    onchain = archive.records("onchain", now, limit=100000)
    mvrv, percentile = mvrv_window(onchain, now)
    add(
        "onchain_valuation_percentile",
        round(percentile, 1) if percentile is not None else None,
        "DAILY_CONTEXT" if percentile is not None else "MISSING",
        ["MVRV 历史分位数；不直接作为入场或退出信号"],
        [mvrv[-1].id] if mvrv else [],
    )
    options = archive.records("options", now, limit=1)
    sample, iv = option_sample(options, now, settings)
    add(
        "options_iv",
        None,
        "AVAILABLE" if iv is not None else "MISSING",
        [
            f"Deribit 7–30 天平值 IV 中位数：{iv if iv is not None else '缺失'}；"
            f"有效样本 {len(sample)} 项，单市场快照"
        ],
        [options[0].id] if iv is not None else [],
    )
    current_documents = {r.id for r in archive.records("research", now)}
    reviews = [
        r
        for r in archive.records("research_review", now)
        if r.key in current_documents
        and r.data.get("document_id") in current_documents
        and "BTC" in r.data.get("assets", [])
        and now - r.market_time <= timedelta(days=30)
    ]
    directions = {r.data.get("direction") for r in reviews}
    add(
        "research_bias",
        None,
        "MIXED"
        if {"BULLISH", "BEARISH"} <= directions
        else next(iter(directions))
        if len(directions) == 1
        else "MIXED"
        if directions
        else "MISSING",
        ["近 30 天最新已审阅研究，不能覆盖实时市场风险"],
        [r.id for r in reviews],
    )
    macro_stress = change is not None and change < 0 and value is not None and value < 0
    observed = sum(r["status"] != "MISSING" for r in dimensions.values())
    return {
        "as_of": now.isoformat(),
        "version": "btc-context-v2",
        "observations": observations(onchain, options, macro, change, value, now, settings, btc),
        "dimensions": dimensions,
        "coverage_pct": round(observed / len(dimensions) * 100, 1),
        "confidence": "UNCALIBRATED",
        "macro_risk": "CAUTION"
        if macro_stress
        else "NO_COMBINED_STRESS"
        if change is not None and value is not None
        else "UNKNOWN",
        "short_term": "LEVERAGE_RISK"
        if lev is not None and lev >= 75
        else "DEMAND_SUPPORT"
        if spot is not None and spot >= 0.58
        else "MIXED"
        if f
        else "UNKNOWN",
        "limitations": [
            "Rules are research hypotheses; not calibrated probabilities",
            "Options and on-chain values are context, not automatic entry triggers",
            "Missing inputs remain missing",
        ],
    }


def apply_btc_context(regime, store, settings, now, btc):
    result = btc_assessment(store, settings, now, btc)
    regime.macro = result["macro_risk"]
    regime.evidence_coverage_pct = result["coverage_pct"]
    regime.scope = "BTC_SPOT_PERP_WITH_PUBLIC_CONTEXT"
    regime.research_context = {
        "version": result["version"],
        "as_of": result["as_of"],
        "observations": result["observations"],
    }
    regime.contradictions = [s for s in regime.contradictions if "尚未融合" not in s]
    regime.contradictions.append(f"BTC 综合证据覆盖 {result['coverage_pct']}%；置信度尚未校准")
    if result["macro_risk"] == "CAUTION":
        regime.evidence.append("公开流动性代理下降且近期 ETF 净流出，限制强机会提醒")
        if regime.risk_mode == "NORMAL":
            regime.risk_mode = "CAUTION"
    from .models import Regime
    from .storage import stamp

    with store.connect() as db:
        previous = db.execute(
            "SELECT payload FROM regimes WHERE as_of<? ORDER BY as_of DESC LIMIT 1", (stamp(now),)
        ).fetchone()
    old = Regime.model_validate_json(previous[0]) if previous else None
    regime.previous_risk_mode = old.risk_mode if old else None
    regime.regime_start_time = (
        (old.regime_start_time or old.as_of) if old and old.risk_mode == regime.risk_mode else now
    )
    regime.regime_duration_seconds = (now - regime.regime_start_time).total_seconds()
    return regime
