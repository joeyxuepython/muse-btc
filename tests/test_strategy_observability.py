"""Offline regressions for score meaning, strategy coverage and delivery integration."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import httpx
import pytest
from conftest import snapshot
from fastapi.testclient import TestClient
from test_alert_delivery import signal_for

from muse_btc.alerts import public_scores, publish_alert
from muse_btc.api import create_app
from muse_btc.btc_intelligence import apply_btc_context, btc_assessment
from muse_btc.context_observations import mvrv_window, option_sample
from muse_btc.decisions import decide
from muse_btc.delivery import build_deliveries, fetch_notification_batch
from muse_btc.intelligence import EvidenceRecord, IntelligenceStore
from muse_btc.models import Module, SignalKind
from muse_btc.rules import market_regime
from muse_btc.service import Collector
from muse_btc.storage import Store
from muse_btc.strategy_audit import report
from muse_btc.validation import measure_signal, replay


def evidence(store, now, metric, value, *, age=0, source="Coin Metrics", available=None, key=None):
    return IntelligenceStore(store).save(
        EvidenceRecord(
            kind="onchain",
            key=key or f"{source}:{metric}:{age}",
            source=source,
            market_time=now - timedelta(days=age),
            available_at=available or now,
            data={"metric": metric, "value": value, "source_url": "https://example.com/data"},
        )
    )


def mvrv_history(store, now, count=30):
    return [evidence(store, now, "mvrv", count - age, age=age) for age in range(count)]


def options_record(store, now, iv=90, age=0):
    chain = [
        {
            "instrument": f"BTC-{day}-{kind}",
            "expiry": (now + timedelta(days=day)).isoformat(),
            "strike_usd": 100,
            "underlying_price_usd": 100,
            "option_type": kind,
            "mark_iv_pct": iv,
        }
        for day in (7, 14)
        for kind in ("call", "put")
    ]
    return IntelligenceStore(store).save(
        EvidenceRecord(
            kind="options",
            key="BTC:chain",
            source="Deribit",
            market_time=now - timedelta(seconds=age),
            available_at=now,
            data={"chain": chain, "source_url": "https://example.com/options"},
        )
    )


def test_rule_scores_separate_from_opportunity_and_risk(store, settings, now):
    snap = snapshot(now)
    store.save_snapshot(snap)
    alerts = []
    for rule, score in (("momentum", 78), ("fusion", 60)):
        signal = signal_for(snap, now, rule=rule)
        signal.evidence_score = score
        alerts.append(publish_alert(store, signal, {"score": 81.2}, now, settings))
    assert [a["opportunity_score"] for a in alerts] == [81.2, 81.2]
    assert [a["score"] for a in alerts] == [78, 60]
    risk = publish_alert(
        store,
        signal_for(snap, now, rule="risk", kind=SignalKind.RISK),
        {"score": 22.6},
        now,
        settings,
    )
    assert risk["risk_level"] == "CRITICAL_RISK" and risk["score"] is None
    assert risk["rule_evidence_score"] is None
    invalid = publish_alert(
        store,
        signal_for(snap, now, rule="invalid", kind=SignalKind.INVALIDATED),
        None,
        now,
        settings,
    )
    assert invalid["score"] is None and invalid["rule_evidence_score"] is None
    assert invalid["original_signal_evidence_score"] == 70


def test_legacy_scores_are_unknown_and_migration_does_not_mix_scales(store, settings, now):
    snap = snapshot(now)
    store.save_snapshot(snap)
    signal = signal_for(snap, now)
    alert = publish_alert(store, signal, {"score": 81.2}, now, settings)
    alert.pop("score_schema")
    alert.update(score=81.2, max_score=81.2, notification_score=81.2)
    store.save_alert(alert)
    projected = public_scores(alert)
    assert projected["score"] is None and projected["rule_evidence_score"] is None
    assert projected["legacy_score"] == 81.2
    new = publish_alert(store, signal, {"score": 81.2}, now, settings)
    assert new["score"] == new["max_score"] == 70 and new["score_delta"] is None


def test_zero_ranking_does_not_make_a_notification_on_every_tick(store, settings, now):
    snap = snapshot(now)
    store.save_snapshot(snap)
    signal = signal_for(snap, now)
    first = publish_alert(store, signal, {"score": 0}, now, settings)
    second = publish_alert(store, signal, {"score": 0}, now + timedelta(seconds=1), settings)
    assert first["notification_id"] == second["notification_id"]


async def test_grouping_drains_frozen_pages_and_keeps_risks_separate(
    store, settings, now, monkeypatch
):
    snap = snapshot(now)
    store.save_snapshot(snap)
    for rule, kind in (
        ("momentum", SignalKind.ENTRY_CANDIDATE),
        ("fusion", SignalKind.ENTRY_CANDIDATE),
        ("sell", SignalKind.RISK),
    ):
        signal = signal_for(snap, now, rule=rule, kind=kind)
        signal.patterns = ["E: 相对 BTC 轮动"] if rule == "fusion" else []
        publish_alert(store, signal, {"score": 81.2}, now, settings)
    monkeypatch.setattr("muse_btc.api.utc_now", lambda: now)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            batch = await fetch_notification_batch(client, page_size=1)
    assert batch["next_cursor"] == 3 and len(batch["items"]) == 3
    assert len(batch["delivery_groups"]) == 2
    long = next(g for g in batch["delivery_groups"] if g["category"] == "LONG")
    assert len(long["member_notification_ids"]) == 2
    assert long["opportunity_score"] == 81.2 and long["independent_strategy_count"] is None
    assert long["patterns"] == ["E: 相对 BTC 轮动"]
    assert len(build_deliveries(batch["items"] + batch["items"])) == 2


def test_receipts_are_atomic_persistent_and_never_regress_sent(store, settings, now):
    snap = snapshot(now)
    store.save_snapshot(snap)
    alert = publish_alert(store, signal_for(snap, now), None, now, settings)
    ids = [alert["notification_id"]]
    with pytest.raises(ValueError):
        store.save_notification_receipt(ids + ["unknown"], "SENT", "mail-1", None, now)
    assert store.notification_receipts(ids) == {}
    store.save_notification_receipt(ids, "FAILED", None, "timeout", now)
    store.save_notification_receipt(ids, "SENT", "mail-1", None, now)
    store.save_notification_receipt(ids, "FAILED", None, "late failure", now)
    receipt = Store(settings.database_path).notification_receipts(ids)[ids[0]]
    assert receipt["status"] == "SENT" and receipt["message_id"] == "mail-1"
    assert receipt["source"] == "MUSE_REPORTED"
    item = alert | {"delivery_status": "READY", "receipt": receipt}
    assert build_deliveries([item]) == []


def test_concurrent_late_failure_cannot_overwrite_sent(store, settings, now):
    snap = snapshot(now)
    store.save_snapshot(snap)
    alert = publish_alert(store, signal_for(snap, now), None, now, settings)
    ids = [alert["notification_id"]]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda status: store.save_notification_receipt(ids, status, "mail", None, now),
                ["SENT"] + ["FAILED"] * 7,
            )
        )
    assert store.notification_receipts(ids)[ids[0]]["status"] == "SENT"


def test_receipt_api_validation_and_authentication(settings, store, now, monkeypatch):
    settings.api_token = "test-secret"
    snap = snapshot(now)
    store.save_snapshot(snap)
    alert = publish_alert(store, signal_for(snap, now), None, now, settings)
    monkeypatch.setattr("muse_btc.api.utc_now", lambda: now)
    with TestClient(create_app(settings)) as client:
        payload = {
            "notification_ids": [alert["notification_id"]],
            "status": "SENT",
            "message_id": "mail",
        }
        assert client.post("/api/alerts/notifications/receipts", json=payload).status_code == 401
        headers = {"Authorization": "Bearer test-secret"}
        assert (
            client.post(
                "/api/alerts/notifications/receipts", json=payload, headers=headers
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/alerts/notifications/receipts",
                json=payload | {"status": "FAKE"},
                headers=headers,
            ).status_code
            == 422
        )


def test_live_audit_includes_missing_not_triggered_disabled_and_delivery(store, settings, now):
    btc = snapshot(now)
    btc.features.funding_rate_pct = None
    store.save_snapshot(btc)
    settings.enable_intelligence = False
    collector = Collector(settings, store, None)
    collector._process_signals([btc], market_regime(btc, now, settings), now)
    rows = {r["rule_id"]: r for r in report(store, settings, now)["evaluations"]}
    assert rows["leverage-overheat"]["status"] == "MISSING_DATA"
    assert rows["spot-sell-pressure"]["status"] == "NOT_TRIGGERED"
    assert rows["pre-pump-fusion"]["status"] == "DISABLED"
    assert rows["btc-options-volatility"]["status"] == "DISABLED"
    assert rows["spot-led-momentum"]["delivery"] == "BELOW_DELIVERY_LEVEL"
    assert "funding_rate_pct" in rows["spot-led-momentum"]["missing"]


def test_fusion_patterns_and_confirmation_block_are_audited(store, settings, now):
    btc = snapshot(now)
    btc.features.return_15m_pct = 0
    alt = snapshot(now, Module.ALT, asset_id="binance:GIGGLEUSDT", symbol="GIGGLEUSDT")
    alt.features.return_15m_pct = 0
    for snap in (btc, alt):
        store.save_snapshot(snap)
    collector = Collector(settings, store, None)
    regime = market_regime(btc, now, settings)
    collector._process_signals([alt], regime, now)
    result = report(store, settings, now)
    rows = {r["rule_id"]: r for r in result["evaluations"]}
    assert rows["pre-pump:A"]["status"] == rows["pre-pump:E"]["status"] == "MATCHED"
    assert rows["pre-pump:F"]["status"] == "MISSING_DATA"
    assert rows["pre-pump-fusion"]["delivery"] == "AWAITING_RECEIPT"
    alert = store.active_alert(alt.asset_id, "pre-pump-fusion", now)
    assert alert["patterns"] == ["A: 平价放量", "E: 相对 BTC 轮动"]
    # A fresh quote can still refer to an old setup; confirmation remains capped.
    alert["first_seen"] = (now - timedelta(minutes=10)).isoformat()
    store.save_alert(alert)
    collector._process_signals([alt], regime, now + timedelta(seconds=1))
    row = next(
        r
        for r in report(store, settings, now + timedelta(seconds=1))["evaluations"]
        if r["rule_id"] == "pre-pump-fusion"
    )
    assert row["cooldown"] and row["publication"] == "BLOCKED_CONFIRMATION"
    assert row["delivery"] == "BELOW_DELIVERY_LEVEL"


def test_no_receipt_stays_unknown_and_missing_batch_is_explicit(store, settings, now):
    btc = snapshot(now)
    store.save_snapshot(btc)
    collector = Collector(settings, store, None)
    collector._process_signals([btc], market_regime(btc, now, settings), now)
    row = next(
        r
        for r in report(store, settings, now)["evaluations"]
        if r["rule_id"] == "spot-led-momentum"
    )
    assert row["delivery"] == "AWAITING_RECEIPT"
    store.save_notification_receipt([row["notification_id"]], "SENT", "mail", None, now)
    assert (
        next(
            r
            for r in report(store, settings, now)["evaluations"]
            if r["rule_id"] == "spot-led-momentum"
        )["delivery"]
        == "SENT"
    )
    collector._process_signals([], market_regime(None, now, settings), now + timedelta(seconds=1))
    restarted = report(Store(settings.database_path), settings, now + timedelta(seconds=1))
    assert restarted["last_run"]["assets_evaluated"] == 0
    assert all(r["status"] == "NOT_EVALUATED_IN_CURRENT_BATCH" for r in restarted["evaluations"])


def test_lifecycle_only_batch_does_not_claim_market_coverage(store, settings, now):
    btc = snapshot(now)
    store.save_snapshot(btc)
    collector = Collector(settings, store, None)
    collector._process_signals([btc], market_regime(btc, now, settings), now)
    regime = market_regime(None, now, settings)
    regime.risk_mode = "RISK_OFF"
    collector._process_signals([], regime, now + timedelta(seconds=1))
    result = report(store, settings, now + timedelta(seconds=1))
    assert result["last_run"]["assets_evaluated"] == 0
    assert result["last_run"]["lifecycle_events"] == 1
    row = next(r for r in result["evaluations"] if r["role"] == "LIFECYCLE")
    assert store.signal(row["signal_id"]).kind == SignalKind.INVALIDATED


def test_cooldown_without_alert_does_not_reference_an_unarchived_signal(store, settings, now):
    btc = snapshot(now)
    store.save_snapshot(btc)
    collector = Collector(settings, store, None)
    regime = market_regime(btc, now, settings)
    collector._process_signals([btc], regime, now)
    alert = store.active_alert(btc.asset_id, "spot-led-momentum", now)
    alert["resolved_at"] = now.isoformat()
    store.save_alert(alert)
    collector._process_signals([btc], regime, now + timedelta(seconds=1))
    row = next(
        r
        for r in report(store, settings, now + timedelta(seconds=1))["evaluations"]
        if r["rule_id"] == "spot-led-momentum"
    )
    assert row["publication"] == "COOLDOWN" and row["signal_id"] is None
    assert store.signal(row["candidate_id"]) is None


def test_mvrv_uses_unique_known_days_and_rejects_stale_history(store, settings, now):
    history = mvrv_history(store, now)
    revision = history[0].model_copy(
        update={"id": "revision", "available_at": now + timedelta(seconds=1)}
    )
    assert mvrv_window(history + [revision], now)[1] == 100
    assert len(mvrv_window(history + [revision], now + timedelta(seconds=1))[0]) == 30
    assert mvrv_window(history, now + timedelta(days=4))[1] is None
    assert mvrv_window(history[:29], now)[1] is None


def test_future_archive_is_not_visible_to_context(store, settings, now):
    future = now + timedelta(days=1)
    mvrv_history(store, future)
    assert btc_assessment(store, settings, now)["observations"][1]["status"] == "MISSING_DATA"


def test_context_signals_are_watch_only_have_provenance_and_validate(store, settings, now):
    mvrv_history(store, now)
    evidence(store, now, "sopr", 0.9, source="BGeometrics", age=7)
    evidence(store, now, "sth-realized-price", 110, source="BGeometrics", age=7)
    options_record(store, now)
    btc = snapshot(now)
    store.save_snapshot(btc)
    regime = apply_btc_context(market_regime(btc, now, settings), store, settings, now, btc)
    signals = [
        s
        for s in decide(btc, regime, now, settings, store)
        if s.model_version == "context-observation"
    ]
    assert len(signals) == 3 and all(s.kind == SignalKind.WATCH for s in signals)
    for signal in signals:
        alert = publish_alert(store, signal, None, now, settings)
        assert alert["level"] == "WATCH" and alert["rule_evidence_score"] is None
        assert alert["context"]["record_ids"]
        assert all(
            datetime.fromisoformat(r["available_at"]) <= now for r in alert["context"]["sources"]
        )
    assert store.notification_page(0, 50)["items"] == []
    end = snapshot(now + timedelta(hours=24), price=103)
    store.save_snapshot(end)
    outcome = measure_signal(store, signals[0], 86400, end.available_at, settings)
    assert outcome.return_pct == pytest.approx(3) and outcome.paper_net_return_pct is None


@pytest.mark.parametrize("metric_age,basis_age", [(11, 11), (7, 8)])
def test_holder_context_rejects_stale_or_misaligned_days(
    store, settings, now, metric_age, basis_age
):
    evidence(store, now, "sopr", 0.9, source="BGeometrics", age=metric_age)
    evidence(store, now, "sth-realized-price", 110, source="BGeometrics", age=basis_age)
    holder = btc_assessment(store, settings, now, snapshot(now))["observations"][2]
    assert holder["status"] == "MISSING_DATA"


def test_options_filter_wings_expiry_and_coverage(store, settings, now):
    option = options_record(store, now)
    wing = option.data["chain"][0] | {"strike_usd": 1000, "mark_iv_pct": 900}
    expired = option.data["chain"][0] | {"expiry": now.isoformat(), "mark_iv_pct": 900}
    option.data["chain"] += [wing, expired]
    rows, iv = option_sample([option], now, settings)
    assert len(rows) == 4 and iv == 90
    option.data["chain"] = [r for r in option.data["chain"] if r["option_type"] == "call"]
    assert option_sample([option], now, settings)[1] is None


def test_stale_options_do_not_trigger_and_no_promotion_on_many_groups(store, settings, now):
    options_record(store, now, age=601)
    mvrv_history(store, now)
    btc = snapshot(now)
    regime = apply_btc_context(market_regime(btc, now, settings), store, settings, now, btc)
    assert regime.research_context["observations"][3]["status"] == "MISSING_DATA"
    assert all(
        s.kind == SignalKind.WATCH
        for s in decide(btc, regime, now, settings, store)
        if s.rule_id.startswith("btc-")
    )
    settings.enable_context_observations = False
    assert not any(s.rule_id.startswith("btc-") for s in decide(btc, regime, now, settings, store))


def test_macro_observation_preserves_existing_caution_gate(store, settings, now):
    archive = IntelligenceStore(store)
    for key, value, previous in (
        ("WALCL", 7000000, 7100000),
        ("WTREGEN", 500000, 500000),
        ("RRPONTSYD", 100, 100),
    ):
        archive.save(
            EvidenceRecord(
                kind="macro",
                key=key,
                source="FRED",
                market_time=now,
                available_at=now,
                data={"value": value, "previous": previous},
            )
        )
    for i in range(5):
        archive.save(
            EvidenceRecord(
                kind="etf",
                key=f"BTC:{i}",
                source="Farside",
                market_time=now - timedelta(days=i),
                available_at=now,
                data={"net_flow_usd": -100},
            )
        )
    btc = snapshot(now)
    regime = apply_btc_context(market_regime(btc, now, settings), store, settings, now, btc)
    assert regime.risk_mode == "CAUTION"
    signal = next(
        s for s in decide(btc, regime, now, settings, store) if s.rule_id == "btc-macro-stress"
    )
    assert signal.kind == SignalKind.WATCH and len(signal.context["record_ids"]) == 8


def test_replay_reuses_saved_context_config_without_writing_audit(store, settings, now):
    mvrv_history(store, now)
    settings.enable_context_observations = False
    btc = snapshot(now)
    store.save_snapshot(btc)
    store.save_decision_config(now, settings)
    settings.enable_context_observations = True
    output = replay(store, now, now + timedelta(seconds=1), settings)
    assert not any(s["rule_id"].startswith("btc-") for s in output["signals"])
    assert store.strategy_report()[0] is None
