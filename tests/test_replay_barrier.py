from datetime import timedelta

import pytest
from conftest import snapshot

from muse_btc.models import Module
from muse_btc.rules import evaluate, market_regime
from muse_btc.validation import replay


def test_replay_uses_same_complete_cycle_as_live_decision(store, settings, now):
    decision = now + timedelta(seconds=10)
    alt = snapshot(now, Module.ALT, decision_at=decision)
    btc = snapshot(now + timedelta(seconds=5), decision_at=decision)
    store.save_snapshot(alt)
    store.save_snapshot(btc)
    live = evaluate(alt, market_regime(btc, decision, settings), decision, settings)
    replayed = replay(store, now - timedelta(seconds=1), decision, settings)["signals"]
    alt_replayed = [s for s in replayed if s["asset_id"] == alt.asset_id]
    assert [s.kind for s in live] == [s["kind"] for s in alt_replayed]
    assert alt_replayed[0]["btc_snapshot_id"] == btc.id


def test_signal_future_reference_is_rejected(store, settings, now):
    btc = snapshot(now)
    future = snapshot(now + timedelta(seconds=10), Module.ALT)
    store.save_snapshot(btc)
    store.save_snapshot(future)
    signal = evaluate(btc, market_regime(btc, now, settings), now, settings)[0]
    signal.snapshot_id = future.id
    with pytest.raises(ValueError, match="future evidence"):
        store.save_signal(signal)
