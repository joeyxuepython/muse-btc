"""Deterministic regressions for verified audit failures and cloud worker contracts."""

import asyncio
import json
import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import pytest
from conftest import snapshot
from fastapi.testclient import TestClient

from muse_btc.api import create_app
from muse_btc.btc_intelligence import apply_btc_context, btc_assessment
from muse_btc.config import Settings
from muse_btc.context import import_context
from muse_btc.decisions import decide
from muse_btc.events import EventEngine, calendar_entries, release_values
from muse_btc.intelligence import ContextInput, EvidenceRecord, IntelligenceStore
from muse_btc.macro import MacroEngine
from muse_btc.models import Features, Module
from muse_btc.providers.common import ProviderError
from muse_btc.rules import market_regime
from muse_btc.storage import Store, stamp
from muse_btc.validation import replay
from muse_btc.worker import IntelligenceWorker, runtime_health


def record(at, key="DGS2", value=4):
    return EvidenceRecord(
        kind="macro", key=key, source="TEST", market_time=at, available_at=at, data={"value": value}
    )


def test_successful_unchanged_fetch_preserves_visibility_and_refreshes_health(store, settings, now):
    archive = IntelligenceStore(store)
    first = archive.save(record(now))
    later = now + timedelta(hours=3)
    refreshed = archive.save(record(now).model_copy(update={"available_at": later}))
    assert first.id == refreshed.id and refreshed.available_at == now
    assert archive.last_checked(first, now) == now
    assert archive.last_checked(first, later) == later
    assert "DGS2" not in MacroEngine(store, settings, None).summary(later)["stale"]
    assert "DGS2" in MacroEngine(store, settings, None).summary(later + timedelta(hours=3))["stale"]


def test_revision_returns_to_previous_value_without_erasing_history(store, now):
    archive = IntelligenceStore(store)
    ids = [
        archive.save(record(now + timedelta(hours=i), "revision", value)).id
        for i, value in enumerate([1, 2, 1])
    ]
    assert len(set(ids)) == 3
    for i, expected in enumerate([1, 2, 1]):
        assert (
            archive.records("macro", now + timedelta(hours=i), key="revision")[0].data["value"]
            == expected
        )


def test_real_v3_unique_constraint_migration_preserves_lineage_and_backup(tmp_path, now):
    path = tmp_path / "old.db"
    old = record(now)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE intelligence_records(id TEXT PRIMARY KEY,kind TEXT,key TEXT,"
            "market_time TEXT,available_at TEXT,content_hash TEXT,payload TEXT,"
            "UNIQUE(kind,key,content_hash))"
        )
        from muse_btc.intelligence import digest

        db.execute(
            "INSERT INTO intelligence_records VALUES (?,?,?,?,?,?,?)",
            (
                old.id,
                old.kind,
                old.key,
                stamp(now),
                stamp(now),
                digest(old.data),
                old.model_dump_json(),
            ),
        )
        db.execute("PRAGMA user_version=3")
    upgraded = Store(path)
    assert path.with_suffix(".db.pre-v4.bak").exists()
    archive = IntelligenceStore(upgraded)
    assert archive.by_id(old.id, now).data == old.data
    archive.save(record(now + timedelta(hours=1), value=5))
    archive.save(record(now + timedelta(hours=2), value=4))
    assert archive.records("macro", now + timedelta(hours=2))[0].data["value"] == 4
    with sqlite3.connect(path.with_suffix(".db.pre-v4.bak")) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3


def event(store, now):
    return import_context(
        store,
        ContextInput(
            kind="macro_event",
            key="test",
            source_url="https://example.org/event",
            market_time=now,
            data={
                "name": "TEST",
                "release_time": now.isoformat(),
                "actual": 1,
                "units": "percent",
                "vintage": "fixture",
            },
        ),
        now,
    )


def test_event_five_minute_return_rejects_one_minute_quote(store, settings, now):
    event(store, now)
    store.save_snapshot(snapshot(now, price=100))
    store.save_snapshot(snapshot(now + timedelta(minutes=1), price=101))
    out = MacroEngine(store, settings, None).event_reactions(now + timedelta(minutes=10))[0]
    assert out["btc_reactions"]["300"] is None


def test_event_quote_uses_separate_archive_and_reports_offset(store, settings, now):
    event(store, now)
    baseline = snapshot(now, price=100)
    store.save_snapshot(baseline)
    at = now + timedelta(minutes=5, seconds=10)
    archive = IntelligenceStore(store)
    archive.save(
        EvidenceRecord(
            kind="btc_event_quote",
            key="test-quote",
            source="TEST",
            market_time=at,
            available_at=at,
            data={"price": 102},
        )
    )
    out = MacroEngine(store, settings, None).event_reactions(at)[0]["btc_reactions"]["300"]
    assert out["return_from_release_pct"] == pytest.approx(2)
    assert out["offset_seconds"] == 10 and out["quote_source"] == "EVENT_QUOTE"
    assert store.latest_snapshots(at)[0].id == baseline.id


