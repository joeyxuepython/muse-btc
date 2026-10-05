from datetime import timedelta

import pytest
from conftest import candles, snapshot
from fastapi.testclient import TestClient

from muse_btc.alerts import publish_alert
from muse_btc.api import create_app
from muse_btc.config import Settings
from muse_btc.decisions import decide
from muse_btc.entry_quality import quality_contexts
from muse_btc.features import candle_features, depth_features
from muse_btc.models import Features, Module, SignalKind
from muse_btc.rules import component_usable, market_regime, usable
from muse_btc.service import Collector
from muse_btc.validation import replay


def rich(at, symbol="BTCUSDT", *, bid=600, ask=150, buy=0.65):
    module = Module.BTC if symbol == "BTCUSDT" else Module.ALT
    snap = snapshot(
        at,
        module,
        asset_id="binance:" + symbol,
        symbol=symbol,
        component_times={
            k: at for k in ("book", "candles", "funding", "oi", "oi_history", "mark", "taker")
        },
    )
    depth_features(
        {
            "bids": [[99.99, bid], [99.8, 1], [98, 1]],
            "asks": [[100.01, ask], [100.2, 1], [102, 1]],
        },
        snap.features,
    )
    snap.features.spot_taker_buy_ratio = buy
    return snap


def pair(store, now, symbol="BTCUSDT", *, bid=600, ask=150):
    old = rich(now - timedelta(seconds=120), symbol, bid=300, ask=ask)
    new = rich(now, symbol, bid=bid, ask=ask)
    store.save_snapshot(old)
    store.save_snapshot(new)
    return old, new


def signal(store, settings, snap, now):
    return next(
        s
        for s in decide(snap, market_regime(snap, now, settings), now, settings, store)
        if s.rule_id == "spot-led-momentum"
    )


def test_quality_is_enabled_by_default_and_first_observation_cannot_confirm(store, settings, now):
    assert Settings(_env_file=None).enable_entry_quality is True
    settings.enable_entry_quality = True
    snap = rich(now)
    store.save_snapshot(snap)
    result = signal(store, settings, snap, now)
    assert result.kind == SignalKind.WATCH and result.entry_zone is None
    assert not result.decision["entry_quality"]["checks"]["persistent_demand"]
    alert = publish_alert(store, result, None, now, settings)
    assert alert["level"] != "STRONG"
    assert store.notification_page(0, 20)["items"] == []


def test_distinct_complete_observations_confirm_without_unproven_market_gate(store, settings, now):
    settings.enable_entry_quality = True
    old, snap = pair(store, now)
    result = signal(store, settings, snap, now)
    assert result.kind == SignalKind.ENTRY_CANDIDATE
    assert result.decision["entry_quality"]["snapshot_ids"] == [snap.id, old.id]
    assert result.decision["market_confirmation"]["status"] == "UNKNOWN"
    assert result.decision["market_confirmation"]["mode"] == "observe"
    settings.market_confirmation_mode = "require"
    assert signal(store, settings, snap, now).kind == SignalKind.WATCH


@pytest.mark.parametrize(
    "problem", ["cached", "partial_thin", "thin", "chase", "future", "misaligned"]
)
def test_entry_blocks_bad_or_unconfirmed_inputs(store, settings, now, problem):
    settings.enable_entry_quality = True
    old, snap = pair(store, now)
    changed = snap.model_copy(deep=True, update={"id": "changed"})
    if problem == "cached":
        changed.component_times["book"] = old.component_times["book"]
        changed.component_times["candles"] = old.component_times["candles"]
    elif problem == "partial_thin":
        changed.features.spot_depth_bands["0.1"]["complete_band"] = False
        changed.features.spot_depth_bands["0.1"]["observed_bid_usd"] = 100
    elif problem == "thin":
        changed.features.spot_depth_bands["0.1"]["observed_bid_usd"] = 100
    elif problem == "chase":
        changed.features.return_5m_pct = 4
    elif problem == "future":
        changed.component_received_at["book"] = now + timedelta(seconds=1)
    else:
        changed.component_times["book"] = now - timedelta(seconds=180)
    result = signal(store, settings, changed, now)
    assert result.kind == SignalKind.WATCH
    assert result.decision["entry_quality"]["status"] == "WAIT"


