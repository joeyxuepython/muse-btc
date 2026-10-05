"""Verify rule wiring, point-in-time explanations and meaningful-change delivery."""

from copy import deepcopy
from datetime import datetime, timedelta

import pytest
from conftest import snapshot
from fastapi.testclient import TestClient
from test_alert_delivery import signal_for
from test_strategy_observability import mvrv_history, options_record

from muse_btc.alerts import publish_alert
from muse_btc.api import create_app
from muse_btc.btc_intelligence import apply_btc_context
from muse_btc.context import asset_context
from muse_btc.decisions import decide
from muse_btc.delivery import build_deliveries, notification_message
from muse_btc.intelligence import EvidenceRecord, IntelligenceStore
from muse_btc.models import Module, SignalKind
from muse_btc.rules import market_regime
from muse_btc.service import Collector
from muse_btc.strategy_audit import attach_publication
from muse_btc.validation import replay


def market(store, settings, now, *, fusion=False):
    times = {key: now for key in ("candles", "funding", "oi_history", "book", "oi", "mark")}
    btc = snapshot(now, component_times=times)
    alt = snapshot(
        now, Module.ALT, asset_id="binance:TIAUSDT", symbol="TIAUSDT", component_times=times
    )
    if fusion:
        alt.features.return_15m_pct = 0.2
        alt.features.oi_change_5m_pct = 1.6
        alt.features.spot_perp_structure = "SPOT_LED_HYPOTHESIS"
    for snap in (btc, alt):
        store.save_snapshot(snap)
    regime = apply_btc_context(market_regime(btc, now, settings), store, settings, now, btc)
    return alt, regime


def catalyst(store, now, asset_id, *, direction="NEGATIVE", verified=True, age=0, available=None):
    return IntelligenceStore(store).save(
        EvidenceRecord(
            kind="catalyst",
            key=asset_id + ":event",
            source="LOCAL_IMPORT",
            market_time=now,
            available_at=available or now,
            data={
                "asset_id": asset_id,
                "title": "可核验测试催化事件",
                "event_time": (now - timedelta(days=age)).isoformat(),
                "category": "other",
                "direction": direction,
                "verified": verified,
                "confidence": 0.8,
                "source_url": "https://example.com/source",
            },
        )
    )


def by_rule(signals, rule="spot-led-momentum"):
    return next(s for s in signals if s.rule_id == rule)


def test_explanation_records_real_checks_and_missing_background(store, settings, now):
    alt, regime = market(store, settings, now)
    trace = []
    signal = by_rule(decide(alt, regime, now, settings, store, trace))
    assert signal.kind == SignalKind.ENTRY_CANDIDATE
    d = signal.decision
    assert d["version"] == "decision-explanation-v1" and d["snapshot_id"] == alt.id
    gates = {g["id"]: g["status"] for g in d["gates"]}
    assert gates["derivatives_cool"] == gates["time_alignment"] == "PASS"
    assert gates["asset_context"] == "UNKNOWN"
    assert len(d["evaluations"]) == len(trace)
    assert any(g["field"] == "ETF 资金流" for g in d["data_gaps"])
    assert "未参与本候选" in d["not_integrated"][0]
    alert = publish_alert(store, signal, {"score": 80}, now, settings)
    alert.update(current_price=100, current_price_market_time=now.isoformat())
    text = notification_message([alert], "LONG")
    assert "Funding 0.01%" in text and "OI 5 分钟变化 0.5%" in text and "价差 2 bps" in text
    assert "已通过" in text and "未全面核实" in text and "缺失/不可用" in text
    assert d["coverage"]["scope"] == "THIS_RULE_AND_MATCHED_PATTERNS"


def test_known_negative_context_blocks_both_rules_and_is_reported(store, settings, now):
    alt, regime = market(store, settings, now, fusion=True)
    event = catalyst(store, now, alt.asset_id)
    trace = []
    signals = decide(alt, regime, now, settings, store, trace)
    for rule in ("spot-led-momentum", "pre-pump-fusion"):
        signal = by_rule(signals, rule)
        assert signal.kind == SignalKind.WATCH and signal.entry_zone is None
        assert event.id in signal.decision["asset_context"]["records"]
        assert any(
            g["id"] == "asset_context" and g["status"] == "BLOCKED"
            for g in signal.decision["gates"]
        )
        alert = publish_alert(store, signal, None, now, settings)
        attach_publication(trace, signal, alert, regime=regime)
        assert alert["level"] != "STRONG"
    assert all(
        r["publication"] == "BLOCKED_CONTEXT"
        for r in trace
        if r["rule_id"] in ("spot-led-momentum", "pre-pump-fusion")
    )
    assert not store.notification_page(0, 50)["items"]
    replayed = replay(store, now - timedelta(seconds=1), now, settings)["signals"]
    assert all(s["kind"] == "WATCH" for s in replayed if s["asset_id"] == alt.asset_id)


