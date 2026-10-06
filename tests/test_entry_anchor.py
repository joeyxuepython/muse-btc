import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from test_entry_quality import rich, signal

from muse_btc.alerts import publish_alert
from muse_btc.api import create_app
from muse_btc.config import Settings
from muse_btc.decisions import decision_config
from muse_btc.delivery import notification_message
from muse_btc.entry_quality import asset_quality, inspect, quality_contexts
from muse_btc.models import SignalKind
from muse_btc.storage import Store, stamp
from muse_btc.validation import replay


def archive_gap(store, now, seconds=600):
    old = rich(now - timedelta(seconds=seconds), bid=300)
    new = rich(now)
    store.save_snapshot(old)
    store.save_snapshot(new)
    return old, new


def quality(store, now, settings):
    return quality_contexts(store, now, settings)["binance:BTCUSDT"]["entry_quality"]


def test_anchor_configuration_has_hard_separation_and_valid_bounds(monkeypatch):
    assert Settings(_env_file=None).entry_anchor_max_age_seconds == 1800
    with pytest.raises(ValueError):
        Settings(_env_file=None, entry_confirmation_seconds=30)
    with pytest.raises(ValueError, match="confirmation minimum"):
        Settings(_env_file=None, entry_confirmation_seconds=120, entry_anchor_max_age_seconds=60)
    monkeypatch.setenv("MUSE_ENTRY_ANCHOR_MAX_AGE_SECONDS", "900")
    assert Settings(_env_file=None).entry_anchor_max_age_seconds == 900


def test_reopened_database_reuses_pre_outage_anchor_without_extra_fresh_pair(store, settings, now):
    settings.enable_entry_quality = True
    old, current = archive_gap(store, now)
    restarted = Store(store.path)
    candidate = signal(restarted, settings, current, now)
    q = candidate.decision["entry_quality"]
    assert candidate.kind == SignalKind.ENTRY_CANDIDATE and q["status"] == "PASS"
    assert q["snapshot_ids"] == [current.id, old.id]
    assert q["anchor_snapshot_id"] == old.id and q["anchor_age_seconds"] == 600
    assert q["max_observation_gap_seconds"] == 600
    assert q["max_component_gap_seconds"] == {"book": 600, "candles": 600}
    assert q["has_observation_gap"] and q["observation_continuity"] == "GAPPED"
    assert q["maximum_age_seconds"] == 300 and q["anchor_max_age_seconds"] == 1800
    assert "entry-quality-v2" in candidate.rule_version


def test_newest_still_has_to_be_fresh_after_a_gap(store, settings, now):
    archive_gap(store, now)
    q = quality(store, now + timedelta(seconds=301), settings)
    assert q["status"] == "WAIT" and not q["checks"]["fresh_components"]
    assert "BOOK_STALE_OR_INVALID" in q["failure_codes"]
    assert not q["support_withdrawn"]


def test_historical_anchor_is_checked_at_receipt_instead_of_now(store, settings, now):
    old = rich(now - timedelta(seconds=240))
    current = rich(now)
    for component in ("book", "candles"):
        old.component_times[component] = now - timedelta(seconds=360)
        current.component_times[component] = now - timedelta(seconds=60)
    store.save_snapshot(old)
    store.save_snapshot(current)
    assert inspect(old, old.available_at, settings)["fresh_components"]
    assert not inspect(old, now, settings)["fresh_components"]
    assert quality(store, now, settings)["status"] == "PASS"


@pytest.mark.parametrize(
    "problem", ["stale", "misaligned", "future_source", "future_receipt", "bad_quality"]
)
def test_anchor_that_was_invalid_at_receipt_never_confirms(store, settings, now, problem):
    old = rich(now - timedelta(minutes=10))
    current = rich(now)
    if problem == "stale":
        for component in ("book", "candles"):
            old.component_times[component] -= timedelta(seconds=301)
    elif problem == "misaligned":
        old.component_times["book"] -= timedelta(seconds=180)
    elif problem == "future_source":
        old.component_times["book"] += timedelta(seconds=1)
    elif problem == "future_receipt":
        old.component_received_at["book"] = old.available_at + timedelta(seconds=1)
    else:
        old.quality_issues.append("INVALID_CANDLE_VALUES")
    store.save_snapshot(old)
    store.save_snapshot(current)
    q = quality(store, now, settings)
    assert q["status"] == "WAIT" and q["confirmation_state"] == "INPUTS_INVALID"
    assert q["anchor_snapshot_id"] == old.id
    assert "CONFIRMATION_INPUTS_INVALID" in q["failure_codes"]


