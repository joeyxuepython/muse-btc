"""Point-in-time BTC context hypotheses, archived only as WATCH observations."""

import math
import statistics
from datetime import datetime, timedelta

from .models import Module, Signal, SignalKind
from .rules import usable
from .strategy_audit import record

RULES = (
    "btc-macro-stress",
    "btc-mvrv-elevated",
    "btc-holder-loss",
    "btc-options-volatility",
)


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def daily_series(rows, source, metric, now):
    days = {}
    for row in sorted(rows, key=lambda r: r.available_at):
        value = row.data.get("value")
        if (
            row.source == source
            and row.data.get("metric") == metric
            and row.market_time <= now
            and row.available_at <= now
            and number(value)
            and value > 0
        ):
            days[row.market_time.date()] = row
    return [days[day] for day in sorted(days)]


def mvrv_window(rows, now):
    history = daily_series(rows, "Coin Metrics", "mvrv", now)[-365:]
    if len(history) < 30 or now - history[-1].market_time > timedelta(days=3):
        return history, None
    value = history[-1].data["value"]
    return history, sum(r.data["value"] <= value for r in history) / len(history) * 100


def option_sample(options, now, settings):
    if (
        not options
        or not 0
        <= (now - options[0].market_time).total_seconds()
        <= settings.btc_options_refresh_seconds * 2
    ):
        return [], None
    rows = []
    for row in options[0].data.get("chain", []):
        try:
            expiry = datetime.fromisoformat(row["expiry"])
            days = (expiry - now).total_seconds() / 86400
            iv, strike, underlying = (
                row.get(k) for k in ("mark_iv_pct", "strike_usd", "underlying_price_usd")
            )
            if (
                7 <= days <= 30
                and all(number(v) and v > 0 for v in (iv, strike, underlying))
                and abs(strike / underlying - 1) <= 0.05
                and row.get("option_type") in ("call", "put")
            ):
                rows.append(row)
        except (ValueError, TypeError, KeyError):
            continue
    # Both sides and more than a single pair; no full-chain median or direction inference.
    iv = (
        statistics.median(r["mark_iv_pct"] for r in rows)
        if len(rows) >= 4 and {r["option_type"] for r in rows} == {"call", "put"}
        else None
    )
    return rows, iv


def reference(row):
    data = row if isinstance(row, dict) else row.model_dump(mode="json")
    return {k: data.get(k) for k in ("id", "source", "market_time", "available_at")} | {
        "source_url": data["data"].get("source_url"),
    }


