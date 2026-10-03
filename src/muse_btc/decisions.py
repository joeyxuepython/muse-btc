"""One point-in-time rule entrypoint shared by live collection and replay."""

from .fusion import pre_pump_signals
from .rules import evaluate

DECISION_FIELDS = (
    "enable_intelligence",
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
)


def decision_config(settings):
    return {key: getattr(settings, key) for key in DECISION_FIELDS}


def decide(snapshot, regime, now, settings, store):
    signals = evaluate(snapshot, regime, now, settings)
    if settings.enable_intelligence:
        signals += pre_pump_signals(snapshot, regime, now, settings, store)
    return signals