@pytest.mark.parametrize("age,expected", [(1800, "PASS"), (1801, "WAIT")])
def test_anchor_age_boundary_is_inclusive_and_bounded(store, settings, now, age, expected):
    archive_gap(store, now, age)
    assert quality(store, now, settings)["status"] == expected


@pytest.mark.parametrize("separation,expected", [(59, "WAIT"), (60, "PASS")])
def test_both_source_times_keep_minimum_separation(store, settings, now, separation, expected):
    archive_gap(store, now, separation)
    assert quality(store, now, settings)["status"] == expected


@pytest.mark.parametrize("cached", ["book", "candles", "both"])
def test_repeated_source_time_cannot_confirm_itself(store, settings, now, cached):
    old = rich(now - timedelta(seconds=120))
    current = rich(now)
    for component in ("book", "candles"):
        if cached == component or cached == "both":
            current.component_times[component] = old.component_times[component]
    store.save_snapshot(old)
    store.save_snapshot(current)
    q = quality(store, now, settings)
    assert q["checks"]["fresh_components"]
    assert q["status"] == "WAIT" and q["confirmation_state"] == "INSUFFICIENT_OBSERVATIONS"


@pytest.mark.parametrize("problem", ["sell_flow", "thin_depth", "missing_flow", "invalid_input"])
def test_intervening_adverse_observations_are_not_skipped(store, settings, now, problem):
    old, current = archive_gap(store, now)
    middle = rich(now - timedelta(seconds=30))
    if problem == "sell_flow":
        middle.features.spot_taker_buy_ratio = 0.3
    elif problem == "thin_depth":
        middle.features.spot_depth_bands["0.1"]["observed_bid_usd"] = 100.0
    elif problem == "missing_flow":
        middle.features.spot_taker_buy_ratio = None
    else:
        middle.component_received_at["book"] = middle.available_at + timedelta(seconds=1)
    store.save_snapshot(middle)
    q = quality(store, now, settings)
    assert q["snapshot_ids"] == [current.id, middle.id, old.id]
    assert q["status"] == "WAIT" and not q["checks"]["persistent_demand"]


def test_slow_cadence_can_confirm_but_reports_sampling_gap(store, settings, now):
    for offset in (0, 325, 650):
        store.save_snapshot(rich(now + timedelta(seconds=offset)))
    q = quality(store, now + timedelta(seconds=650), settings)
    assert q["status"] == "PASS" and q["max_observation_gap_seconds"] == 325
    assert q["has_observation_gap"]


def test_component_gap_is_visible_even_with_short_snapshot_intervals(store, settings, now):
    old = rich(now - timedelta(seconds=280))
    middle = rich(now - timedelta(seconds=20))
    current = rich(now)
    for component in ("book", "candles"):
        old.component_times[component] = now - timedelta(seconds=400)
        middle.component_times[component] = now - timedelta(seconds=30)
    for snap in (old, middle, current):
        store.save_snapshot(snap)
    q = quality(store, now, settings)
    assert q["status"] == "PASS"
    assert q["max_observation_gap_seconds"] == 260
    assert q["max_component_gap_seconds"] == {"book": 370, "candles": 370}
    assert q["has_observation_gap"]


def test_historical_lows_cannot_revoke_without_two_current_fresh_observations(store, settings, now):
    old = rich(now - timedelta(minutes=10), buy=0.3)
    current = rich(now, buy=0.3)
    store.save_snapshot(old)
    store.save_snapshot(current)
    assert quality(store, now, settings)["confirmation_state"] == "CONFIRMED"
    assert not quality(store, now, settings)["support_withdrawn"]
    later = rich(now + timedelta(seconds=120), buy=0.3)
    store.save_snapshot(later)
    assert quality(store, later.available_at, settings)["support_withdrawn"]


