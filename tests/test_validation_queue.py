import asyncio
import threading
from datetime import timedelta

import pytest
from conftest import snapshot

from muse_btc.api import create_app
from muse_btc.intelligence import IntelligenceStore
from muse_btc.models import SignalEvent, utc_now
from muse_btc.rules import evaluate, market_regime
from muse_btc.storage import Store, stamp
from muse_btc.validation import measure_signal, validate_pending, validation_batch


def candidate(store, settings, now):
    snap = snapshot(now)
    store.save_snapshot(snap)
    signal = evaluate(snap, market_regime(snap, now, settings), now, settings)[0]
    store.save_signal(signal)
    return signal


def test_future_jobs_do_not_load_config_or_prices(store, settings, now, monkeypatch):
    candidate(store, settings, now)

    def forbid(*args):
        raise AssertionError("Future jobs must not query their historical inputs")

    monkeypatch.setattr(store, "decision_config", forbid)
    monkeypatch.setattr(store, "quote_range", forbid)
    assert validation_batch(store, now, settings)["attempted"] == 0


def test_bounded_queue_survives_restart_and_does_not_repeat_results(store, settings, now):
    for _ in range(5):
        candidate(store, settings, now)
    store.save_snapshot(snapshot(now + timedelta(minutes=5), price=102))
    as_of = now + timedelta(minutes=6)
    first = validation_batch(store, as_of, settings, limit=2)
    assert first["attempted"] == first["completed"] == 2
    restarted = Store(store.path)
    assert restarted.validation_queue(as_of)["due_count"] == 3
    assert validate_pending(restarted, as_of, settings) == 3
    assert validate_pending(restarted, as_of, settings) == 0
    assert len(restarted.outcomes()) == 5


def test_closed_missing_window_is_not_rescanned_without_explicit_retry(
    store, settings, now, monkeypatch
):
    signal = candidate(store, settings, now)
    as_of = now + timedelta(minutes=10)
    result = validation_batch(store, as_of, settings)
    assert result["missing"] == 1
    assert store.outcomes() == []
    calls = []
    original = store.quote_range
    monkeypatch.setattr(store, "quote_range", lambda *args: calls.append(args) or original(*args))
    assert validation_batch(Store(store.path), as_of, settings)["attempted"] == 0
    assert validation_batch(store, as_of, settings)["attempted"] == 0
    assert calls == []
    # A later import retains the historical source/availability timestamps.
    store.save_snapshot(snapshot(now + timedelta(minutes=5), price=102))
    assert validate_pending(store, as_of, settings) == 0
    assert validate_pending(store, as_of, settings, retry_missing=True) == 1
    assert store.outcomes()[0].signal_id == signal.id
    assert store.outcomes()[0].return_pct == pytest.approx(2)


def test_open_endpoint_window_is_deferred_until_new_quote_arrives(store, settings, now):
    candidate(store, settings, now)
    first = validation_batch(store, now + timedelta(minutes=5), settings)
    assert first["deferred"] == 1 and first["missing"] == 0
    store.save_snapshot(snapshot(now + timedelta(minutes=6), price=101))
    assert validation_batch(store, now + timedelta(minutes=6), settings)["attempted"] == 0
    result = validation_batch(store, now + timedelta(minutes=7), settings)
    assert result["completed"] == 1


def test_backfill_cursor_is_incremental_and_preserves_completed_labels(store, settings, now):
    one = candidate(store, settings, now)
    candidate(store, settings, now)
    store.save_snapshot(snapshot(now + timedelta(minutes=5), price=102))
    outcome = measure_signal(store, one, 300, now + timedelta(minutes=6), settings)
    store.save_outcome(outcome)
    with store.connect() as db:
        db.execute("DELETE FROM validation_jobs")
    assert store.seed_validation_jobs(1) == 1
    assert Store(store.path).seed_validation_jobs(1) == 1
    assert store.seed_validation_jobs(1) == 0
    assert store.validation_queue(now)["counts"]["PENDING"] == 5
    assert store.outcomes()[0] == outcome


