"""Offline regressions for the cloud poller's missed and delayed alert reports."""

import asyncio
import json
import sqlite3
from datetime import timedelta

import httpx
import pytest
from conftest import snapshot
from fastapi.testclient import TestClient

from muse_btc.alerts import change_alert, publish_alert
from muse_btc.api import create_app
from muse_btc.models import Signal, SignalEvaluation, SignalKind
from muse_btc.providers import Providers
from muse_btc.storage import Store


def signal_for(snap, at, *, rule="test-rule", kind=SignalKind.ENTRY_CANDIDATE):
    return Signal(
        asset_id=snap.asset_id,
        symbol=snap.symbol,
        module=snap.module,
        kind=kind,
        rule_id=rule,
        emitted_at=at,
        snapshot_id=snap.id,
        title="测试信号",
        evidence=["测试证据 1%"],
        evidence_groups=["price"] if kind == SignalKind.WATCH else ["price", "flow", "oi"],
        evidence_score=70,
        reference_price=snap.price,
        expires_at=at + timedelta(hours=4),
        horizon_seconds=3600,
        evaluation=SignalEvaluation(
            metric="RISK_DIRECTION" if kind == SignalKind.RISK else "LONG_RETURN",
            horizons_seconds=(3600,),
            primary_horizon_seconds=3600,
            rationale="Explicit test-fixture observation window",
        ),
    )


def test_frozen_pages_drain_more_than_50_and_resume_after_restart(store, settings, now):
    snap = snapshot(now)
    store.save_snapshot(snap)
    for i in range(115):
        publish_alert(
            store,
            signal_for(snap, now, rule=f"risk-{i}", kind=SignalKind.RISK),
            None,
            now,
            settings,
        )
    first = store.notification_page(0, 50)
    assert first["has_more"] and first["upper_cursor"] == 115
    publish_alert(
        store, signal_for(snap, now, rule="new-risk", kind=SignalKind.RISK), None, now, settings
    )
    restarted = Store(settings.database_path)
    second = restarted.notification_page(
        first["next_cursor"], 50, first["upper_cursor"], first["generation"]
    )
    last = restarted.notification_page(
        second["next_cursor"], 50, first["upper_cursor"], first["generation"]
    )
    all_items = first["items"] + second["items"] + last["items"]
    assert [item["sequence"] for item in all_items] == list(range(1, 116))
    assert len({item["notification_id"] for item in all_items}) == 115
    assert not last["has_more"] and last["next_cursor"] == 115
    new_page = restarted.notification_page(115, 50, generation=first["generation"])
    assert [item["rule_id"] for item in new_page["items"]] == ["new-risk"]
    empty = restarted.notification_page(116, 50, generation=first["generation"])
    assert empty["next_cursor"] == 116 and not empty["has_more"]


def test_alert_identity_and_notification_identity_are_distinct_even_same_timestamp(
    store, settings, now
):
    snap = snapshot(now)
    store.save_snapshot(snap)
    signal = signal_for(snap, now, kind=SignalKind.WATCH)
    watch = publish_alert(store, signal, None, now, settings)
    assert store.notification_page(0, 50)["items"] == []
    signal.kind = SignalKind.ENTRY_CANDIDATE
    signal.evidence_groups = ["price", "flow", "oi"]
    signal.reference_price = 101
    strong = publish_alert(store, signal, None, now, settings)
    change_alert(store, strong["id"], "read", now)
    change_alert(store, strong["id"], "pin", now)
    signal.kind = SignalKind.RISK
    signal.reference_price = 102
    risk = publish_alert(store, signal, None, now, settings)
    assert watch["id"] == strong["id"] == risk["id"]
    assert strong["notification_id"] != risk["notification_id"]
    assert strong["notification_revision"] == risk["notification_revision"]
    assert risk["first_price"] == 100 and risk["escalated_price"] == 102
    history = store.notification_page(0, 50)["items"]
    assert [item["notification_price"] for item in history] == [101, 102]
    assert [item["level"] for item in history] == ["STRONG", "CRITICAL_RISK"]
    assert history[0]["notification_price_market_time"] == now.isoformat()
    change_alert(store, risk["id"], "resolve", now)
    reopened = publish_alert(store, signal, None, now, settings)
    assert reopened["id"] != risk["id"]
    expired = publish_alert(store, signal, None, now + timedelta(hours=5), settings)
    assert expired["id"] != reopened["id"]