def test_intervening_sell_flow_cannot_be_skipped_to_find_older_confirmation(store, settings, now):
    settings.enable_entry_quality = True
    _, snap = pair(store, now)
    middle = rich(now - timedelta(seconds=30), buy=0.3)
    store.save_snapshot(middle)
    result = signal(store, settings, snap, now)
    assert result.kind == SignalKind.WATCH
    assert middle.id in result.decision["entry_quality"]["snapshot_ids"]


def test_market_excludes_target_and_retains_missing_members(store, settings, now):
    target = pair(store, now, bid=100000)[1]
    symbols = ["BTCUSDT"] + [f"ALT{i}USDT" for i in range(5)]
    for symbol in symbols[1:]:
        pair(store, now, symbol, bid=300, ask=400)
    store.save_universe(
        {
            "selected_at": now.isoformat(),
            "entries": [{"binance_symbol": s, "canonical_asset_id": s} for s in symbols],
        }
    )
    market = quality_contexts(store, now, settings)[target.asset_id]["market_confirmation"]
    assert market["status"] == "WEAK" and market["eligible_assets"] == 5
    assert target.asset_id not in market["peer_snapshot_ids"]
    assert market["support_breadth_pct"] == 0
    store.save_universe(
        {
            "selected_at": now.isoformat(),
            "entries": [
                {"binance_symbol": s, "canonical_asset_id": s}
                for s in symbols + [f"MISSING{i}" for i in range(5)]
            ],
        }
    )
    market = quality_contexts(store, now, settings)[target.asset_id]["market_confirmation"]
    assert market["status"] == "UNKNOWN" and market["coverage_pct"] == 50
    assert len(market["missing_assets"]) == 5


def test_market_resonance_requires_bid_growth_and_excludes_future(store, settings, now):
    for symbol in ["BTCUSDT"] + [f"ALT{i}USDT" for i in range(5)]:
        pair(store, now, symbol)
    contexts = quality_contexts(store, now, settings)
    market = contexts["binance:BTCUSDT"]["market_confirmation"]
    assert market["status"] == "SUPPORTIVE" and market["resonance"]
    store.save_snapshot(rich(now + timedelta(seconds=1), "ALT0USDT", bid=1, ask=100000))
    assert quality_contexts(store, now, settings) == contexts
    later = rich(now + timedelta(seconds=120), bid=600, ask=50)
    store.save_snapshot(later)
    q = quality_contexts(store, later.available_at, settings)[later.asset_id]["entry_quality"]
    assert not q["buy_support_improving"]  # Ask withdrawals alone cannot establish buying.


def test_quality_live_replay_and_configuration_version(store, settings, now):
    settings.enable_entry_quality = True
    _, snap = pair(store, now)
    store.save_decision_config(now, settings)
    live = signal(store, settings, snap, now)
    replayed = replay(store, now, now + timedelta(seconds=1), settings)["signals"]
    again = next(s for s in replayed if s["rule_id"] == live.rule_id)
    assert again["kind"] == live.kind
    assert again["decision"]["entry_quality"] == live.decision["entry_quality"]
    settings.entry_min_depth_usdt *= 2
    assert signal(store, settings, snap, now).rule_version != live.rule_version


def test_sender_rechecks_current_rule_and_retains_immutable_original(
    store, settings, now, monkeypatch
):
    settings.enable_entry_quality = True
    _, snap = pair(store, now)
    original = signal(store, settings, snap, now)
    store.save_signal(original)
    alert = publish_alert(store, original, None, now, settings)
    later = rich(now + timedelta(seconds=60), buy=0.3)
    store.save_snapshot(later)
    monkeypatch.setattr("muse_btc.api.utc_now", lambda: later.available_at)
    with TestClient(create_app(settings)) as client:
        result = client.get("/api/alerts/" + alert["id"]).json()["alert"]
        assert result["delivery_guard"] == "SKIP_ENTRY_NOT_CONFIRMED"
        assert not result["notification_eligible"]
        assert result["decision"]["entry_quality"]["status"] == "PASS"
        assert result["current_entry_assessment"]["entry_quality"]["status"] == "WAIT"
        page = client.get("/api/alerts/notifications").json()
        assert page["items"][0]["delivery_status"] == "SKIP_ENTRY_NOT_CONFIRMED"
        assert page["delivery_groups_preview"] == []
        quality = client.get("/api/quality").json()
        assert quality["production_accuracy"] == "NOT_ESTABLISHED"