def observations(onchain, options, macro, change, flow, now, settings, btc):
    result = []

    def add(rule, title, inputs, matched, refs, limitation, horizon=86400, source_samples=None):
        result.append(
            {
                "rule_id": rule,
                "title": title,
                "inputs": inputs,
                "status": "MISSING_DATA"
                if any(v is None for v in inputs.values())
                else "TRIGGERED_WATCH"
                if matched
                else "NOT_TRIGGERED",
                "sources": [
                    reference(r) for r in (source_samples if source_samples is not None else refs)
                ],
                "record_ids": [r["id"] if isinstance(r, dict) else r.id for r in refs],
                "limitations": limitation,
                "horizon_seconds": horizon,
                "validation_status": "OBSERVATION_ONLY",
                "direction": "RISK_CONTEXT",
            }
        )

    macro_rows = [
        r for r in macro["series"] if r["key"] in {"WALCL", "WTREGEN", "RRPONTSYD"}
    ] + macro["etf"][-5:]
    add(
        RULES[0],
        "宏观流动性与 ETF 同向收缩",
        {"net_liquidity_change_usd": change, "five_observation_etf_flow_usd": flow},
        change is not None and flow is not None and change < 0 and flow < 0,
        macro_rows,
        [
            "不同发布频率与 ETF 延迟；代理变量不能证明 BTC 因果关系",
            "沿用已有 CAUTION 门控，观察本身不自动升级",
        ],
    )
    history, percentile = mvrv_window(onchain, now)
    add(
        RULES[1],
        "BTC 链上估值处于近期历史高分位",
        {
            "mvrv_percentile": percentile,
            "threshold_percentile": settings.context_mvrv_percentile,
            "unique_daily_points": len(history),
        },
        percentile is not None and percentile >= settings.context_mvrv_percentile,
        history,
        [
            "使用至多 365 个已知日样本；高分位不等于顶部",
            "阈值尚未回测校准，样本不足 30 天或过期则不判断",
        ],
        source_samples=history[-1:],
    )
    sopr = daily_series(onchain, "BGeometrics", "sopr", now)
    basis = daily_series(onchain, "BGeometrics", "sth-realized-price", now)
    recent_sopr = sopr[-1] if sopr and now - sopr[-1].market_time <= timedelta(days=10) else None
    recent_basis = (
        basis[-1] if basis and now - basis[-1].market_time <= timedelta(days=10) else None
    )
    # Compare observations from the same day, despite a seven-day free-source delay.
    aligned = (
        recent_sopr
        and recent_basis
        and recent_sopr.market_time.date() == recent_basis.market_time.date()
    )
    price = btc.price if btc and usable(btc, now, settings) else None
    values = {
        "sopr": recent_sopr.data["value"] if aligned else None,
        "sth_realized_price_usd": recent_basis.data["value"] if aligned else None,
        "current_btc_price_usd": price,
    }
    add(
        RULES[2],
        "历史 SOPR 亏损与当前短持成本压力",
        values,
        all(v is not None for v in values.values())
        and values["sopr"] < 1
        and price < values["sth_realized_price_usd"],
        [r for r in (recent_sopr, recent_basis) if r],
        [
            "免费数据约延迟 7 天；历史 SOPR 与当前价格不是同日市场状态",
            "只作日级风险观察，不判断当前卖盘或入场时机",
        ],
    )
    sample, iv = option_sample(options, now, settings)
    add(
        RULES[3],
        "BTC 近月平值期权波动率偏高",
        {
            "median_atm_iv_pct": iv,
            "threshold_iv_pct": settings.context_options_iv_pct,
            "contracts": len(sample),
        },
        iv is not None and iv >= settings.context_options_iv_pct,
        options[:1] if iv is not None else [],
        [
            "Deribit 单市场、7–30 天到期、行权价距标的 5% 内且至少 4 项含看涨及看跌",
            "固定 IV 阈值未校准；波动率不代表价格方向，也不表示完整头寸",
        ],
        14400,
    )
    return result


def context_signals(snapshot, regime, now, settings, trace=None):
    if snapshot.module != Module.BTC:
        return []
    enabled = settings.enable_intelligence and settings.enable_context_observations
    context = {r["rule_id"]: r for r in regime.research_context.get("observations", [])}
    signals = []
    for rule in RULES:
        item = context.get(rule)
        status = (
            "DISABLED"
            if not enabled
            else "MISSING_DATA"
            if not usable(snapshot, now, settings) or not item
            else item["status"]
        )
        signal = None
        if status == "TRIGGERED_WATCH":
            signal = Signal(
                asset_id=snapshot.asset_id,
                symbol=snapshot.symbol,
                module=snapshot.module,
                kind=SignalKind.WATCH,
                rule_id=rule,
                rule_version="btc-observations-v1",
                emitted_at=now,
                snapshot_id=snapshot.id,
                btc_snapshot_id=snapshot.id,
                title=item["title"],
                evidence=[f"{k}: {v}" for k, v in item["inputs"].items()],
                contradictions=item["limitations"],
                evidence_groups=["btc_context"],
                evidence_score=0,
                reference_price=snapshot.price,
                expires_at=now + timedelta(seconds=item["horizon_seconds"]),
                horizon_seconds=item["horizon_seconds"],
                model_version="context-observation",
                context=item,
            )
            signals.append(signal)
        record(
            trace,
            snapshot,
            rule,
            status=status,
            signal=signal,
            inputs=item["inputs"] if item else {},
            reasons=item["limitations"] if item else ["上下文观察未启用或无可用证据"],
            role="CONTEXT_OBSERVATION",
        )
    return signals