@pytest.mark.parametrize("case", ["unverified", "expired", "future", "positive"])
def test_unusable_or_positive_catalysts_do_not_fake_a_veto(store, settings, now, case):
    alt, regime = market(store, settings, now)
    event = catalyst(
        store,
        now,
        alt.asset_id,
        verified=case != "unverified",
        age=8 if case == "expired" else 0,
        available=now + timedelta(seconds=1) if case == "future" else now,
        direction="POSITIVE" if case == "positive" else "NEGATIVE",
    )
    signal = by_rule(decide(alt, regime, now, settings, store))
    assert signal.kind == SignalKind.ENTRY_CANDIDATE
    ctx = signal.decision["asset_context"]
    assert not ctx["risks"]
    if case == "future":
        assert event.id not in ctx["records"]
    elif case in ("expired", "unverified"):
        assert "catalyst" in ctx["missing"]
    else:
        assert "catalyst" not in signal.evidence_groups  # Positive catalyst supports fusion only.


@pytest.mark.parametrize("component", ["funding", "oi_history", "book"])
def test_stale_components_are_not_reported_as_passing(store, settings, now, component):
    alt, regime = market(store, settings, now)
    alt.component_times[component] = now - timedelta(seconds=1200)
    signal = by_rule(decide(alt, regime, now, settings, store))
    assert signal.kind == SignalKind.WATCH
    assert any(
        g["component"] == component and g["status"] == "STALE" for g in signal.decision["data_gaps"]
    )
    assert any(g["status"] == "BLOCKED" for g in signal.decision["gates"])


def test_research_is_background_and_frozen_notification_is_not_rewritten(store, settings, now):
    mvrv_history(store, now)
    original_option = options_record(store, now, iv=90)
    alt, regime = market(store, settings, now)
    signal = by_rule(decide(alt, regime, now, settings, store))
    original = publish_alert(store, signal, {"score": 80}, now, settings)
    frozen = deepcopy(store.notification_page(0, 50)["items"][0])
    for rule in ("btc-mvrv-elevated", "btc-options-volatility"):
        item = next(b for b in signal.decision["background"] if b["id"] == rule)
        assert item["role"] == "BACKGROUND_ONLY" and item["status"] == "TRIGGERED_WATCH"
    assert any(
        r["id"] == original_option.id
        for b in signal.decision["background"]
        for r in b.get("sources", [])
    )
    later = now + timedelta(seconds=1)
    options_record(store, later, iv=95)
    alt2, regime2 = market(store, settings, later)
    alt2.features.funding_rate_pct = 0.012
    alt2.features.relative_volume = 2.6
    signal2 = by_rule(decide(alt2, regime2, later, settings, store))
    updated = publish_alert(store, signal2, {"score": 81}, later, settings)
    assert original["notification_id"] == updated["notification_id"]
    assert updated["decision"]["as_of"] == later.isoformat()
    assert store.notification_page(0, 50)["items"][0] == frozen
    iv = next(b for b in frozen["decision"]["background"] if b["id"] == "btc-options-volatility")
    assert iv["inputs"]["median_atm_iv_pct"] == 90


def test_new_context_blocks_queued_opportunity_before_next_collection(
    store, settings, now, monkeypatch
):
    alt, regime = market(store, settings, now)
    signal = by_rule(decide(alt, regime, now, settings, store))
    store.save_signal(signal)
    published = publish_alert(store, signal, None, now, settings)
    later = now + timedelta(seconds=1)
    alt = alt.model_copy(
        update={"id": "risk-snapshot", "market_time": later, "available_at": later}, deep=True
    )
    alt.features.return_15m_pct = -2
    alt.features.spot_taker_buy_ratio = 0.4
    store.save_snapshot(alt)
    risk = signal_for(alt, later, rule="spot-sell-pressure", kind=SignalKind.RISK)
    store.save_signal(risk)
    publish_alert(store, risk, None, later, settings)
    catalyst(store, later, alt.asset_id)
    monkeypatch.setattr("muse_btc.api.utc_now", lambda: later)
    with TestClient(create_app(settings)) as client:
        page = client.get("/api/alerts/notifications").json()
        live = client.get("/api/alerts/" + published["id"]).json()["alert"]
        assert (
            client.get("/health").json()["decision_explanation_version"]
            == "decision-explanation-v1"
        )
    opportunity = next(a for a in page["items"] if a["rule_id"] == "spot-led-momentum")
    assert opportunity["delivery_status"] == "SKIP_CONTEXT_BLOCKED"
    assert not opportunity["decision"]["asset_context"]["risks"]
    assert live["state"] == "PAUSED" and live["message_zh"] is None
    assert (
        next(a for a in page["items"] if a["rule_id"] == "spot-sell-pressure")["delivery_status"]
        == "READY"
    )


