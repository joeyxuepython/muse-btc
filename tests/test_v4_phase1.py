import sqlite3
from datetime import timedelta

import httpx
import pytest
from conftest import snapshot
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_providers_api import public_api_fixture

from muse_btc.alerts import change_alert, publish_alert
from muse_btc.api import create_app
from muse_btc.features import cross_venue_features, depth_features
from muse_btc.models import Features, Module, SignalKind, utc_now
from muse_btc.providers import Providers
from muse_btc.ranking import rank_assets
from muse_btc.rules import evaluate, market_regime
from muse_btc.storage import Store
from muse_btc.universe import build_universe
from muse_btc.validation import measure_signal


def test_pins_and_bounded_daily_replacements_preserve_universe(settings, now):
    settings.max_altcoins = 3
    settings.universe_max_replacements = 1
    names = ["BTC", "ETH", "AAA", "BBB", "CCC", "DDD", "PEPE", "USDC"]
    info = {
        "symbols": [
            {
                "symbol": b + "USDT",
                "baseAsset": b,
                "quoteAsset": "USDT",
                "status": "TRADING",
                "isSpotTradingAllowed": True,
            }
            for b in names
        ]
    }
    quotes = [
        {"symbol": b + "USDT", "lastPrice": "1", "quoteVolume": str(1000000 - i * 10000)}
        for i, b in enumerate(names)
    ]
    first = build_universe(info, quotes, [], [], None, settings, now, [])
    settings.pinned_symbols = ["PEPEUSDT"]
    next_day = build_universe(info, quotes, [], [], first, settings, now + timedelta(days=1), [])
    assert len(next_day["entries"]) == 5
    assert [r["binance_symbol"] for r in next_day["entries"][:2]] == ["BTCUSDT", "ETHUSDT"]
    assert "PEPEUSDT" in {r["binance_symbol"] for r in next_day["entries"]}
    assert "USDCUSDT" not in {r["binance_symbol"] for r in next_day["entries"]}
    assert next_day["replacements"] <= 1


def test_web_alert_escalation_and_durable_reader_state(settings, store, now):
    btc = snapshot(now)
    store.save_snapshot(btc)
    signal = evaluate(btc, market_regime(btc, now, settings), now, settings)[0]
    signal.kind = SignalKind.WATCH
    signal.evidence_groups = ["price"]
    first = publish_alert(store, signal, None, now, settings)
    change_alert(store, first["id"], "read", now)
    change_alert(store, first["id"], "pin", now)
    signal.kind = SignalKind.ENTRY_CANDIDATE
    signal.evidence_groups = ["price", "spot_flow", "derivatives"]
    later = publish_alert(store, signal, None, now + timedelta(minutes=1), settings)
    assert later["id"] == first["id"]
    assert later["level"] == "STRONG"
    assert later["read_at"] is None
    assert later["pinned"]
    assert len(store.alerts()) == 1
    assert len(store.alert_events(first["id"])) == 4
    assert store.alerts()[0]["first_seen"] == now.isoformat()


def test_quote_only_snapshots_measure_forward_outcomes(store, settings, now):
    btc = snapshot(now)
    store.save_snapshot(btc)
    signal = evaluate(btc, market_regime(btc, now, settings), now, settings)[0]
    store.save_signal(signal)
    future = snapshot(
        now + timedelta(minutes=15),
        price=110,
        quality_issues=["INSUFFICIENT_CANDLE_HISTORY"],
        candles=[],
    )
    store.save_snapshot(future)
    outcome = measure_signal(store, signal, 900, future.available_at, settings)
    assert outcome is not None
    assert round(outcome.return_pct) == 10
    assert outcome.time_to_mfe_seconds == 900


def test_ranking_delta_and_time_point_reads(store, settings, now):
    alt = snapshot(now, Module.ALT, canonical_asset_id="asset:TEST")
    first = rank_assets([alt], [], now, settings)
    store.save_rankings(first, now)
    alt.features.relative_strength_15m_pct = 0
    second = rank_assets([alt], first, now + timedelta(seconds=1), settings)
    store.save_rankings(second, now + timedelta(seconds=1))
    assert second[0]["score_delta"] < 0
    assert store.rankings(now) == first
    assert store.rankings(now + timedelta(seconds=1)) == second