def test_archived_anchor_policy_is_replayed_and_parameter_changes_split_versions(
    store, settings, now
):
    settings.enable_entry_quality = True
    _, current = archive_gap(store, now)
    store.save_decision_config(now, settings)
    live = signal(store, settings, current, now)
    settings.entry_anchor_max_age_seconds = 300
    changed = signal(store, settings, current, now)
    assert changed.kind == SignalKind.WATCH and changed.rule_version != live.rule_version
    replayed = replay(store, now, now + timedelta(seconds=1), settings)["signals"]
    again = next(s for s in replayed if s["rule_id"] == live.rule_id)
    assert again["kind"] == live.kind
    assert again["decision"]["entry_quality"] == live.decision["entry_quality"]
    assert store.decision_config(now)["entry_anchor_max_age_seconds"] == 1800


def test_replay_of_old_config_does_not_silently_expand_its_anchor_window(store, settings, now):
    settings.enable_entry_quality = True
    _, current = archive_gap(store, now)
    recorded = decision_config(settings)
    recorded.pop("entry_anchor_max_age_seconds")
    payload = json.dumps(recorded)
    with store.connect() as db:
        db.execute("INSERT INTO decision_configs VALUES (?,?)", (stamp(now), payload))
    assert signal(store, settings, current, now).kind == SignalKind.ENTRY_CANDIDATE
    replayed = replay(store, now, now + timedelta(seconds=1), settings)["signals"]
    again = next(s for s in replayed if s["rule_id"] == "spot-led-momentum")
    assert again["kind"] == "WATCH"
    assert again["decision"]["entry_quality"]["anchor_max_age_seconds"] == 300
    with store.connect() as db:
        assert db.execute("SELECT payload FROM decision_configs").fetchone()[0] == payload


def test_legacy_thirty_second_config_cannot_bypass_v2_minimum_in_replay(store, settings, now):
    settings.enable_entry_quality = True
    archive_gap(store, now, 30)
    recorded = decision_config(settings)
    recorded.pop("entry_anchor_max_age_seconds")
    recorded["entry_confirmation_seconds"] = 30
    payload = json.dumps(recorded)
    with store.connect() as db:
        db.execute("INSERT INTO decision_configs VALUES (?,?)", (stamp(now), payload))
    replayed = replay(store, now, now + timedelta(seconds=1), settings)["signals"]
    candidate = next(s for s in replayed if s["rule_id"] == "spot-led-momentum")
    assert candidate["kind"] == "WATCH"
    assert candidate["decision"]["entry_quality"]["minimum_component_separation_seconds"] == 60
    with store.connect() as db:
        assert db.execute("SELECT payload FROM decision_configs").fetchone()[0] == payload


def test_api_and_notification_expose_gap_instead_of_claiming_continuity(
    store, settings, now, monkeypatch
):
    settings.enable_entry_quality = True
    _, current = archive_gap(store, now)
    candidate = signal(store, settings, current, now)
    store.save_signal(candidate)
    alert = publish_alert(store, candidate, None, now, settings)
    message = notification_message([alert], "LONG")
    assert "历史锚点距今 600 秒" in message
    assert "最大观测间隔 600 秒" in message
    assert "盘口/K 线最大源间隔 600/600 秒" in message
    assert "不代表停机期间买盘持续成立" in message
    monkeypatch.setattr("muse_btc.api.utc_now", lambda: now)
    with TestClient(create_app(settings)) as client:
        report = client.get("/api/quality").json()
        assert client.get("/health").json()["entry_quality_version"] == "entry-quality-v2"
    q = report["assets"][current.asset_id]["entry_quality"]
    assert q["status"] == "PASS" and q["observation_continuity"] == "GAPPED"
    assert q["anchor_age_seconds"] == 600


def test_anchor_cannot_cross_sources(settings, now):
    old = rich(now - timedelta(minutes=10))
    current = rich(now)
    old.source = "OTHER_SOURCE"
    assert asset_quality(current, [old, current], now, settings)["status"] == "WAIT"


def test_future_decision_metadata_cannot_supply_an_anchor(store, settings, now):
    old = rich(now - timedelta(minutes=10))
    old.decision_at = now + timedelta(seconds=1)
    current = rich(now)
    store.save_snapshot(old)
    store.save_snapshot(current)
    q = quality(store, now, settings)
    assert q["status"] == "WAIT" and old.id not in q["snapshot_ids"]