def test_fusion_replay_uses_archived_config_and_same_decision_entry(store, settings, now):
    btc = snapshot(now, decision_at=now)
    alt = snapshot(
        now,
        Module.ALT,
        asset_id="binance:TESTUSDT",
        symbol="TESTUSDT",
        decision_at=now,
        features=Features(
            return_15m_pct=0,
            return_5m_pct=0,
            relative_volume=3,
            spot_taker_buy_ratio=0.51,
            funding_rate_pct=0.01,
            oi_change_5m_pct=0.1,
            spread_bps=2,
        ),
    )
    store.save_snapshot(btc)
    store.save_snapshot(alt)
    store.save_decision_config(now, settings)
    regime = apply_btc_context(market_regime(btc, now, settings), store, settings, now, btc)
    live = decide(alt, regime, now, settings, store)
    changed = settings.model_copy(update={"enable_intelligence": False})
    output = replay(store, now - timedelta(seconds=1), now + timedelta(seconds=1), changed)
    replayed = [s for s in output["signals"] if s["asset_id"] == alt.asset_id]
    assert [s.rule_id for s in live] == [s["rule_id"] for s in replayed] == ["pre-pump-fusion"]
    assert output["legacy_config_batches"] == 0


def test_calendar_requires_timezone_and_handles_dst():
    text = (
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:cpi\nSUMMARY:Consumer Price Index\n"
        "DTSTART;TZID=US/Eastern:20260714T083000\nEND:VEVENT\nEND:VCALENDAR"
    )
    assert calendar_entries(text)[0]["release_time"] == "2026-07-14T12:30:00+00:00"
    with pytest.raises(ProviderError):
        calendar_entries(text.replace(";TZID=US/Eastern", ""))


def test_release_parses_actual_and_embargo_but_never_guesses_consensus(now):
    text = (
        "8:30 a.m. (ET) Friday, December 6, 2024 "
        "Total nonfarm payroll employment increased by 227,000."
    )
    at, rows = release_values(text, "NFP", now)
    assert at.hour == 13 and rows[0][1:3] == (227000, "persons")
    with pytest.raises(ProviderError):
        release_values(text, "NFP", at - timedelta(seconds=1))
    with pytest.raises(ProviderError):
        release_values("Total nonfarm payroll employment increased by 227,000.", "NFP", now)


@pytest.mark.asyncio
async def test_calendar_failure_is_explicit_and_does_not_fabricate_events(store, settings, now):
    class Public:
        async def fetch(self, *args):
            raise ProviderError("HTTP 403")

    result = await EventEngine(store, settings, Public()).collect()
    assert result["status"] == "DEGRADED" and len(result["sources"]) == 3
    assert not IntelligenceStore(store).records("macro_event", now)


@pytest.mark.asyncio
async def test_worker_failure_persists_and_does_not_block_another_job(store, settings):
    settings.background_scopes = ["macro", "options"]
    worker = IntelligenceWorker(store, settings, SimpleNamespace(public=None))

    async def fail():
        raise ValueError("secret-must-not-be-exposed")

    async def success():
        return {"status": "COMPLETE"}

    worker.jobs["macro"] = (3600, fail)
    worker.jobs["options"] = (300, success)
    await worker.tick()
    await asyncio.gather(*worker.running.values())
    assert store.state("job:macro")["status"] == "FAILED"
    assert store.state("job:options")["last_success_at"]
    assert "secret-must-not-be-exposed" not in json.dumps(store.state("job:macro"))
    old = worker.running["macro"]
    await worker.tick()
    assert worker.running["macro"] is old


@pytest.mark.parametrize(
    "state_key,report_key",
    [("intelligence_worker", "worker"), ("validation_worker", "validation_worker")],
)
def test_worker_heartbeat_detects_dead_process(store, now, state_key, report_key):
    store.set_state(state_key, {"status": "RUNNING", "heartbeat_at": now.isoformat()})
    assert runtime_health(store, now)[report_key]["status"] == "RUNNING"
    assert runtime_health(store, now + timedelta(seconds=121))[report_key]["status"] == "STALE"
    assert store.state(state_key)["status"] == "RUNNING"  # Health reports do not mutate history.


def test_btc_context_has_missing_values_not_fake_neutral_scores(store, settings, now):
    output = btc_assessment(store, settings, now)
    assert output["coverage_pct"] == 0
    assert output["macro_risk"] == "UNKNOWN"
    assert all(v["score"] is None for v in output["dimensions"].values())