def test_numeric_ticks_and_reader_actions_do_not_append_notifications(store, settings, now):
    snap = snapshot(now)
    store.save_snapshot(snap)
    signal = signal_for(snap, now)
    initial = publish_alert(store, signal, {"score": 70}, now, settings)
    signal.reference_price = 102
    signal.evidence = ["测试证据 2%"]
    updated = publish_alert(store, signal, {"score": 71}, now + timedelta(seconds=1), settings)
    for action in ("read", "pin", "unpin", "unread", "resolve"):
        change_alert(store, updated["id"], action, now)
    assert updated["notification_id"] == initial["notification_id"]
    assert updated["notification_price"] == 100 and updated["price"] == 102
    assert len(store.notification_page(0, 50)["items"]) == 1


def test_alert_event_and_outbox_writes_roll_back_together(store, settings, now):
    snap = snapshot(now)
    store.save_snapshot(snap)
    signal = signal_for(snap, now, kind=SignalKind.WATCH)
    initial = publish_alert(store, signal, None, now, settings)
    with store.connect() as db:
        db.execute(
            "CREATE TRIGGER reject_notifications BEFORE INSERT ON alert_notifications "
            "BEGIN SELECT RAISE(ABORT,'outbox unavailable'); END"
        )
    signal.kind = SignalKind.RISK
    with pytest.raises(sqlite3.IntegrityError, match="outbox unavailable"):
        publish_alert(store, signal, None, now + timedelta(seconds=1), settings)
    assert store.alert(initial["id"])["level"] == "WATCH"
    assert len(store.alert_events(initial["id"])) == 1
    assert store.notification_page(0, 50)["items"] == []


def test_existing_high_alerts_seed_once_without_inventing_prices(store, settings, now):
    snap = snapshot(now)
    store.save_snapshot(snap)
    alert = publish_alert(store, signal_for(snap, now, kind=SignalKind.RISK), None, now, settings)
    # Recreate the pre-upgrade V4 database shape, with only its current alert payload.
    with store.connect() as db:
        db.execute("DROP TABLE alert_notifications")
        db.execute("DELETE FROM runtime_state WHERE key='alert_notification_feed'")
        legacy = {key: value for key, value in alert.items() if not key.startswith("notification_")}
        legacy.pop("escalated_price")
        db.execute("UPDATE web_alerts SET payload=? WHERE id=?", (json.dumps(legacy), alert["id"]))
    upgraded = Store(settings.database_path)
    first = upgraded.notification_page(0, 50)
    item = first["items"][0]
    assert item["id"] == alert["id"] and item["price_provenance"] == "LEGACY_UNKNOWN"
    assert item["notification_price"] is None and item["escalated_price"] is None
    assert Store(settings.database_path).notification_page(0, 50) == first
    assert upgraded.alert(alert["id"]) == legacy


def test_cursor_rejects_missing_generation_replaced_db_and_restore_rollback(
    store, settings, now, tmp_path
):
    snap = snapshot(now)
    store.save_snapshot(snap)
    publish_alert(store, signal_for(snap, now, kind=SignalKind.RISK), None, now, settings)
    generation = store.notification_page(0, 50)["generation"]
    with pytest.raises(ValueError, match="generation"):
        store.notification_page(1, 50)
    with pytest.raises(ValueError, match="范围"):
        store.notification_page(2, 50, generation=generation)
    other = Store(tmp_path / "replacement.db")
    with pytest.raises(ValueError, match="已更换"):
        other.notification_page(0, 50, generation=generation)


@pytest.mark.parametrize(
    "minutes,price,momentum,status",
    [
        (2, 101, 1, "CONFIRMED"),
        (2, 103, 3, "CONFIRMED"),
        (5, 101, 1, "TOO_LATE"),
        (31, 101, 1, "TOO_LATE"),
        (2, 110, 1, "PRICE_EXTENDED"),
        (2, 101, 10, "PRICE_EXTENDED"),
    ],
)
def test_pre_pump_promotion_has_time_and_price_delivery_guards(
    store, settings, now, minutes, price, momentum, status
):
    first = snapshot(now)
    store.save_snapshot(first)
    published = publish_alert(
        store,
        signal_for(first, now, rule="pre-pump-fusion", kind=SignalKind.WATCH),
        None,
        now,
        settings,
    )
    at = now + timedelta(minutes=minutes)
    current = snapshot(at, price=price)
    current.features.return_15m_pct = momentum
    store.save_snapshot(current)
    signal = signal_for(current, at, rule="pre-pump-fusion")
    result = publish_alert(store, signal, None, at, settings)
    assert result["id"] == published["id"] and result["confirmation_status"] == status
    assert result["level"] == ("STRONG" if status == "CONFIRMED" else "SETUP")
    assert signal.kind == SignalKind.ENTRY_CANDIDATE and signal.contradictions == []
    assert bool(store.notification_page(0, 50)["items"]) == (status == "CONFIRMED")
    # Risk warnings stay deliverable, even when the bullish setup is too late.
    signal.kind = SignalKind.RISK
    assert publish_alert(store, signal, None, at, settings)["level"] == "CRITICAL_RISK"