def test_grouped_decisions_keep_members_and_deduplicate_common_context(store, settings, now):
    alt, regime = market(store, settings, now, fusion=True)
    signals = decide(alt, regime, now, settings, store)
    rows = []
    for signal in signals:
        if signal.kind == SignalKind.ENTRY_CANDIDATE:
            row = publish_alert(store, signal, None, now, settings)
            row.update(
                delivery_status="READY",
                current_price=100,
                current_price_market_time=now.isoformat(),
            )
            rows.append(row)
    group = build_deliveries(rows)[0]
    assert len(group["member_notification_ids"]) == len(group["decision_explanations"]) == 2
    assert group["message_zh"].count("BTC 背景：") == 1
    assert group["message_zh"].count("机会排行只用于排序") == 1
    assert group["independent_strategy_count"] is None
    assert "pre-pump:" in str(group["decision_explanations"])


def test_lifecycle_does_not_reuse_original_bullish_decision(store, settings, now):
    alt, regime = market(store, settings, now)
    parent = by_rule(decide(alt, regime, now, settings, store))
    store.save_signal(parent)
    publish_alert(store, parent, None, now, settings)
    later = now + timedelta(seconds=10)
    current = alt.model_copy(
        update={"id": "later", "price": 97, "market_time": later, "available_at": later}
    )
    store.save_snapshot(current)
    Collector(settings, store, None)._process_signals([current], regime, later)
    children = [s for s in store.signals(limit=100) if s.kind == SignalKind.INVALIDATED]
    assert len(children) == 1
    d = children[0].decision
    assert d["role"] == "LIFECYCLE" and d["parent_signal_id"] == parent.id
    assert d["parent_decision_as_of"] == parent.decision["as_of"] and "triggered" not in d


def test_legacy_and_disabled_context_remain_explicitly_unknown(store, settings, now):
    alt, regime = market(store, settings, now)
    legacy = publish_alert(store, signal_for(alt, now), None, now, settings)
    legacy.update(current_price=100, current_price_market_time=now.isoformat())
    assert "旧记录未保存完整检查结果" in notification_message([legacy], "LONG")
    catalyst(store, now, alt.asset_id)
    cache = asset_context(IntelligenceStore(store), alt.asset_id, now)
    settings.enable_intelligence = False
    signal = by_rule(decide(alt, regime, now, settings, store, context=cache))
    assert signal.kind == SignalKind.ENTRY_CANDIDATE
    assert signal.decision["asset_context"] == {}
    assert any(g["status"] == "DISABLED" for g in signal.decision["gates"])


@pytest.mark.parametrize("risk", ["unlock", "fdv"])
def test_existing_tokenomics_veto_reaches_momentum(store, settings, now, risk):
    alt, regime = market(store, settings, now)
    IntelligenceStore(store).save(
        EvidenceRecord(
            kind="tokenomics",
            key=alt.asset_id,
            source="LOCAL_IMPORT",
            market_time=now,
            available_at=now,
            data={
                "asset_id": alt.asset_id,
                "unlock_time": (now + timedelta(days=2)).isoformat(),
                "unlock_pct_circulating": 5 if risk == "unlock" else 0,
                "fdv_usd": 1100,
                "market_cap_usd": 100 if risk == "fdv" else 1000,
                "source_url": "https://example.com/tokenomics",
            },
        )
    )
    signal = by_rule(decide(alt, regime, now, settings, store))
    assert signal.kind == SignalKind.WATCH and signal.decision["asset_context"]["risks"]
    item = signal.decision["asset_context"]["evaluations"][0]
    assert (
        item["status"] == "BLOCKED" and datetime.fromisoformat(item["source"]["market_time"]) == now
    )