def test_macro_caution_requires_both_independent_inputs_and_is_pit(
    store, settings, now, monkeypatch
):
    archive = IntelligenceStore(store)
    for key, value, previous in [
        ("WALCL", 7000000, 7100000),
        ("WTREGEN", 500000, 500000),
        ("RRPONTSYD", 100, 100),
    ]:
        archive.save(
            record(now, key, value).model_copy(
                update={"data": {"value": value, "previous": previous}}
            )
        )
    btc = snapshot(now)
    store.save_snapshot(btc)
    assert btc_assessment(store, settings, now, btc)["macro_risk"] == "UNKNOWN"
    for i in range(5):
        archive.save(
            EvidenceRecord(
                kind="etf",
                key=f"BTC:{i}",
                source="TEST",
                market_time=now - timedelta(days=i),
                available_at=now,
                data={"net_flow_usd": -100},
            )
        )
    assert btc_assessment(store, settings, now, btc)["macro_risk"] == "CAUTION"
    assert (
        apply_btc_context(market_regime(btc, now, settings), store, settings, now, btc).risk_mode
        == "CAUTION"
    )
    assert btc_assessment(store, settings, now - timedelta(seconds=1))["macro_risk"] == "UNKNOWN"
    monkeypatch.setattr("muse_btc.api.utc_now", lambda: now)
    from muse_btc.rules import evaluate

    signal = evaluate(btc, market_regime(btc, now, settings), now, settings)[0]
    store.save_signal(signal)
    with TestClient(create_app(settings)) as client:
        result = client.get("/api/overview").json()
        assert result["regime"]["risk_mode"] == "CAUTION"
        assert result["signals"][0]["state"] == "PAUSED"


def test_default_budget_covers_all_assets():
    settings = Settings(_env_file=None)
    assert settings.detail_batch_size >= settings.max_altcoins
    assert not settings.enable_background_intelligence
    assert "research" not in settings.background_scopes


def test_new_read_interfaces_do_not_start_collectors(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "api.db", enable_collector=False)
    with TestClient(create_app(settings)) as client:
        for endpoint in (
            "/api/runtime",
            "/api/btc/assessment",
            "/api/macro/calendar",
            "/api/research/queue",
        ):
            response = client.get(endpoint)
            assert response.status_code == 200
        assert client.get("/api/runtime").json()["worker"]["status"] == "NOT_STARTED"


def test_compressed_raw_preserves_hash_and_transparent_reading(store, now):
    import hashlib

    payload = {"rows": ["repeated-market-data"] * 2000}
    raw_id = store.save_raw("TEST", "https://example.org/data", payload, now)
    raw = store.raw(raw_id)
    assert raw["payload"] == payload
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    assert raw["payload_hash"] == hashlib.sha256(encoded).hexdigest()
    with store.connect() as db:
        stored = db.execute(
            "SELECT payload FROM raw_observations WHERE id=?", (raw_id,)
        ).fetchone()[0]
    assert stored.startswith("gzip:") and len(stored) < len(encoded) / 2


def test_evidence_lease_renewal_cannot_revive_expired_owner(store, now):
    archive = IntelligenceStore(store)
    token = archive.acquire("test", now, 30)
    assert archive.renew("test", token, now + timedelta(seconds=20), 30)
    assert archive.acquire("test", now + timedelta(seconds=31), 30) is None
    assert not archive.renew("test", token, now + timedelta(seconds=51), 30)


def test_research_queue_keeps_muse_email_connection_and_revision_identity(store, settings, now):
    from test_phase2_6_framework import paper, review

    from muse_btc.research import ResearchEngine

    engine = ResearchEngine(store, settings, None)
    doc = engine.save_document("arXiv", paper(now), [], now, None)
    assert engine.queue(now)["email_ingestion"] == "EXISTING_MUSE_CONNECTION"
    assert engine.queue(now)["items"][0]["id"] == doc.id
    engine.review(doc.id, review(), now)
    assert not engine.queue(now)["items"]


