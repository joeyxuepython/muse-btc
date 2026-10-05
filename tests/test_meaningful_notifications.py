"""Regression cases for stale, duplicate and unreported candidate cancellations."""

from datetime import timedelta

import httpx
import pytest
from conftest import snapshot
from fastapi.testclient import TestClient
from test_alert_delivery import signal_for

from muse_btc.alerts import notification_projections, publish_alert
from muse_btc.api import create_app
from muse_btc.delivery import build_deliveries, fetch_notification_batch
from muse_btc.models import SignalKind, new_id
from muse_btc.rules import market_regime
from muse_btc.service import Collector, signal_view


def original(
    store,
    settings,
    now,
    *,
    rule="momentum",
    kind=SignalKind.ENTRY_CANDIDATE,
    receipt="SENT",
    horizon=3600,
):
    snap = snapshot(now)
    store.save_snapshot(snap)
    parent = signal_for(snap, now, rule=rule, kind=kind)
    parent.invalidation_price = 98
    parent.invalidation_method = "ATR_2_FLOOR_0_5_FALLBACK_2"
    parent.horizon_seconds = horizon
    store.save_signal(parent)
    alert = publish_alert(store, parent, {"score": 81.2}, now, settings)
    if kind == SignalKind.ENTRY_CANDIDATE and receipt:
        store.save_notification_receipt([alert["notification_id"]], receipt, "mail", None, now)
    return parent, alert


def cancel(store, settings, parent, at):
    snap = snapshot(at, price=97)
    store.save_snapshot(snap)
    child = parent.model_copy(
        update={
            "id": new_id(),
            "kind": SignalKind.INVALIDATED,
            "parent_signal_id": parent.id,
            "emitted_at": at,
            "snapshot_id": snap.id,
            "reference_price": 97,
            "entry_zone": None,
            "title": "原信号失效",
            "evidence": ["价格跌破失效参考位"],
        }
    )
    store.save_signal(child)
    return publish_alert(store, child, None, at, settings)


def feed(settings, monkeypatch, at):
    monkeypatch.setattr("muse_btc.api.utc_now", lambda: at)
    with TestClient(create_app(settings)) as client:
        return client.get("/api/alerts/notifications").json()


def cancellation(page):
    return next(a for a in page["items"] if a["notification_class"] == "CANCELLATION")


def test_watch_failure_is_archived_without_creating_a_risk_notification(store, settings, now):
    parent, _ = original(store, settings, now, kind=SignalKind.WATCH)
    alert = cancel(store, settings, parent, now + timedelta(seconds=60))
    assert alert["level"] == "INFO" and alert["risk_level"] is None
    assert store.notification_page(0, 50)["items"] == []
    assert store.signal(alert["signal_id"]).evidence_score == parent.evidence_score


@pytest.mark.parametrize(
    "receipt,expected",
    [
        (None, "WAIT_PARENT_RECEIPT"),
        ("FAILED", "WAIT_PARENT_RECEIPT"),
        ("SKIPPED", "SKIP_UNDELIVERED_PARENT"),
        ("SENT", "READY"),
    ],
)
def test_cancel_requires_original_sent_receipt(
    store, settings, now, monkeypatch, receipt, expected
):
    parent, _ = original(store, settings, now, receipt=receipt)
    at = now + timedelta(seconds=60)
    cancel(store, settings, parent, at)
    page = feed(settings, monkeypatch, at)
    row = cancellation(page)
    assert row["delivery_status"] == expected
    assert row["parent_delivery_confirmed"] == (receipt == "SENT")
    assert len(build_deliveries(page["items"])) == (receipt == "SENT")


async def test_delayed_parent_receipt_is_deferred_then_becomes_ready(
    store, settings, now, monkeypatch
):
    parent, alert = original(store, settings, now, receipt=None)
    at = now + timedelta(seconds=60)
    child = cancel(store, settings, parent, at)
    monkeypatch.setattr("muse_btc.api.utc_now", lambda: at)
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        batch = await fetch_notification_batch(client, page_size=1)
        assert batch["deferred_notification_ids"] == [child["notification_id"]]
        assert batch["commit_cursor"] < batch["next_cursor"]
        assert batch["delivery_groups"] == []
    store.save_notification_receipt([alert["notification_id"]], "SENT", "mail", None, at)
    assert cancellation(feed(settings, monkeypatch, at))["delivery_status"] == "READY"


def test_future_receipt_does_not_prove_delivery_in_the_past(store, settings, now):
    parent, alert = original(store, settings, now, receipt=None)
    at = now + timedelta(seconds=60)
    child = cancel(store, settings, parent, at)
    store.save_notification_receipt(
        [alert["notification_id"]], "SENT", "mail", None, at + timedelta(seconds=1)
    )
    projected = notification_projections(store, [child], at)[0]
    assert projected["cancellation_delivery_status"] == "WAIT_PARENT_RECEIPT"