def test_support_withdrawal_requires_two_fresh_observations(store, settings, now):
    settings.enable_entry_quality = True
    _, snap = pair(store, now)
    original = signal(store, settings, snap, now)
    store.save_signal(original)
    collector = Collector(settings, store, None)
    for offset in (120, 240):
        later = rich(now + timedelta(seconds=offset), buy=0.3)
        store.save_snapshot(later)
        collector._process_signals(
            [later], market_regime(later, later.available_at, settings), later.available_at
        )
        invalid = [s for s in store.signals() if s.kind == SignalKind.INVALIDATED]
        assert len(invalid) == (0 if offset == 120 else 1)
    assert invalid[0].parent_signal_id == original.id
    assert "支撑消失" in invalid[0].evidence[0]
    assert (
        store.notification_page(0, 50)["items"] == []
    )  # No delivered parent, no cancellation spam.


def test_component_cannot_use_information_received_after_snapshot(store, settings, now):
    snap = rich(now)
    snap.component_received_at["funding"] = now + timedelta(seconds=1)
    assert not component_usable(snap, "funding", now + timedelta(seconds=2), settings)


@pytest.mark.parametrize(
    "book",
    [
        {"bids": [], "asks": []},
        {"bids": [[100, 1]], "asks": [[99, 1]]},
        {"bids": [[99, 1], [99, 2]], "asks": [[101, 1]]},
    ],
)
def test_invalid_books_are_explicit_errors(book):
    with pytest.raises(ValueError):
        depth_features(book, Features())


def test_zero_size_boundary_does_not_fake_complete_depth():
    f = Features()
    depth_features({"bids": [[99.99, 10], [99, 0]], "asks": [[100.01, 10], [101, 0]]}, f)
    assert not f.spot_depth_bands["0.1"]["complete_band"]


def test_invalid_ohlc_and_taker_data_block_strategy(settings, now):
    history = candles(now)
    history[-1].high = history[-1].low - 1
    _, issues = candle_features(history, now)
    assert "INVALID_CANDLE_VALUES" in issues
    assert not usable(snapshot(now, quality_issues=issues), now, settings)


def test_fusion_cannot_promote_from_context_votes_without_market_confirmation(store, settings, now):
    from muse_btc.fusion import pre_pump_signals

    btc = rich(now)
    store.save_snapshot(btc)
    regime = market_regime(btc, now, settings)
    alt = rich(now, "ALTUSDT")
    alt.features.return_15m_pct = 0.2
    alt.features.relative_strength_15m_pct = 0
    context = {
        "evidence_groups": ["catalyst", "tokenomics"],
        "supporting": ["正面背景"],
        "risks": [],
    }
    # Only price, volume, spot flow are supported: background cannot fill group four.
    trace = []
    result = pre_pump_signals(alt, regime, now, settings, store, trace, context=context)[0]
    assert len(result.evidence_groups) >= settings.rule_thresholds["strong_groups"]
    assert result.kind == SignalKind.WATCH
    assert not trace[-1]["checks"]["evidence_groups"]
    alt.features.relative_strength_15m_pct = 0.5
    alt.features.oi_change_5m_pct = 1.6
    alt.features.spot_taker_buy_ratio = 0.4
    result = pre_pump_signals(alt, regime, now, settings, store, context=context)[0]
    assert result.kind == SignalKind.WATCH
    assert "尚无实际现货主动买入确认" in result.contradictions


def test_quality_policy_change_has_new_archive_even_inside_cooldown(store, settings, now):
    settings.enable_entry_quality = True
    _, snap = pair(store, now)
    collector = Collector(settings, store, None)
    collector._process_signals([snap], market_regime(snap, now, settings), now)
    settings.entry_min_depth_usdt = 12000
    later = rich(now + timedelta(seconds=60))
    store.save_snapshot(later)
    collector._process_signals(
        [later], market_regime(later, later.available_at, settings), later.available_at
    )
    versions = {s.rule_version for s in store.signals() if s.kind == SignalKind.ENTRY_CANDIDATE}
    assert len(versions) == 2


def test_partial_deep_book_can_prove_floor_but_not_market_imbalance(store, settings, now):
    settings.enable_entry_quality = True
    old = rich(now - timedelta(seconds=120))
    current = rich(now)
    for snap in (old, current):
        snap.features.spot_depth_bands["0.1"]["complete_band"] = False
        store.save_snapshot(snap)
    result = signal(store, settings, current, now)
    assert result.kind == SignalKind.ENTRY_CANDIDATE
    q = result.decision["entry_quality"]
    assert q["depth_interpretation"] == "OBSERVED_LOWER_BOUND"
    assert q["imbalance"] is None
    assert not q["market_comparable"]