def test_alert_endpoints_and_live_feed_require_api_auth(settings, monkeypatch):
    settings.api_token = "v4-test-only-token"
    app = create_app(
        settings,
        providers_factory=lambda c, s: Providers(
            c, s, transport=httpx.MockTransport(public_api_fixture)
        ),
    )
    with TestClient(app) as client:
        assert client.get("/api/alerts").status_code == 401
        auth = {"Authorization": "Bearer v4-test-only-token"}
        assert client.post("/api/collect", headers=auth).json()["snapshots"] == 3
        assert len(client.get("/api/rankings", headers=auth).json()) == 1
        with client.websocket_connect("/api/live") as ws:
            ws.send_json({"token": "v4-test-only-token"})
            assert ws.receive_json()["type"] == "update"
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect("/api/live") as ws:
                ws.send_json({"token": "wrong"})
                ws.receive_json()
        assert denied.value.code == 1008
        assert client.post("/api/alerts/missing/read", headers=auth).status_code == 404
        later = utc_now() + timedelta(seconds=settings.stale_seconds + 1)
        monkeypatch.setattr("muse_btc.api.utc_now", lambda: later)
        assert not any(
            row["data_ready"] for row in client.get("/api/rankings", headers=auth).json()
        )
        assert all(a["state"] != "ACTIVE" for a in client.get("/api/alerts", headers=auth).json())


def test_numeric_evidence_updates_do_not_reset_read_state_or_notify(settings, store, now):
    btc = snapshot(now)
    signal = evaluate(btc, market_regime(btc, now, settings), now, settings)[0]
    first = publish_alert(store, signal, {"score": 70}, now, settings)
    change_alert(store, first["id"], "read", now)
    signal.evidence[0] = "5 分钟变化 0.81%"
    signal.reference_price = 101
    updated = publish_alert(store, signal, {"score": 71}, now + timedelta(minutes=1), settings)
    assert updated["read_at"] == now.isoformat()
    assert updated["price"] == 101
    assert updated["notification_revision"] == first["notification_revision"]
    assert len(store.alert_events(first["id"])) == 2
    upgraded = publish_alert(store, signal, {"score": 81}, now + timedelta(minutes=2), settings)
    assert upgraded["read_at"] is None
    assert upgraded["notification_revision"] != first["notification_revision"]
    assert len(store.alert_events(first["id"])) == 3


def test_misaligned_or_stale_derivatives_cap_alert_at_watch(settings, now):
    btc = snapshot(now)
    regime = market_regime(btc, now, settings)
    btc.component_times = {
        name: now for name in ("quote", "candles", "book", "funding", "oi", "oi_history", "mark")
    }
    assert evaluate(btc, regime, now, settings)[0].kind == SignalKind.ENTRY_CANDIDATE
    btc.component_times["mark"] = now - timedelta(seconds=121)
    assert evaluate(btc, regime, now, settings)[0].kind == SignalKind.WATCH
    btc.component_times["mark"] = now
    btc.component_times["funding"] = now - timedelta(seconds=301)
    signal = evaluate(btc, regime, now, settings)[0]
    assert signal.kind == SignalKind.WATCH
    assert "数据覆盖不足，仅供观察" in signal.contradictions
    assert market_regime(btc, now, settings).risk_mode == "CAUTION"


def test_missing_ranking_component_has_negative_explained_delta(settings, now):
    alt = snapshot(now, Module.ALT)
    initial = rank_assets([alt], [], now, settings)
    alt.features.funding_rate_pct = None
    changed = rank_assets([alt], initial, now, settings)[0]
    assert changed["score_changes"]["funding"] < 0
    assert changed["coverage_pct"] < initial[0]["coverage_pct"]
    assert "funding" in changed["missing"]


def test_cross_venue_hypotheses_require_time_alignment_and_finite_book(settings, now):
    features = Features(
        spot_taker_buy_ratio=0.65, perp_taker_buy_ratio=0.51, mark_price=101, index_price=100
    )
    times = {name: now for name in ("candles", "taker", "quote", "mark", "index", "oi_history")}
    cross_venue_features(features, 100, times, now, settings)
    assert features.spot_perp_structure == "SPOT_LED_HYPOTHESIS"
    assert features.basis_pct == pytest.approx(1)
    times["taker"] = now - timedelta(minutes=5)
    cross_venue_features(features, 100, times, now, settings)
    assert features.spot_perp_structure is None
    assert features.deleveraging_signal is None
    with pytest.raises(ValueError):
        depth_features({"bids": [["NaN", "1"]], "asks": [["101", "1"]]}, features)
    with pytest.raises(ValueError):
        features.oi_change_5m_pct = float("inf")


