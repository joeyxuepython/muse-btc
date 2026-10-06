import json
from datetime import timedelta

import pytest
from conftest import snapshot
from fastapi.testclient import TestClient
from test_alert_delivery import signal_for

from muse_btc.api import create_app
from muse_btc.context_observations import RULES as CONTEXT_RULES
from muse_btc.evaluation_policy import (
    RULE_EVALUATIONS,
    declared_evaluation,
    evaluation_id,
)
from muse_btc.models import Outcome, Regime, SignalEvaluation, SignalKind
from muse_btc.performance import build_report
from muse_btc.rules import _signal
from muse_btc.service import Collector
from muse_btc.storage import Store, stamp
from muse_btc.strategy_audit import BASIC_INPUTS
from muse_btc.validation import (
    measure_signal,
    validate_pending,
    validation_batch,
    validation_report,
)


def rule_signal(store, now, rule, kind=SignalKind.ENTRY_CANDIDATE):
    snap = snapshot(now)
    store.save_snapshot(snap)
    signal = _signal(
        snap, Regime(as_of=now, risk_mode="NORMAL"), now, kind, rule, "test", [], ["price"], 70
    )
    store.save_signal(signal)
    return signal


def test_every_production_rule_declares_measurement_and_primary():
    assert set(RULE_EVALUATIONS) == set(BASIC_INPUTS) | set(CONTEXT_RULES) | {"pre-pump-fusion"}
    with pytest.raises(KeyError):
        declared_evaluation("undeclared-new-strategy")
    with pytest.raises(ValueError, match="Primary"):
        SignalEvaluation(
            metric="LONG_RETURN",
            horizons_seconds=(300,),
            primary_horizon_seconds=900,
            rationale="invalid primary",
        )


def test_new_signals_cannot_silently_use_a_generic_plan(store, now):
    snap = snapshot(now)
    store.save_snapshot(snap)
    signal = signal_for(snap, now)
    signal.evaluation = None
    with pytest.raises(ValueError, match="must declare"):
        store.save_signal(signal)
    assert store.signals() == []


def test_pre_pump_has_only_declared_windows_and_keeps_four_hour_lifecycle(store, now):
    signal = rule_signal(store, now, "pre-pump-fusion")
    with store.connect() as db:
        periods = [
            r[0]
            for r in db.execute(
                "SELECT horizon_seconds FROM validation_jobs ORDER BY horizon_seconds"
            )
        ]
    assert periods == [300, 900]
    assert signal.evaluation.primary_horizon_seconds == 900
    assert signal.horizon_seconds == 3600  # Existing notification grouping is preserved.
    assert signal.expires_at == now + timedelta(hours=4)
    assert Store(store.path).signal(signal.id).evaluation == signal.evaluation


def test_undeclared_horizon_is_rejected_before_any_price_read(store, settings, now, monkeypatch):
    signal = rule_signal(store, now, "pre-pump-fusion")

    def forbidden(*args):
        raise AssertionError("Out-of-plan horizons must never query prices")

    monkeypatch.setattr(store, "quote_range", forbidden)
    with pytest.raises(ValueError, match="not declared"):
        measure_signal(store, signal, 2592000, now + timedelta(days=31), settings)


def test_negative_early_checkpoint_does_not_invalidate_entry(store, settings, now):
    signal = rule_signal(store, now, "pre-pump-fusion")
    for minute in (2, 4, 5):
        store.save_snapshot(snapshot(now + timedelta(minutes=minute), price=99.9))
    at = now + timedelta(minutes=5)
    assert validate_pending(store, at, settings) == 1
    Collector(settings, store, None)._process_signals(
        store.latest_snapshots(at), Regime(as_of=at, risk_mode="NORMAL"), at
    )
    assert store.outcomes()[0].paper_net_return_pct < 0
    assert store.signal_events(signal.id)[-1].state == "ACTIVE"
    rows = [
        r
        for r in validation_report(store, settings, at)["rules"]
        if r["rule_id"] == "pre-pump-fusion"
    ]
    assert {r["horizon_seconds"]: r["horizon_role"] for r in rows} == {
        300: "AUXILIARY",
        900: "PRIMARY",
    }
    assert next(r for r in rows if r["horizon_role"] == "PRIMARY")["pending_count"] == 1


