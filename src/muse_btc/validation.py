import time
from datetime import datetime, timedelta
from itertools import groupby

from .btc_intelligence import apply_btc_context
from .config import Settings
from .decisions import decide
from .entry_quality import quality_contexts
from .models import Outcome, Signal, SignalKind, Snapshot, utc_now
from .performance import build_report
from .rules import RULE_VERSION, market_regime, quote_usable
from .storage import Store

HORIZONS = (300, 900, 3600, 14400, 86400, 259200, 604800, 1209600, 2592000)


def observed_path(history, start, end, settings):
    """Order by quote time; repeated or delayed copies are not new observations."""
    by_time = {}
    for snapshot in sorted(history, key=lambda s: s.available_at):
        if start < snapshot.market_time <= end and quote_usable(
            snapshot, snapshot.available_at, settings
        ):
            by_time.setdefault(snapshot.market_time, snapshot)
    return [by_time[t] for t in sorted(by_time)]


def measure_signal(
    store: Store,
    signal: Signal,
    horizon: int,
    now: datetime,
    settings: Settings,
    *,
    recorded_settings=False,
    history=None,
) -> Outcome | None:
    target = signal.emitted_at + timedelta(seconds=horizon)
    if target > now:
        return None
    if not recorded_settings:
        recorded = store.decision_config(signal.emitted_at)
        if recorded:
            settings = settings.model_copy(update=recorded)
    tolerance = max(settings.poll_seconds * 2, 60)
    end_limit = min(now, target + timedelta(seconds=tolerance))
    history = observed_path(
        history
        if history is not None
        else store.quote_range(signal.emitted_at, end_limit, signal.asset_id),
        signal.emitted_at,
        end_limit,
        settings,
    )
    end = next((s for s in history if s.market_time >= target), None)
    if end is None:
        return None
    measured = [
        s
        for s in history
        if s.available_at <= end.available_at and s.market_time <= end.market_time
    ]
    prices = [signal.reference_price] + [s.price for s in measured]
    returns = [(price / signal.reference_price - 1) * 100 for price in prices]
    stamps = [signal.emitted_at] + [s.market_time for s in measured]
    max_gap = max(
        ((right - left).total_seconds() for left, right in zip(stamps, stamps[1:], strict=False)),
        default=0,
    )
    btc_return = None
    baseline = store.snapshot(signal.btc_snapshot_id) if signal.btc_snapshot_id else None
    if baseline and quote_usable(baseline, signal.emitted_at, settings):
        btc_history = store.quote_range(target, end_limit, "binance:BTCUSDT")
        btc_end = next(
            (
                s
                for s in btc_history
                if abs((s.market_time - end.market_time).total_seconds())
                <= settings.time_alignment_seconds
                and s.market_time >= target
                and quote_usable(s, s.available_at, settings)
            ),
            None,
        )
        if btc_end:
            btc_return = (btc_end.price / baseline.price - 1) * 100
    cost = 2 * (settings.fee_bps_each_way + settings.slippage_bps_each_way)
    raw_return = (end.price / signal.reference_price - 1) * 100
    return Outcome(
        signal_id=signal.id,
        horizon_seconds=horizon,
        evaluated_at=now,
        end_snapshot_id=end.id,
        measured_at=end.market_time,
        return_pct=raw_return,
        btc_return_pct=btc_return,
        excess_return_pct=raw_return - btc_return if btc_return is not None else None,
        max_favorable_pct=max(returns),
        max_adverse_pct=min(returns),
        paper_net_return_pct=raw_return - cost / 100
        if signal.kind == SignalKind.ENTRY_CANDIDATE
        else None,
        round_trip_cost_bps=cost,
        sample_count=len(measured),
        max_observation_gap_seconds=max_gap,
        max_allowed_gap_seconds=settings.stale_seconds,
        label_available_at=end.available_at,
        time_to_mfe_seconds=(
            stamps[returns.index(max(returns))] - signal.emitted_at
        ).total_seconds(),
        time_to_mae_seconds=(
            stamps[returns.index(min(returns))] - signal.emitted_at
        ).total_seconds(),
    )