def test_validation_batch_reuses_prices_and_archived_configuration(
    store, settings, now, monkeypatch
):
    signal = candidate(store, settings, now)
    store.save_decision_config(now, settings)
    for minute in range(2, 17, 2):
        store.save_snapshot(snapshot(now + timedelta(minutes=minute), price=102))
    original = store.quote_range
    calls = []

    def quotes(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(store, "quote_range", quotes)
    settings.fee_bps_each_way = 100
    result = validation_batch(store, now + timedelta(minutes=17), settings)
    assert result["completed"] == 2
    assert len([c for c in calls if c[0] == signal.emitted_at]) == 1
    assert all(o.round_trip_cost_bps == 30 for o in store.outcomes())


def test_soft_budget_still_makes_progress_after_a_slow_price_read(
    store, settings, now, monkeypatch
):
    candidate(store, settings, now)
    clock = [0]
    monkeypatch.setattr("muse_btc.validation.time.monotonic", lambda: clock[0])
    original = store.quote_range

    def slow_read(*args):
        clock[0] += 1
        return original(*args)

    monkeypatch.setattr(store, "quote_range", slow_read)
    result = validation_batch(store, now + timedelta(minutes=20), settings, budget_seconds=0.1)
    assert result["attempted"] == 1 and result["budget_exhausted"]
    assert store.validation_queue(now + timedelta(minutes=20))["due_count"] == 1


def test_time_range_query_uses_index_and_price_projection_omits_candles(store, now):
    snap = snapshot(now)
    store.save_snapshot(snap)
    with store.connect() as db:
        plan = [
            row[3]
            for row in db.execute(
                "EXPLAIN QUERY PLAN SELECT payload FROM snapshots "
                "WHERE available_at>=? AND available_at<=? ORDER BY available_at,rowid",
                (stamp(now), stamp(now)),
            )
        ]
    assert any("SEARCH snapshots" in item for item in plan)
    assert not any("SCAN snapshots" in item or "TEMP B-TREE" in item for item in plan)
    quote = store.quote_range(now, now, snap.asset_id)[0]
    assert quote.id == snap.id and quote.price == snap.price
    assert not hasattr(quote, "candles") and not hasattr(quote, "features")


def test_active_signal_query_matches_latest_lifecycle_state(store, settings, now):
    active = candidate(store, settings, now)
    ended = candidate(store, settings, now)
    store.add_event(SignalEvent(signal_id=ended.id, state="EXPIRED", event_at=now, reason="test"))
    assert [s.id for s in store.active_signals(now)] == [active.id]
    store.add_event(
        SignalEvent(signal_id=ended.id, state="ACTIVE", event_at=now, reason="test reactivation")
    )
    assert {s.id for s in store.active_signals(now)} == {active.id, ended.id}
    assert store.active_signals(now - timedelta(seconds=1)) == []


@pytest.mark.asyncio
async def test_market_collection_continues_while_validation_is_blocked(settings, monkeypatch):
    settings.enable_background_validation = True
    entered, release = threading.Event(), threading.Event()
    app = create_app(settings)

    def blocked(*args, **kwargs):
        entered.set()
        if not release.wait(5):
            raise AssertionError("Validation was not released")
        return {"completed": 0}

    async def current():
        return [snapshot(utc_now())]

    monkeypatch.setattr("muse_btc.validation_worker.validation_batch", blocked)
    monkeypatch.setattr(app.state.collector.providers, "binance", current)
    async with app.router.lifespan_context(app):
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            result = await asyncio.wait_for(app.state.collector.collect_once(), 2)
            assert result["status"] == "COMPLETE"
            assert "VALIDATING" not in result["phase_seconds"]
            assert result["validation_status"] == "INDEPENDENT_WORKER"
            assert not release.is_set()
            task = app.state.collector.validation_task
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done()
            archive = IntelligenceStore(app.state.store)
            assert archive.acquire("validation-worker", utc_now(), 120) is None
            assert archive.acquire("market-collector", utc_now(), 120)
        finally:
            release.set()
    assert app.state.store.state("validation_worker")["status"] == "STOPPED"