def test_auxiliary_win_cannot_replace_missing_primary(store, settings, now):
    signal = rule_signal(store, now, "pre-pump-fusion")
    for minute in (2, 4, 5):
        store.save_snapshot(snapshot(now + timedelta(minutes=minute), price=103))
    validate_pending(store, now + timedelta(minutes=20), settings)
    rows = validation_report(store, settings, now + timedelta(minutes=20))["rules"]
    assert rows[0]["horizon_role"] == "AUXILIARY" and rows[0]["mean_paper_net_return_pct"] > 0
    assert rows[1]["horizon_role"] == "PRIMARY" and rows[1]["missing_outcome_count"] == 1
    assert rows[1]["mean_paper_net_return_pct"] is None
    assert not validation_report(store, settings, now + timedelta(minutes=20))[
        "production_promotion"
    ]
    assert store.signal_events(signal.id)[-1].state == "ACTIVE"


def test_risk_alert_measures_decline_and_rebound_without_hold_pnl(store, settings, now):
    signal = rule_signal(store, now, "spot-sell-pressure", SignalKind.RISK)
    for minute in (2, 4, 6, 8, 10, 12, 14, 15):
        store.save_snapshot(
            snapshot(now + timedelta(minutes=minute), price=98 if minute == 2 else 101)
        )
    outcome = measure_signal(store, signal, 900, now + timedelta(minutes=15), settings)
    assert outcome.risk_terminal_decline is False and outcome.risk_window_decline is True
    assert outcome.risk_max_decline_pct == pytest.approx(2)
    assert outcome.risk_max_rebound_pct == pytest.approx(1)
    assert outcome.paper_net_return_pct is outcome.round_trip_cost_bps is None
    assert outcome.btc_return_pct is outcome.excess_return_pct is None
    row = build_report([signal], [outcome], settings, now + timedelta(minutes=16))["rules"][0]
    assert row["evaluation_metric"] == "RISK_DIRECTION"
    assert row["risk_terminal_decline_rate"] == 0 and row["risk_window_decline_rate"] == 1
    assert row["mean_paper_net_return_pct"] is row["paper_positive_rate"] is None
    assert row["mean_double_cost_return_pct"] is None


def test_volatility_observation_is_not_direction_or_trade_pnl(store, settings, now):
    signal = rule_signal(store, now, "btc-options-volatility", SignalKind.WATCH)
    store.save_snapshot(snapshot(now + timedelta(hours=4), price=96))
    result = measure_signal(store, signal, 14400, now + timedelta(hours=4), settings)
    assert result.evaluation_metric == "VOLATILITY"
    assert result.observed_range_pct == result.absolute_end_change_pct == pytest.approx(4)
    assert result.risk_terminal_decline is result.paper_net_return_pct is None


def test_rule_definition_change_cannot_rewrite_archived_plan(store, settings, now, monkeypatch):
    signal = rule_signal(store, now, "pre-pump-fusion")
    archived_id = evaluation_id(signal.evaluation)
    changed = signal.evaluation.model_copy(update={"horizons_seconds": (900, 3600)})
    monkeypatch.setitem(RULE_EVALUATIONS, "pre-pump-fusion", changed)
    restarted = Store(store.path)
    restored = restarted.signal(signal.id)
    assert restored.evaluation.horizons_seconds == (300, 900)
    assert evaluation_id(restored.evaluation) == archived_id
    rows = build_report([restored], [], settings, now)["rules"]
    assert [r["horizon_seconds"] for r in rows] == [300, 900]


def test_legacy_queue_is_retired_incrementally_without_rewriting_negative_results(
    store, settings, now, monkeypatch
):
    snap = snapshot(now)
    old = signal_for(snap, now)
    payload = old.model_dump(mode="json")
    payload.pop("evaluation")
    old_result = Outcome(
        signal_id=old.id,
        horizon_seconds=300,
        evaluated_at=now + timedelta(minutes=5),
        measured_at=now + timedelta(minutes=5),
        end_snapshot_id="old-end",
        return_pct=-1,
        max_favorable_pct=0,
        max_adverse_pct=-1,
        paper_net_return_pct=-1.3,
        round_trip_cost_bps=30,
        sample_count=3,
        max_observation_gap_seconds=120,
    ).model_dump_json()
    legacy_periods = (300, 900, 3600, 14400, 86400, 259200, 604800, 1209600, 2592000)
    with store.connect() as db:
        db.execute(
            "INSERT INTO signals VALUES (?,?,?,?,?,?)",
            (old.id, old.asset_id, old.rule_id, old.kind, stamp(now), json.dumps(payload)),
        )
        db.execute("INSERT INTO outcomes VALUES (?,?,?)", (old.id, 300, old_result))
        db.executemany(
            "INSERT INTO validation_jobs VALUES (?,?,?,?,'PENDING',NULL)",
            [
                (old.id, h, stamp(now + timedelta(seconds=h)), stamp(now + timedelta(seconds=h)))
                for h in legacy_periods
            ],
        )
    store.set_state("validation_seed_cursor", 1000)  # The previous worker completed its backfill.

    def forbidden(*args):
        raise AssertionError("Retired legacy windows must not read history")

    monkeypatch.setattr(store, "quote_range", forbidden)
    assert validation_batch(store, now + timedelta(minutes=20), settings)["attempted"] == 0
    assert store.validation_queue(now)["counts"] == {"PENDING": 1, "RETIRED_POLICY": 8}
    with store.connect() as db:
        assert db.execute("SELECT payload FROM outcomes").fetchone()[0] == old_result
    report = validation_report(store, settings, now + timedelta(minutes=20))
    assert report["version"] == "validation-v3" and report["rules"] == []
    old_row = next(r for r in report["legacy_rules"] if r["horizon_seconds"] == 300)
    assert old_row["mean_paper_net_return_pct"] == pytest.approx(-1.3)
    assert old_row["horizon_role"] == "LEGACY_OBSERVATION"