def test_cancel_after_original_observation_horizon_is_not_delivered(
    store, settings, now, monkeypatch
):
    parent, _ = original(store, settings, now, horizon=60)
    at = now + timedelta(seconds=61)
    cancel(store, settings, parent, at)
    assert cancellation(feed(settings, monkeypatch, at))["delivery_status"] == "SKIP_PARENT_EXPIRED"


def test_cancel_expires_in_five_minutes_even_with_fresh_quotes(store, settings, now, monkeypatch):
    parent, _ = original(store, settings, now)
    at = now + timedelta(seconds=60)
    cancel(store, settings, parent, at)
    later = at + timedelta(seconds=300)
    store.save_snapshot(snapshot(later, price=97))
    assert cancellation(feed(settings, monkeypatch, later))["delivery_status"] == "SKIP_EXPIRED"


async def test_same_asset_same_cause_cancel_groups_across_pages_and_reports_recovery(
    store, settings, now, monkeypatch
):
    parents = [original(store, settings, now, rule=rule)[0] for rule in ("momentum", "fusion")]
    at = now + timedelta(seconds=60)
    for parent in parents:
        cancel(store, settings, parent, at)
    store.save_snapshot(snapshot(at + timedelta(seconds=1), price=99))
    monkeypatch.setattr("muse_btc.api.utc_now", lambda: at + timedelta(seconds=1))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app(settings)), base_url="http://test"
    ) as client:
        batch = await fetch_notification_batch(client, page_size=1)
    assert len(batch["delivery_groups"]) == 1
    group = batch["delivery_groups"][0]
    assert group["category"] == "CANCELLATION"
    assert len(group["member_notification_ids"]) == len(group["parent_signal_ids"]) == 2
    assert group["opportunity_score"] is None and group["risk_level"] is None
    message = group["message_zh"]
    assert "原候选参考价 100" in message and "当时报价 97" in message and "最新价 99" in message
    assert "失效规则阈值 98" in message and "当前已回到旧阈值之上" in message
    assert "不代表新的看空判断" in message
    assert "81.2" not in message and "关键支撑" not in message and "此路不通" not in message


def test_cancel_does_not_overwrite_a_newer_opportunity(store, settings, now):
    parent, old_alert = original(store, settings, now)
    later = now + timedelta(seconds=30)
    snap = snapshot(later, price=103)
    store.save_snapshot(snap)
    newer = signal_for(snap, later, rule=parent.rule_id)
    store.save_signal(newer)
    active = publish_alert(store, newer, None, later, settings)
    assert active["id"] == old_alert["id"]
    child = cancel(store, settings, parent, now + timedelta(seconds=60))
    assert child["id"] != active["id"]
    assert (
        store.active_alert(parent.asset_id, parent.rule_id, now + timedelta(seconds=60))[
            "signal_id"
        ]
        == newer.id
    )
    assert store.alert(active["id"])["resolved_at"] is None


def test_legacy_cancel_is_classified_using_archived_signal_without_changing_outbox(
    store, settings, now, monkeypatch
):
    parent, _ = original(store, settings, now, kind=SignalKind.WATCH)
    at = now + timedelta(seconds=60)
    child = cancel(store, settings, parent, at)
    for field in ("notification_class", "parent_signal_id", "signal_kind", "risk_type"):
        child.pop(field, None)
    child["level"] = "CRITICAL_RISK"
    child["notification_id"] = new_id()
    store.save_alert(
        child, {"event_at": at.isoformat(), "notification_id": child["notification_id"]}
    )
    before = store.notification_page(0, 50)["items"]
    row = cancellation(feed(settings, monkeypatch, at))
    assert row["delivery_status"] == "SKIP_UNDELIVERED_PARENT" and row["risk_level"] is None
    assert row["display_level"] == "INFO"
    assert store.notification_page(0, 50)["items"] == before