def make_app(settings):
    return create_app(
        settings,
        providers_factory=lambda c, s: Providers(
            c, s, transport=httpx.MockTransport(lambda request: httpx.Response(500))
        ),
    )


def test_alert_query_is_bounded_and_btc_context_is_computed_once(settings, store, now, monkeypatch):
    snap = snapshot(now)
    store.save_snapshot(snap)
    for i in range(120):
        publish_alert(store, signal_for(snap, now, rule=f"rule-{i}"), None, now, settings)
    risk = publish_alert(
        store, signal_for(snap, now, rule="risk", kind=SignalKind.RISK), None, now, settings
    )
    app = make_app(settings)
    monkeypatch.setattr("muse_btc.api.utc_now", lambda: now)
    calls = []
    monkeypatch.setattr(
        "muse_btc.api.apply_btc_context", lambda regime, *args: calls.append(1) or regime
    )
    # Any per-alert query or full history scan would fail this regression.
    original_alerts = app.state.store.alerts

    def bounded_alerts(**kwargs):
        assert kwargs.get("limit") is not None
        return original_alerts(**kwargs)

    monkeypatch.setattr(app.state.store, "alerts", bounded_alerts)
    monkeypatch.setattr(
        app.state.store, "snapshot", lambda *args: pytest.fail("per-alert snapshot query")
    )
    with TestClient(app) as client:
        assert len(client.get("/api/alerts?limit=50").json()) == 50
        assert len(calls) == 1
        calls.clear()
        page = client.get("/api/alerts/notifications?limit=50").json()
        assert len(page["items"]) == 50 and page["has_more"] and len(calls) == 1
        filtered = client.get("/api/alerts", params={"limit": 1, "level": "CRITICAL_RISK"}).json()
        assert [a["id"] for a in filtered] == [risk["id"]]
        assert len(client.get("/api/alerts?limit=50&offset=50").json()) == 50
        assert (
            client.get(
                "/api/alerts", params={"since": (now + timedelta(seconds=1)).isoformat()}
            ).json()
            == []
        )
        assert (
            len(client.get("/api/alerts", params={"since": now.isoformat(), "limit": 300}).json())
            == 121
        )
        assert client.get("/api/alerts?limit=0").status_code == 422
        assert client.get("/api/alerts?since=2025-01-01T00:00:00").status_code == 422
        assert client.get("/api/alerts/notifications?after=1").status_code == 409


@pytest.mark.parametrize(
    "later_seconds,current_price,action,expected",
    [
        (60, 101, None, "READY"),
        (60, 103, None, "READY"),
        (60, 110, None, "SKIP_PRICE_EXTENDED"),
        (300, 101, None, "SKIP_EXPIRED"),
        (301, 101, None, "SKIP_EXPIRED"),
        (60, 101, "resolve", "SKIP_RESOLVED"),
    ],
)
def test_delivery_eligibility_checks_current_quote_and_expiry(
    settings, store, now, monkeypatch, later_seconds, current_price, action, expected
):
    settings.enable_intelligence = False
    first = snapshot(now)
    store.save_snapshot(first)
    initial = publish_alert(
        store, signal_for(first, now, rule="pre-pump-fusion"), None, now, settings
    )
    if action:
        change_alert(store, initial["id"], action, now)
    at = now + timedelta(seconds=later_seconds)
    store.save_snapshot(snapshot(at, price=current_price))
    app = make_app(settings)
    monkeypatch.setattr("muse_btc.api.utc_now", lambda: at)
    with TestClient(app) as client:
        item = client.get("/api/alerts/notifications").json()["items"][0]
        assert item["delivery_status"] == expected
        assert item["notification_price"] == 100 and item["current_price"] == current_price
        assert item["price_change_since_notification_pct"] == pytest.approx(current_price - 100)
        assert item["notification_age_seconds"] == later_seconds
        view = client.get("/api/alerts").json()[0]
        if expected == "READY":
            assert view["state"] == "ACTIVE"
        elif expected == "SKIP_RESOLVED":
            assert view["state"] == "RESOLVED"
        else:
            assert view["state"] == "PAUSED" and not view["unread"]
            assert view["delivery_guard"] == expected
        if later_seconds == 300:
            assert view["data_current"]  # Expiry pauses even while both snapshots are fresh.