def test_out_of_plan_results_remain_archived_but_cannot_fill_primary(store, settings, now):
    signal = rule_signal(store, now, "pre-pump-fusion")
    wrong = Outcome(
        signal_id=signal.id,
        horizon_seconds=3600,
        evaluated_at=now + timedelta(hours=1),
        measured_at=now + timedelta(hours=1),
        end_snapshot_id="wrong-window",
        return_pct=10,
        max_favorable_pct=10,
        max_adverse_pct=0,
        paper_net_return_pct=9.7,
        round_trip_cost_bps=30,
        sample_count=30,
        max_observation_gap_seconds=120,
        evaluation_policy_id=evaluation_id(signal.evaluation),
        evaluation_metric="LONG_RETURN",
    )
    store.save_outcome(wrong)
    report = validation_report(store, settings, now + timedelta(hours=2))
    assert [r["horizon_seconds"] for r in report["rules"]] == [300, 900]
    assert all(r["missing_outcome_count"] == 1 for r in report["rules"])
    assert report["out_of_plan_outcome_count"] == 1
    assert store.outcomes()[0] == wrong


def test_wrong_metric_cannot_fill_a_declared_period(store, settings, now):
    signal = rule_signal(store, now, "spot-sell-pressure", SignalKind.RISK)
    wrong = Outcome(
        signal_id=signal.id,
        horizon_seconds=900,
        evaluated_at=now + timedelta(minutes=15),
        measured_at=now + timedelta(minutes=15),
        end_snapshot_id="wrong-metric",
        return_pct=-2,
        max_favorable_pct=0,
        max_adverse_pct=-2,
        sample_count=8,
        max_observation_gap_seconds=120,
        evaluation_policy_id=evaluation_id(signal.evaluation),
        evaluation_metric="LONG_RETURN",
    )
    report = build_report([signal], [wrong], settings, now + timedelta(minutes=16))
    assert report["out_of_plan_outcome_count"] == 1
    assert report["rules"][0]["missing_outcome_count"] == 1
    assert report["rules"][0]["risk_terminal_decline_rate"] is None


def test_api_exposes_archived_plan_and_auxiliary_result_without_invalidation(
    store, settings, now, monkeypatch
):
    signal = rule_signal(store, now, "pre-pump-fusion")
    for minute in (2, 4, 5):
        store.save_snapshot(snapshot(now + timedelta(minutes=minute), price=99.9))
    at = now + timedelta(minutes=5)
    validate_pending(store, at, settings)
    monkeypatch.setattr("muse_btc.api.utc_now", lambda: at)
    monkeypatch.setattr("muse_btc.validation.utc_now", lambda: at)
    with TestClient(create_app(settings)) as client:
        detail = client.get("/api/signals/" + signal.id).json()
        report = client.get("/api/validation").json()
    assert detail["signal"]["evaluation"]["horizons_seconds"] == [300, 900]
    assert detail["signal"]["evaluation_policy_id"] == evaluation_id(signal.evaluation)
    assert detail["signal"]["lifecycle_state"] == "ACTIVE"
    assert "独立" in detail["signal"]["evaluation_lifecycle_note"]
    assert detail["outcomes"][0]["paper_net_return_pct"] < 0
    assert report["version"] == "validation-v3" and report["legacy_rules"] == []
    assert {r["horizon_seconds"]: r["horizon_role"] for r in report["rules"]} == {
        300: "AUXILIARY",
        900: "PRIMARY",
    }