@pytest.mark.asyncio
async def test_social_pagination_resumes_fixed_window_and_does_not_skip_failed_page(
    store, now, monkeypatch
):
    import httpx

    from muse_btc.social import SocialEngine

    monkeypatch.setattr("muse_btc.social.utc_now", lambda: now + timedelta(minutes=1))
    calls = []
    payloads = [
        {
            "data": [
                {
                    "id": "1",
                    "author_id": "a",
                    "created_at": now.isoformat(),
                    "text": "BTC liquidity",
                }
            ],
            "meta": {"next_token": "page2"},
        },
        None,
        {
            "data": [
                {"id": "2", "author_id": "b", "created_at": now.isoformat(), "text": "ETH market"}
            ],
            "meta": {"result_count": 1},
        },
    ]

    def respond(request):
        calls.append(dict(request.url.params))
        payload = payloads.pop(0)
        return httpx.Response(503 if payload is None else 200, json=payload or {})

    config = Settings(
        _env_file=None, enable_social=True, x_bearer_token="offline-fixture", social_max_pages=1
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        engine = SocialEngine(store, config, SimpleNamespace(client=client))
        assert (await engine.collect())["status"] == "DEGRADED"
        checkpoint = store.state("social_checkpoint")
        assert checkpoint["pending"]["next_token"] == "page2"
        assert not checkpoint.get("complete_through")
        assert (await engine.collect())["status"] == "UNAVAILABLE"
        assert store.state("social_checkpoint") == checkpoint
        assert (await engine.collect())["status"] == "COMPLETE"
    assert calls[1] == calls[2]
    assert calls[0]["end_time"] == calls[2]["end_time"]
    assert store.state("social_checkpoint")["complete_through"] == calls[0]["end_time"]
    assert len(IntelligenceStore(store).records("social", now + timedelta(days=365))) == 2


@pytest.mark.asyncio
async def test_social_partial_error_preserves_pending_window(store):
    import httpx

    from muse_btc.social import SocialEngine

    config = Settings(_env_file=None, enable_social=True, x_bearer_token="offline-fixture")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"errors": [{"title": "partial error"}], "meta": {}}
            )
        )
    ) as client:
        result = await SocialEngine(store, config, SimpleNamespace(client=client)).collect()
    assert result["status"] == "UNAVAILABLE"
    state = store.state("social_checkpoint")
    assert state["pending"] and not state.get("complete_through")


def test_html_only_muse_email_retains_original_link(store, settings, now):
    from email.message import EmailMessage
    from email.utils import format_datetime

    from muse_btc.research import ResearchEngine

    message = EmailMessage()
    message["From"] = "research@glassnode.com"
    message["Subject"] = "Bitcoin liquidity research"
    message["Date"] = format_datetime(now)
    message.set_content(
        "<p>Bitcoin liquidity research dataset methodology.</p>"
        '<a href="https://research.glassnode.com/fixture-report/">Read report</a>',
        subtype="html",
    )
    result = ResearchEngine(store, settings, None).import_email(message.as_bytes(), now)
    doc = IntelligenceStore(store).records("research", now)[0]
    assert doc.data["source_url"] == "https://research.glassnode.com/fixture-report/"
    assert store.raw(doc.raw_ids[0])["payload"]["sender_verified"] is False
    assert result["document"]["id"] == doc.id


def test_forward_result_uses_archived_costs_not_later_settings(store, settings, now):
    from muse_btc.rules import evaluate
    from muse_btc.validation import measure_signal

    btc = snapshot(now)
    signal = evaluate(btc, market_regime(btc, now, settings), now, settings)[0]
    store.save_decision_config(now, settings)
    settings.fee_bps_each_way = 100
    for minutes, value in [(2, 99), (4, 101), (5, 102)]:
        store.save_snapshot(snapshot(now + timedelta(minutes=minutes), price=value))
    outcome = measure_signal(store, signal, 300, now + timedelta(minutes=6), settings)
    assert outcome.round_trip_cost_bps == 30
    assert outcome.paper_net_return_pct == pytest.approx(1.7)


def test_ranking_delta_is_suppressed_when_another_member_loses_coverage(store, settings, now):
    from muse_btc.fusion import enrich_rankings
    from muse_btc.ranking import rank_assets

    one = snapshot(now, module=Module.ALT, asset_id="binance:AAAUSDT", symbol="AAAUSDT")
    two = snapshot(now, module=Module.ALT, asset_id="binance:BBBUSDT", symbol="BBBUSDT")
    first = enrich_rankings(
        rank_assets([one, two], [], now, settings), [one, two], store, now, settings
    )
    store.save_rankings(first, now)
    later = now + timedelta(seconds=1)
    changed = two.model_copy(deep=True)
    changed.features.funding_rate_pct = None
    rows = enrich_rankings(
        rank_assets([one, changed], first, later, settings), [one, changed], store, later, settings
    )
    assert all(r["rank_change"] is None and not r["rank_change_comparable"] for r in rows)


@pytest.mark.asyncio
async def test_successful_worker_cadence_does_not_add_fetch_latency(
    store, settings, now, monkeypatch
):
    settings.background_scopes = ["macro"]
    store.set_state(
        "job:macro",
        {
            "status": "COMPLETE",
            "started_at": now.isoformat(),
            "finished_at": (now + timedelta(seconds=5)).isoformat(),
        },
    )
    monkeypatch.setattr("muse_btc.worker.utc_now", lambda: now + timedelta(seconds=30))
    worker = IntelligenceWorker(store, settings, SimpleNamespace(public=None))

    async def success():
        return {"status": "COMPLETE"}

    worker.jobs["macro"] = (30, success)
    await worker.tick()
    assert "macro" in worker.running
    await asyncio.gather(*worker.running.values())
