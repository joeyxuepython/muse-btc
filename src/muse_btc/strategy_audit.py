"""Latest per-asset evaluations plus compact, immutable collection-run summaries."""

from collections import Counter, defaultdict
from datetime import datetime

from .models import new_id

BASIC_INPUTS = {
    "leverage-overheat": ("funding_rate_pct", "oi_change_5m_pct"),
    "spot-sell-pressure": ("return_15m_pct", "spot_taker_buy_ratio"),
    "squeeze-candidate": ("return_5m_pct", "oi_change_5m_pct", "funding_rate_pct"),
    "spot-led-momentum": (
        "return_5m_pct",
        "relative_volume",
        "spot_taker_buy_ratio",
        "ema20",
        "ema50",
        "relative_strength_15m_pct",
        "funding_rate_pct",
        "oi_change_5m_pct",
        "spread_bps",
    ),
}


def record(
    trace, snapshot, rule, *, status=None, signal=None, inputs=None, reasons=(), role="RULE"
):
    if trace is None:
        return
    inputs = inputs or {}
    missing = [k for k, v in inputs.items() if v is None]
    trace.append(
        {
            "asset_id": snapshot.asset_id,
            "symbol": snapshot.symbol,
            "snapshot_id": snapshot.id,
            "rule_id": rule,
            "role": role,
            "status": status
            or (
                "TRIGGERED_" + signal.kind
                if signal
                else "MISSING_DATA"
                if missing
                else "NOT_TRIGGERED"
            ),
            "inputs": inputs,
            "missing": missing,
            "reasons": list(reasons)
            or (
                signal.contradictions
                if signal
                else ["缺少或过期的必要数据"]
                if missing
                else ["条件组合未满足"]
            ),
            "rule_version": signal.rule_version if signal else None,
            "signal_id": signal.id if signal else None,
            "alert_id": None,
            "notification_id": None,
            "publication": "NOT_PUBLISHED",
            "delivery": "NOT_QUEUED",
        }
    )


def attach_publication(trace, signal, alert, *, cooldown=False, regime=None):
    queued = alert["level"] in ("STRONG", "CRITICAL_RISK") or (
        alert.get("notification_class") == "CANCELLATION" and alert.get("parent_had_strong_notice")
    )
    for row in trace:
        if row["rule_id"] != signal.rule_id:
            continue
        row.update(
            signal_id=signal.id,
            alert_id=alert["id"],
            publication=alert["level"],
            published_level=alert["level"],
            cooldown=cooldown,
            publication_reason=alert.get("confirmation_reason"),
            notification_id=alert.get("notification_id") if queued else None,
            delivery="AWAITING_RECEIPT" if queued else "BELOW_DELIVERY_LEVEL",
        )
        if alert.get("requested_level") == "STRONG" and alert["level"] != "STRONG":
            row["publication"] = "BLOCKED_CONFIRMATION"
        elif (
            regime
            and regime.risk_mode != "NORMAL"
            and signal.kind == "WATCH"
            and signal.rule_id in ("spot-led-momentum", "pre-pump-fusion")
        ):
            row["publication"] = "BLOCKED_RISK"
            row["publication_reason"] = "BTC 风险背景为 " + regime.risk_mode


def save_run(store, rows, now):
    batch = new_id()
    counts = defaultdict(Counter)
    for row in rows:
        row.update(batch_id=batch, as_of=now.isoformat())
        counts[row["rule_id"]][row["status"]] += 1
        if row["publication"].startswith("BLOCKED_"):
            counts[row["rule_id"]][row["publication"]] += 1
    summary = {
        "batch_id": batch,
        "as_of": now.isoformat(),
        "assets_evaluated": len({r["asset_id"] for r in rows if r["role"] != "LIFECYCLE"}),
        "lifecycle_events": sum(r["role"] == "LIFECYCLE" for r in rows),
        "rules": {k: dict(v) for k, v in counts.items()},
    }
    store.save_strategy_run(summary, rows)
    return summary


def report(store, settings, now, *, asset_id=None, limit=100):
    run, rows = store.strategy_report(asset_id, limit)
    receipts = store.notification_receipts(
        [r["notification_id"] for r in rows if r.get("notification_id")]
    )
    for row in rows:
        row["stale"] = (
            not run
            or row["batch_id"] != run["batch_id"]
            or (now - datetime.fromisoformat(row["as_of"])).total_seconds()
            > settings.poll_seconds * 2
        )
        receipt = receipts.get(row.get("notification_id"))
        row["delivery"] = receipt["status"] if receipt else row["delivery"]
        if row["stale"]:
            row["last_status"] = row["status"]
            row["status"] = "NOT_EVALUATED_IN_CURRENT_BATCH"
    return {
        "last_run": run,
        "evaluations": rows,
        "fusion_enabled": settings.enable_intelligence,
        "research_observations_enabled": settings.enable_intelligence
        and settings.enable_context_observations,
        "limitations": [
            "逐资产保存最近一次评估；批次汇总保留历史，原始证据及信号独立归档",
            "发送状态来自 Muse 回执，不是独立验证邮件送达；无回执保持未知",
            "PATTERN 为同一融合规则的子模式，不能当作独立策略票数",
        ],
    }