def validation_batch(store, now, settings, *, limit=None, budget_seconds=None, seed_limit=64):
    """Bound work between tasks; a missing closed window requires explicit retry.

    The time budget is cooperative, not a promise to interrupt an active SQL query.
    Completion and deferral are durable, so cancellation/restarts do not lose work.
    """
    started = time.monotonic()
    seeded = store.seed_validation_jobs(seed_limit)
    jobs = store.due_validation_jobs(now, limit or settings.validation_batch_size)
    by_signal = {}
    for job in jobs:
        by_signal.setdefault(job["signal_id"], []).append(job)
    result = {
        "seeded_signals": seeded,
        "attempted": 0,
        "completed": 0,
        "missing": 0,
        "deferred": 0,
        "budget_exhausted": False,
    }
    for batch in by_signal.values():
        if budget_seconds is not None and time.monotonic() - started >= budget_seconds:
            result["budget_exhausted"] = True
            break
        signal = Signal.model_validate_json(batch[0]["payload"])
        recorded = store.decision_config(signal.emitted_at)
        config = settings.model_copy(update=recorded) if recorded else settings
        tolerance = max(config.poll_seconds * 2, 60)
        end = min(
            now,
            signal.emitted_at
            + timedelta(seconds=max(j["horizon_seconds"] for j in batch) + tolerance),
        )
        history = store.quote_range(signal.emitted_at, end, signal.asset_id)
        for job in batch:
            if (
                result["attempted"]
                and budget_seconds is not None
                and time.monotonic() - started >= budget_seconds
            ):
                result["budget_exhausted"] = True
                break
            horizon = job["horizon_seconds"]
            outcome = measure_signal(
                store, signal, horizon, now, config, recorded_settings=True, history=history
            )
            result["attempted"] += 1
            if outcome:
                result["completed"] += store.save_outcome(outcome)
            else:
                deadline = signal.emitted_at + timedelta(seconds=horizon + tolerance)
                missing = now >= deadline
                retry = min(now + timedelta(seconds=config.poll_seconds), deadline)
                store.defer_validation(signal.id, horizon, now, retry, missing=missing)
                result["missing" if missing else "deferred"] += 1
        if result["budget_exhausted"]:
            break
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


def validate_pending(
    store: Store, now: datetime, settings: Settings, *, retry_missing=False
) -> int:
    # Explicit CLI work can drain the archive; the market collector never calls this.
    while store.seed_validation_jobs(256):
        pass
    if retry_missing:
        store.retry_missing_validation(now)
    count = 0
    while store.due_validation_jobs(now, 1):
        result = validation_batch(store, now, settings, limit=256)
        count += result["completed"]
    return count


def validation_report(store: Store, settings: Settings, now: datetime | None = None) -> dict:
    now = now or utc_now()
    report = build_report(
        store.signals(limit=100000, as_of=now), store.outcomes(), HORIZONS, settings, now
    )
    report["validation_queue"] = store.validation_queue(now)
    return report


def replay(store: Store, start: datetime, end: datetime, settings: Settings) -> dict:
    if end <= start:
        raise ValueError("Replay end must be after start")
    latest: dict[str, Snapshot] = {s.asset_id: s for s in store.latest_snapshots(start)}
    cooldowns: dict[tuple[str, str, str, str], datetime] = {}
    signals: list[Signal] = []
    skipped = 0
    legacy_config_batches = 0
    archived = store.snapshot_range(start - timedelta(seconds=settings.stale_seconds), end)
    archived = [s for s in archived if start <= (s.decision_at or s.available_at) <= end]
    archived.sort(key=lambda s: s.decision_at or s.available_at)
    # Reproduce the collection barrier: all evidence is available before decisions.
    for as_of, group in groupby(archived, key=lambda s: s.decision_at or s.available_at):
        recorded = store.decision_config(as_of)
        config = settings.model_copy(update=recorded) if recorded else settings
        legacy_config_batches += int(recorded is None)
        batch = []
        membership = store.universe(as_of)
        allowed = (
            {"binance:" + r["binance_symbol"] for r in membership["entries"]}
            if membership
            else None
        )
        for snapshot in group:
            if allowed is not None and snapshot.asset_id not in allowed:
                continue
            if snapshot.feature_version not in {"features-v1", "features-v4-1"}:
                skipped += 1
                continue
            if snapshot.available_at > as_of:
                raise ValueError("Archived snapshot contains future evidence")
            latest[snapshot.asset_id] = snapshot
            batch.append(snapshot)
        regime = market_regime(latest.get("binance:BTCUSDT"), as_of, config)
        if config.enable_intelligence:
            regime = apply_btc_context(regime, store, config, as_of, latest.get("binance:BTCUSDT"))
        qualities = quality_contexts(store, as_of, config, batch)
        for snapshot in batch:
            for signal in decide(
                snapshot, regime, as_of, config, store, quality=qualities.get(snapshot.asset_id)
            ):
                key = (signal.asset_id, signal.rule_id, signal.kind, signal.rule_version)
                previous = cooldowns.get(key)
                if previous and (as_of - previous).total_seconds() < config.alert_cooldown_seconds:
                    continue
                signals.append(signal)
                cooldowns[key] = as_of
    outcomes = [
        outcome
        for signal in signals
        for horizon in HORIZONS
        if (outcome := measure_signal(store, signal, horizon, end, settings))
    ]
    return {
        "mode": "ARCHIVED_POINT_IN_TIME_REPLAY",
        "rule_version": RULE_VERSION,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "signal_count": len(signals),
        "outcome_count": len(outcomes),
        "skipped_feature_versions": skipped,
        "legacy_config_batches": legacy_config_batches,
        "config_policy": "ARCHIVED_WHEN_PRESENT_OTHERWISE_EXPLICIT_CURRENT_FALLBACK",
        "signals": [s.model_dump(mode="json") for s in signals],
        "outcomes": [o.model_dump(mode="json") for o in outcomes],
        "limitations": [
            "仅使用当时已采集的快照；不是采集前市场的完整历史回测",
            "应用当前规则版本，不修改历史提醒，不进行阈值优化",
        ],
    }
