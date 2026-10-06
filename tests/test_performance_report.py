from datetime import timedelta

import pytest
from conftest import snapshot
from test_alert_delivery import signal_for as base_signal

from muse_btc.evaluation_policy import evaluation_id
from muse_btc.models import Outcome, SignalKind
from muse_btc.performance import build_report
from muse_btc.rules import evaluate, market_regime
from muse_btc.validation import measure_signal


def signal_for(*args, **kwargs):
    signal = base_signal(*args, **kwargs)
    signal.evaluation = signal.evaluation.model_copy(
        update={"horizons_seconds": (300,), "primary_horizon_seconds": 300}
    )
    return signal


def outcome(signal, *, change=1, horizon=300, gap=60):
    at = signal.emitted_at + timedelta(seconds=horizon)
    return Outcome(
        signal_id=signal.id,
        horizon_seconds=horizon,
        evaluated_at=at,
        measured_at=at,
        end_snapshot_id="end",
        return_pct=change,
        max_favorable_pct=max(0, change),
        max_adverse_pct=min(0, change),
        paper_net_return_pct=change - 0.3 if signal.kind == SignalKind.ENTRY_CANDIDATE else None,
        round_trip_cost_bps=30,
        sample_count=5,
        max_observation_gap_seconds=gap,
        max_allowed_gap_seconds=300,
        evaluation_policy_id=evaluation_id(signal.evaluation),
        evaluation_metric=signal.evaluation.metric,
    )


def test_report_separates_types_versions_and_preserves_missing(settings, now):
    signals = [
        signal_for(snapshot(now), now, kind=kind)
        for kind in (SignalKind.ENTRY_CANDIDATE, SignalKind.WATCH, SignalKind.RISK)
    ]
    later_version = signal_for(snapshot(now), now)
    later_version.rule_version = "new-version"
    signals.append(later_version)
    report = build_report(
        signals, [outcome(s) for s in signals[:3]], settings, now + timedelta(seconds=600)
    )
    assert len(report["rules"]) == 4
    for row in report["rules"]:
        if row["signal_kind"] != "ENTRY_CANDIDATE":
            assert row["paper_positive_rate"] is None
        if row["rule_version"] == "new-version":
            assert row["missing_outcome_count"] == 1
            assert row["paper_positive_rate"] is None
    assert not report["production_promotion"]


def test_overlap_selection_does_not_replace_missing_first_signal_with_winner(settings, now):
    first = signal_for(snapshot(now), now)
    second = signal_for(snapshot(now + timedelta(seconds=60)), now + timedelta(seconds=60))
    row = build_report(
        [first, second], [outcome(second, change=10)], settings, now + timedelta(seconds=600)
    )["rules"][0]
    assert row["covered_count"] == 1
    assert row["overlapping_count"] == 1
    assert row["nonoverlapping_count"] == 0
    assert row["selected_missing_or_gapped_count"] == 1
    assert row["paper_positive_rate"] is None


def test_cost_stress_and_intervals_are_not_fake_certainty(settings, now):
    first = signal_for(snapshot(now), now)
    row = build_report(
        [first], [outcome(first, change=0.4)], settings, now + timedelta(seconds=600)
    )["rules"][0]
    assert row["mean_paper_net_return_pct"] == pytest.approx(0.1)
    assert row["mean_double_cost_return_pct"] == pytest.approx(-0.2)
    assert row["paper_positive_rate"] == 1
    assert row["paper_positive_rate_wilson_95"][0] < 0.21
    assert row["evaluation_status"] == "INSUFFICIENT_SAMPLES"


def test_archived_coverage_threshold_and_future_labels(settings, now):
    first = signal_for(snapshot(now), now)
    first.evaluation = first.evaluation.model_copy(update={"horizons_seconds": (300, 900)})
    result = outcome(first, gap=200)
    settings.stale_seconds = 60
    rows = build_report([first], [result], settings, now + timedelta(seconds=600))["rules"]
    assert rows[0]["covered_count"] == 1  # Uses the measurement's recorded threshold.
    assert rows[1]["pending_count"] == 1
    result.evaluated_at = now + timedelta(seconds=1200)
    row = build_report([first], [result], settings, now + timedelta(seconds=600))["rules"][0]
    assert row["missing_outcome_count"] == 1


def test_market_cohorts_and_chronological_boundary_are_descriptive(settings, now):
    signals = []
    outcomes = []
    for i in range(10):
        at = now + timedelta(seconds=600 * i)
        s = signal_for(snapshot(at), at)
        s.decision = {
            "market_confirmation": {
                "status": "SUPPORTIVE" if i % 2 else "MIXED",
                "resonance": bool(i % 2),
            }
        }
        signals.append(s)
        outcomes.append(outcome(s, change=1 if i % 2 else -1))
    row = build_report(signals, outcomes, settings, now + timedelta(seconds=6000))["rules"][0]
    assert row["market_confirmation_cohorts"]["RESONANT"]["sample_count"] == 5
    assert row["market_confirmation_cohorts"]["NON_RESONANT"]["sample_count"] == 5
    assert row["chronological_diagnostic"]["later"]["sample_count"] == 2
    assert not row["chronological_diagnostic"]["is_untouched_out_of_sample"]


def test_repeated_and_out_of_order_quotes_do_not_fake_path_coverage(store, settings, now):
    base = snapshot(now)
    store.save_snapshot(base)
    signal = evaluate(base, market_regime(base, now, settings), now, settings)[0]
    # A late copy of an earlier market quote must not create negative time gaps,
    # extra observations or override the first available value at that quote time.
    for market_minute, receive_minute, price in [(2, 2, 101), (1, 3, 99), (2, 4, 200), (5, 5, 102)]:
        store.save_snapshot(
            snapshot(
                now + timedelta(minutes=receive_minute),
                market_time=now + timedelta(minutes=market_minute),
                price=price,
            )
        )
    result = measure_signal(store, signal, 300, now + timedelta(minutes=6), settings)
    assert result.sample_count == 3
    assert result.max_favorable_pct == pytest.approx(2)
    assert result.max_adverse_pct == pytest.approx(-1)
    assert result.max_observation_gap_seconds == 180
    assert result.time_to_mae_seconds == 60
