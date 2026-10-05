"""One point-in-time rule entrypoint shared by live collection and replay."""

from .context import asset_context
from .context_observations import context_signals
from .decision_explanation import explain
from .entry_quality import ENTRY_RULES, apply_quality, quality_contexts
from .fusion import pre_pump_signals
from .intelligence import IntelligenceStore
from .models import Module, SignalKind
from .rules import RULE_VERSION, evaluate
from .strategy_audit import record

DECISION_FIELDS = (
    "enable_intelligence",
    "enable_context_observations",
    "context_mvrv_percentile",
    "context_options_iv_pct",
    "stale_seconds",
    "statistics_stale_seconds",
    "time_alignment_seconds",
    "rule_thresholds",
    "threshold_version",
    "alert_cooldown_seconds",
    "poll_seconds",
    "fee_bps_each_way",
    "slippage_bps_each_way",
    "macro_series",
    "macro_stale_days",
    "macro_series_max_age_days",
    "intelligence_refresh_seconds",
    "btc_options_refresh_seconds",
    "enable_entry_quality",
    "entry_confirmation_seconds",
    "entry_min_depth_usdt",
    "entry_max_chase_pct",
    "market_confirmation_mode",
    "market_confirmation_min_assets",
    "market_confirmation_min_coverage",
)


def decision_config(settings):
    return {key: getattr(settings, key) for key in DECISION_FIELDS}


def decide(snapshot, regime, now, settings, store, trace=None, *, context=None, quality=None):
    trace = trace if trace is not None else []
    start = len(trace)
    signals = evaluate(snapshot, regime, now, settings, trace)
    if not settings.enable_intelligence:
        context = {}
    elif context is None or context.get("as_of") != now.isoformat():
        context = (
            asset_context(IntelligenceStore(store), snapshot.asset_id, now)
            if settings.enable_intelligence and snapshot.module != Module.MEME
            else {}
        )
    if settings.enable_intelligence:
        signals += pre_pump_signals(snapshot, regime, now, settings, store, trace, context=context)
    else:
        record(trace, snapshot, "pre-pump-fusion", status="DISABLED", reasons=["扩展规则未启用"])
    signals += context_signals(snapshot, regime, now, settings, trace)
    for signal in signals:
        if signal.rule_id == "spot-led-momentum" and context.get("risks"):
            signal.kind = SignalKind.WATCH
            signal.entry_zone = None
            signal.evidence_score = 48
            signal.title = "现货动量：资产风险限制，继续观察"
            signal.contradictions.extend(context["risks"])
            for row in trace[start:]:
                if row["rule_id"] == signal.rule_id:
                    row.update(status="TRIGGERED_WATCH", reasons=signal.contradictions)
    if any(s.rule_id in ENTRY_RULES for s in signals):
        if (
            quality is None
            or quality["entry_quality"]["as_of"] != now.isoformat()
            or quality["entry_quality"]["snapshot_ids"][0] != snapshot.id
        ):
            quality = quality_contexts(store, now, settings, [snapshot])[snapshot.asset_id]
        apply_quality(signals, quality, trace[start:], settings)
    if trace is not None:
        for row in trace[start:]:
            if row["rule_version"] is None:
                row["rule_version"] = (
                    "btc-observations-v1"
                    if row["role"] == "CONTEXT_OBSERVATION"
                    else "fusion-v4-4:" + settings.threshold_version
                    if row["rule_id"].startswith("pre-pump")
                    else RULE_VERSION
                )
    for signal in signals:
        signal.decision = explain(signal, snapshot, regime, trace[start:], context, now, settings)
        if quality and signal.rule_id in ENTRY_RULES:
            signal.decision.update(quality)
    return signals