@pytest.mark.parametrize("rule", ["spot-sell-pressure", "leverage-overheat"])
@pytest.mark.parametrize(
    "current_state,expected",
    [
        ("active", "READY"),
        ("cleared", "SKIP_RISK_CLEARED"),
        ("missing", "SKIP_STALE_DATA"),
    ],
)
def test_current_market_risk_conditions_are_rechecked(
    store, settings, now, monkeypatch, rule, current_state, expected
):
    snap = snapshot(now)
    snap.features.return_15m_pct = -2
    snap.features.spot_taker_buy_ratio = 0.4
    snap.features.funding_rate_pct = 0.06
    snap.features.oi_change_5m_pct = 3
    store.save_snapshot(snap)
    publish_alert(
        store, signal_for(snap, now, rule=rule, kind=SignalKind.RISK), None, now, settings
    )
    at = now + timedelta(seconds=60)
    current = snap.model_copy(
        deep=True, update={"id": new_id(), "market_time": at, "available_at": at}
    )
    if current_state != "active":
        current.features.return_15m_pct = 1 if current_state == "cleared" else None
        current.features.funding_rate_pct = 0.01 if current_state == "cleared" else None
    store.save_snapshot(current)
    row = feed(settings, monkeypatch, at)["items"][0]
    assert row["delivery_status"] == expected
    assert bool(build_deliveries([row])) == (expected == "READY")


def test_stale_risk_and_old_queued_risk_cannot_be_sent(store, settings, now, monkeypatch):
    snap = snapshot(now)
    store.save_snapshot(snap)
    alert = publish_alert(store, signal_for(snap, now, kind=SignalKind.RISK), None, now, settings)
    at = now + timedelta(seconds=120)
    settings.stale_seconds = 60
    assert feed(settings, monkeypatch, at)["items"][0]["delivery_status"] == "SKIP_STALE_DATA"
    # Legacy TTL did not constrain notification_expires_at. The read policy does.
    alert["notification_expires_at"] = alert["expires_at"]
    store.save_alert(alert)
    at = now + timedelta(seconds=300)
    store.save_snapshot(snapshot(at))
    assert feed(settings, monkeypatch, at)["items"][0]["delivery_status"] == "SKIP_EXPIRED"


@pytest.mark.parametrize("kind", [SignalKind.RISK, SignalKind.ENTRY_CANDIDATE])
def test_ranking_and_numeric_evidence_do_not_repeat_notifications(store, settings, now, kind):
    snap = snapshot(now)
    store.save_snapshot(snap)
    signal = signal_for(snap, now, kind=kind)
    first = publish_alert(store, signal, {"score": 10}, now, settings)
    signal.evidence = ["测试证据 9%"]
    second = publish_alert(store, signal, {"score": 90}, now + timedelta(seconds=1), settings)
    assert first["notification_id"] == second["notification_id"]
    assert len(store.notification_page(0, 50)["items"]) == 1


def test_opportunity_cannot_be_delivered_below_its_recorded_threshold(
    store, settings, now, monkeypatch
):
    original(store, settings, now, receipt=None)
    at = now + timedelta(seconds=60)
    store.save_snapshot(snapshot(at, price=97))
    assert (
        feed(settings, monkeypatch, at)["items"][0]["delivery_status"]
        == "SKIP_INVALIDATION_REACHED"
    )


@pytest.mark.parametrize(
    "state,expected", [("active", "ACTIVE"), ("cleared", "RESOLVED"), ("stale", "PAUSED")]
)
def test_legacy_signal_view_does_not_label_cleared_or_stale_risk_as_active(
    store, settings, now, state, expected
):
    first = snapshot(now)
    first.features.return_15m_pct = -2
    first.features.spot_taker_buy_ratio = 0.4
    signal = signal_for(first, now, rule="spot-sell-pressure", kind=SignalKind.RISK)
    store.save_snapshot(first)
    store.save_signal(signal)
    events = store.signal_events(signal.id)
    at = now + timedelta(seconds=301 if state == "stale" else 60)
    if state != "stale":
        current = snapshot(at)
        if state == "active":
            current.features.return_15m_pct = -2
            current.features.spot_taker_buy_ratio = 0.4
        store.save_snapshot(current)
    assert signal_view(store, signal, at, settings)["state"] == expected
    assert store.signal_events(signal.id) == events  # Projection never rewrites history.


def test_risk_can_notify_again_only_after_conditions_clear(store, settings, now):
    settings.enable_intelligence = False
    collector = Collector(settings, store, None)

    def run(at, active):
        snap = snapshot(at)
        if active:
            snap.features.return_15m_pct = -2
            snap.features.spot_taker_buy_ratio = 0.4
        store.save_snapshot(snap)
        collector._process_signals([snap], market_regime(snap, at, settings), at)

    run(now, True)
    run(now + timedelta(seconds=60), True)
    first = [
        a for a in store.notification_page(0, 50)["items"] if a["rule_id"] == "spot-sell-pressure"
    ]
    assert len(first) == 1
    run(now + timedelta(seconds=120), False)
    assert store.alert(first[0]["id"])["resolved_at"]
    run(now + timedelta(seconds=180), True)
    risks = [
        a for a in store.notification_page(0, 50)["items"] if a["rule_id"] == "spot-sell-pressure"
    ]
    assert len(risks) == 2 and risks[0]["id"] != risks[1]["id"]