def test_additive_migration_preserves_old_json_and_backup(store, now, tmp_path):
    old = snapshot(now)
    # Old payloads lack all new V4 optional fields.
    import json

    old_json = old.model_dump(mode="json")
    for field in (
        "canonical_asset_id",
        "component_times",
        "component_received_at",
        "missing_metrics",
        "tier",
    ):
        old_json.pop(field)
    store.save_snapshot(old)
    original = json.dumps(old_json, sort_keys=True)
    with store.connect() as db:
        db.execute("UPDATE snapshots SET payload=?", (original,))
        for table in (
            "instrument_registry",
            "universe_history",
            "ranking_history",
            "web_alerts",
            "web_alert_events",
            "runtime_state",
        ):
            db.execute("DROP TABLE " + table)
        db.execute("PRAGMA user_version=0")
    migrated = Store(store.path)
    assert migrated.snapshot(old.id).canonical_asset_id is None
    with migrated.connect() as db:
        assert db.execute("SELECT payload FROM snapshots").fetchone()[0] == original
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    destination = tmp_path / "backup.db"
    migrated.backup(destination)
    with sqlite3.connect(destination) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT payload FROM snapshots").fetchone()[0] == original
    with migrated.connect() as db:
        db.execute("PRAGMA user_version=999")
    with pytest.raises(ValueError, match="newer"):
        Store(store.path)


@pytest.mark.asyncio
async def test_registry_metadata_and_checkpoint_survive_provider_restart(settings, store):
    settings.detail_batch_size = 1
    requests = []

    def handler(request):
        requests.append(request)
        return public_api_fixture(request)

    first = Providers(settings, store, transport=httpx.MockTransport(handler))
    await first.binance()
    selected_at = store.universe()["selected_at"]
    round_number = store.state("detail_round")
    await first.close()
    restarted = Providers(settings, Store(store.path), transport=httpx.MockTransport(handler))
    try:
        requests.clear()
        await restarted.binance()
        assert store.state("detail_round") == round_number + 1
        assert store.universe()["selected_at"] == selected_at
        assert not any(r.url.path.endswith("exchangeInfo") for r in requests)
    finally:
        await restarted.close()


def test_universe_converts_perp_coin_volume_and_never_guesses_prefixes(settings, now):
    settings.max_altcoins = 3
    settings.universe_weights = {"derivatives": 100}
    names = ["BTC", "ETH", "EXPENSIVE", "CHEAP", "PEPE"]
    info = {
        "symbols": [
            {"symbol": b + "USDT", "baseAsset": b, "quoteAsset": "USDT", "status": "TRADING"}
            for b in names
        ]
    }
    quotes = [{"symbol": b + "USDT", "lastPrice": "1", "quoteVolume": "1000000"} for b in names]
    swaps = [
        {
            "instId": b + "-USDT-SWAP",
            "instFamily": b + "-USDT",
            "state": "live",
            "settleCcy": "USDT",
            "ctType": "linear",
        }
        for b in ("EXPENSIVE", "CHEAP", "1000PEPE")
    ]
    tickers = [
        {"instId": "EXPENSIVE-USDT-SWAP", "volCcy24h": "2", "last": "100000"},
        {"instId": "CHEAP-USDT-SWAP", "volCcy24h": "1000000", "last": "0.01"},
    ]
    result = build_universe(info, quotes, swaps, tickers, None, settings, now, [])
    entries = {r["binance_symbol"]: r for r in result["entries"]}
    assert entries["EXPENSIVEUSDT"]["score"] > entries["CHEAPUSDT"]["score"]
    assert entries["EXPENSIVEUSDT"]["okx_volume_24h_usdt_proxy"] == 200000
    assert entries["PEPEUSDT"]["okx_inst_id"] is None
    assert entries["PEPEUSDT"]["selection_coverage_pct"] == 0
