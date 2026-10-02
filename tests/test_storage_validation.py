from datetime import timedelta, timezone

import pytest
from conftest import snapshot

from muse_btc.models import Module
from muse_btc.rules import evaluate, market_regime
from muse_btc.service import Collector, signal_view
from muse_btc.storage import Store
from muse_btc.validation import measure_signal, replay, validate_pending, validation_report


def test_raw_records_are_immutable_hashed_and_restartable(store, now):
    one = store.save_raw("TEST", "/test", {"price": 100}, now)
    two = store.save_raw("TEST", "/test", {"price": 101}, now)
    assert one != two
    assert store.raw(one)["payload"] == {"price": 100}
    assert store.raw(one)["payload_hash"] != store.raw(two)["payload_hash"]
    assert Store(store.path).raw(two)["payload"]["price"] == 101


def test_missing_or_future_lineage_rejected(store, now):
    with pytest.raises(ValueError, match="lineage"):
        store.save_snapshot(snapshot(now, raw_ids=["missing"]))
    future = store.save_raw("TEST", "/test", {}, now + timedelta(seconds=1))
    with pytest.raises(ValueError, match="lineage"):
        store.save_snapshot(snapshot(now, raw_ids=[future]))
    with pytest.raises(ValueError, match="future"):
        store.save_snapshot(snapshot(now, market_time=now + timedelta(seconds=1)))


def test_as_of_reads_cannot_see_future_and_normalize_timezones(store, now):
    early = snapshot(now)
    late = snapshot(now + timedelta(minutes=1), price=200)
    store.save_snapshot(early)
    store.save_snapshot(late)
    alternate_zone = now.astimezone(timezone(timedelta(hours=8)))
    assert store.latest_snapshots(alternate_zone)[0].id == early.id
    assert store.latest_snapshots(now + timedelta(minutes=2))[0].price == 200


def test_alert_cooldown_survives_collector_restart(store, settings, now):
    btc = snapshot(now)
    store.save_snapshot(btc)
    regime = market_regime(btc, now, settings)
    first = Collector(settings, store, None)
    assert first._process_signals([btc], regime, now) == 1
    second = Collector(settings, Store(store.path), None)
    assert second._process_signals([btc], regime, now + timedelta(seconds=10)) == 0
    assert len(store.signals()) == 1


def test_stale_signal_is_paused_without_rewriting_original_event(store, settings, now):
    btc = snapshot(now)
    store.save_snapshot(btc)
    signal = evaluate(btc, market_regime(btc, now, settings), now, settings)[0]
    store.save_signal(signal)
    view = signal_view(store, signal, now + timedelta(minutes=10), settings)
    assert view["state"] == "PAUSED"
    assert view["lifecycle_state"] == "ACTIVE"
    assert view["data_current"] is False
    assert store.signal_events(signal.id)[-1].state == "ACTIVE"


def test_price_invalidation_creates_linked_event_once(store, settings, now):
    btc = snapshot(now)
    store.save_snapshot(btc)
    collector = Collector(settings, store, None)
    collector._process_signals([btc], market_regime(btc, now, settings), now)
    original = store.signals()[0]
    later = snapshot(now + timedelta(minutes=1), price=90)
    later.features.relative_volume = 1
    store.save_snapshot(later)
    collector._process_signals(
        [later], market_regime(later, later.available_at, settings), later.available_at
    )
    assert store.signal_events(original.id)[-1].state == "INVALIDATED"
    invalidated = [s for s in store.signals() if s.kind == "INVALIDATED"]
    assert len(invalidated) == 1
    assert invalidated[0].parent_signal_id == original.id
    collector._process_signals(
        [later], market_regime(later, later.available_at, settings), later.available_at
    )
    assert len([s for s in store.signals() if s.kind == "INVALIDATED"]) == 1


def test_forward_outcome_waits_for_real_future_price_and_costs(store, settings, now):
    btc = snapshot(now)
    store.save_snapshot(btc)
    signal = evaluate(btc, market_regime(btc, now, settings), now, settings)[0]
    store.save_signal(signal)
    assert measure_signal(store, signal, 300, now, settings) is None
    assert measure_signal(store, signal, 300, now + timedelta(minutes=10), settings) is None
    for minutes, value in [(2, 99), (4, 101), (5, 102)]:
        store.save_snapshot(snapshot(now + timedelta(minutes=minutes), price=value))
    outcome = measure_signal(store, signal, 300, now + timedelta(minutes=6), settings)
    assert outcome.return_pct == pytest.approx(2)
    assert outcome.paper_net_return_pct == pytest.approx(1.7)
    assert outcome.max_adverse_pct == pytest.approx(-1)
    assert outcome.sample_count == 3
    assert validate_pending(store, now + timedelta(minutes=6), settings) == 1
    assert validate_pending(store, now + timedelta(minutes=6), settings) == 0
    assert validation_report(store, settings)["validation_status"] == "OBSERVATION_ONLY"


def test_sparse_samples_are_excluded_from_covered_statistics(store, settings, now):
    btc = snapshot(now)
    store.save_snapshot(btc)
    signal = evaluate(btc, market_regime(btc, now, settings), now, settings)[0]
    store.save_signal(signal)
    store.save_snapshot(snapshot(now + timedelta(hours=1), price=110))
    validate_pending(store, now + timedelta(hours=1), settings)
    hour = next(
        r for r in validation_report(store, settings)["rules"] if r["horizon_seconds"] == 3600
    )
    assert hour["measured_count"] == 1
    assert hour["covered_count"] == 0
    assert hour["mean_return_pct"] is None


def test_replay_never_uses_future_btc_regime(store, settings, now):
    alt = snapshot(now, Module.ALT)
    future_btc = snapshot(now + timedelta(minutes=1))
    store.save_snapshot(alt)
    store.save_snapshot(future_btc)
    result = replay(store, now - timedelta(seconds=1), now + timedelta(minutes=2), settings)
    early = [s for s in result["signals"] if s["asset_id"] == alt.asset_id]
    assert early and early[0]["kind"] == "WATCH"
    assert early[0]["btc_snapshot_id"] is None
    assert store.signals() == []  # Replay never publishes alerts.