def test_feed_keeps_superseded_history_and_is_authenticated(settings, store, now, monkeypatch):
    settings.api_token = "alert-test-token"
    snap = snapshot(now)
    store.save_snapshot(snap)
    signal = signal_for(snap, now)
    first = publish_alert(store, signal, None, now, settings)
    signal.kind = SignalKind.RISK
    current = publish_alert(store, signal, None, now, settings)
    app = make_app(settings)
    monkeypatch.setattr("muse_btc.api.utc_now", lambda: now)
    with TestClient(app) as client:
        assert client.get("/api/alerts/notifications").status_code == 401
        items = client.get(
            "/api/alerts/notifications", headers={"Authorization": "Bearer alert-test-token"}
        ).json()["items"]
        assert [item["notification_id"] for item in items] == [
            first["notification_id"],
            current["notification_id"],
        ]
        assert [item["delivery_status"] for item in items] == ["SKIP_SUPERSEDED", "READY"]


def test_strong_delivery_rejects_a_newer_quote_with_bad_evidence(settings, store, now, monkeypatch):
    settings.enable_intelligence = False
    first = snapshot(now)
    store.save_snapshot(first)
    publish_alert(store, signal_for(first, now), None, now, settings)
    at = now + timedelta(seconds=60)
    store.save_snapshot(snapshot(at, quality_issues=["INSUFFICIENT_CANDLE_HISTORY"], candles=[]))
    app = make_app(settings)
    monkeypatch.setattr("muse_btc.api.utc_now", lambda: at)
    with TestClient(app) as client:
        item = client.get("/api/alerts/notifications").json()["items"][0]
        assert item["current_price"] == 100
        assert not item["data_current"] and item["delivery_status"] == "SKIP_STALE_DATA"


def test_scoped_latest_snapshots_keep_point_in_time_and_tie_order(store, now):
    old = snapshot(now)
    tie = snapshot(now, price=101)
    future = snapshot(now + timedelta(seconds=1), price=102)
    other = snapshot(now, asset_id="binance:OTHERUSDT", symbol="OTHERUSDT")
    for snap in (old, tie, future, other):
        store.save_snapshot(snap)
    assert [s.id for s in store.latest_snapshots(now, asset_ids=[old.asset_id])] == [tie.id]
    assert [
        s.id for s in store.latest_snapshots(future.available_at, asset_ids=[old.asset_id])
    ] == [future.id]
    assert store.latest_snapshots(now, asset_ids=["missing"]) == []
    assert store.latest_snapshots(now, asset_ids=[]) == []


@pytest.mark.asyncio
async def test_alert_queries_respond_during_fetching_and_a_wal_writer(
    settings, store, now, monkeypatch
):
    settings.enable_intelligence = False
    snap = snapshot(now)
    store.save_snapshot(snap)
    for i in range(300):
        publish_alert(
            store,
            signal_for(snap, now, rule=f"rule-{i}", kind=SignalKind.RISK),
            None,
            now,
            settings,
        )
    app = make_app(settings)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_fetch():
        entered.set()
        await release.wait()
        return []

    monkeypatch.setattr(app.state.collector.providers, "binance", slow_fetch)
    monkeypatch.setattr("muse_btc.api.utc_now", lambda: now)
    async with app.router.lifespan_context(app):
        collecting = asyncio.create_task(app.state.collector.collect_once())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert app.state.collector.progress()["phase"] == "FETCHING"
            with sqlite3.connect(settings.database_path) as writer:
                writer.execute("BEGIN IMMEDIATE")
                writer.execute("INSERT INTO runtime_state VALUES ('uncommitted-writer','{}')")
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    alerts, notifications = await asyncio.wait_for(
                        asyncio.gather(
                            client.get("/api/alerts?limit=50"),
                            client.get("/api/alerts/notifications?limit=50"),
                        ),
                        1,
                    )
                writer.rollback()
            assert len(alerts.json()) == 50
            assert len(notifications.json()["items"]) == 50
            assert notifications.json()["has_more"]
            assert not collecting.done()
        finally:
            release.set()
            await collecting