def test_alignment_and_competing_rule_reasons_match_actual_decision(store, settings, now):
    alt, regime = market(store, settings, now)
    alt.component_times["oi"] = now - timedelta(seconds=121)
    signal = by_rule(decide(alt, regime, now, settings, store))
    assert signal.kind == SignalKind.WATCH
    assert (
        next(g for g in signal.decision["gates"] if g["id"] == "time_alignment")["status"]
        == "BLOCKED"
    )
    alt.component_times["oi"] = now
    alt.features.oi_change_5m_pct = -1
    alt.features.funding_rate_pct = -0.01
    signals = decide(alt, regime, now, settings, store)
    assert by_rule(signals, "squeeze-candidate").kind == SignalKind.WATCH
    signal = by_rule(signals)
    assert signal.kind == SignalKind.WATCH
    assert (
        next(g for g in signal.decision["gates"] if g["id"] == "no_competing_signal")["status"]
        == "BLOCKED"
    )
    assert any("其他基础规则" in c for c in signal.contradictions)


@pytest.mark.parametrize("sent", [False, True])
def test_new_asset_veto_cancels_sent_candidate_without_spamming_unsent(store, settings, now, sent):
    alt, regime = market(store, settings, now)
    parent = by_rule(decide(alt, regime, now, settings, store))
    store.save_signal(parent)
    alert = publish_alert(store, parent, None, now, settings)
    if sent:
        store.save_notification_receipt([alert["notification_id"]], "SENT", "fixture", None, now)
    later = now + timedelta(seconds=10)
    catalyst(store, later, alt.asset_id)
    current = alt.model_copy(
        update={"id": "veto-snapshot", "market_time": later, "available_at": later}
    )
    store.save_snapshot(current)
    Collector(settings, store, None)._process_signals([current], regime, later)
    cancelled = next(s for s in store.signals(limit=100) if s.kind == SignalKind.INVALIDATED)
    assert "资产背景风险" in cancelled.decision["reason"]
    assert cancelled.decision["asset_context"]["risks"]
    assert store.signal_events(parent.id)[-1].state == "INVALIDATED"
    # The existing sender still requires a SENT receipt before delivering the cancellation.
    from muse_btc.alerts import notification_projections

    child = next(
        a
        for a in store.notification_page(0, 50)["items"]
        if a["notification_class"] == "CANCELLATION"
    )
    projected = notification_projections(store, [child], later)[0]
    assert projected["cancellation_delivery_status"] == ("READY" if sent else "WAIT_PARENT_RECEIPT")


def test_wrong_time_context_cache_is_recomputed(store, settings, now):
    alt, regime = market(store, settings, now)
    later = now + timedelta(seconds=1)
    catalyst(store, later, alt.asset_id)
    cache = asset_context(IntelligenceStore(store), alt.asset_id, later)
    assert cache["risks"]
    signal = by_rule(decide(alt, regime, now, settings, store, context=cache))
    assert (
        signal.kind == SignalKind.ENTRY_CANDIDATE and not signal.decision["asset_context"]["risks"]
    )


def test_macro_veto_explains_actual_usd_units_and_keeps_source_times(store, settings, now):
    archive = IntelligenceStore(store)
    records = []
    for key, value, previous in (
        ("WALCL", 7000000, 7100000),
        ("WTREGEN", 500000, 500000),
        ("RRPONTSYD", 100, 101),
    ):
        records.append(
            archive.save(
                EvidenceRecord(
                    kind="macro",
                    key=key,
                    source="FRED",
                    market_time=now,
                    available_at=now,
                    data={
                        "value": value,
                        "previous": previous,
                        "source_url": "https://example.com/fred",
                    },
                )
            )
        )
    for i in range(5):
        records.append(
            archive.save(
                EvidenceRecord(
                    kind="etf",
                    key=f"BTC:{i}",
                    source="Farside",
                    market_time=now - timedelta(days=i),
                    available_at=now,
                    data={
                        "asset": "BTC",
                        "net_flow_usd": -100,
                        "source_url": "https://example.com/etf",
                    },
                )
            )
        )
    alt, regime = market(store, settings, now)
    signal = by_rule(decide(alt, regime, now, settings, store))
    assert signal.kind == SignalKind.WATCH and regime.risk_mode == "CAUTION"
    macro = next(b for b in signal.decision["background"] if b["id"] == "btc-macro-stress")
    # -100,000 millions plus a 1 billion decline in RRP: -99 billion USD, not -99,000.
    assert macro["inputs"]["net_liquidity_change_usd"] == -99000000000
    assert macro["role"] == "MACRO_GATE_CONTEXT" and len(macro["sources"]) == 8
    assert {r["id"] for r in macro["sources"]} == {r.id for r in records}
    assert all(datetime.fromisoformat(r["available_at"]) <= now for r in macro["sources"])
    assert (
        next(g for g in signal.decision["gates"] if g["id"] == "btc_regime")["status"] == "BLOCKED"
    )
