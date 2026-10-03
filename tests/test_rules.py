from datetime import timedelta

import pytest
from conftest import snapshot

from muse_btc.models import Features, Module, SignalKind, TokenRisk
from muse_btc.providers import token_risk
from muse_btc.rules import evaluate, market_regime


def test_btc_entry_requires_independent_groups_and_complete_derivatives(now, settings):
    btc = snapshot(now)
    signals = evaluate(btc, market_regime(btc, now, settings), now, settings)
    entry = next(s for s in signals if s.kind == SignalKind.ENTRY_CANDIDATE)
    assert set(entry.evidence_groups) == {"price", "spot_flow", "derivatives"}
    assert entry.validation_status == "OBSERVATION_ONLY"
    assert entry.invalidation_price < entry.reference_price
    assert entry.entry_zone[0] < entry.reference_price < entry.entry_zone[1]


@pytest.mark.parametrize("reason", ["stale", "future", "gaps", "history"])
def test_bad_data_never_creates_opportunity(now, settings, reason):
    btc = snapshot(now)
    if reason == "stale":
        btc.available_at = btc.market_time = now - timedelta(hours=1)
    elif reason == "future":
        btc.available_at = now + timedelta(minutes=1)
    else:
        btc.quality_issues = ["CANDLE_GAPS" if reason == "gaps" else "INSUFFICIENT_CANDLE_HISTORY"]
    assert evaluate(btc, market_regime(btc, now, settings), now, settings) == []


def test_missing_futures_downgrades_btc_and_alt_to_watch(now, settings):
    btc = snapshot(now)
    btc.features.funding_rate_pct = None
    regime = market_regime(btc, now, settings)
    assert regime.risk_mode == "CAUTION"
    for asset in [btc, snapshot(now, Module.ALT)]:
        signals = evaluate(asset, regime, now, settings)
        assert not any(s.kind == SignalKind.ENTRY_CANDIDATE for s in signals)


def test_btc_risk_overrides_strong_altcoin(now, settings):
    btc = snapshot(now)
    btc.features.return_15m_pct = -2
    btc.features.spot_taker_buy_ratio = 0.3
    regime = market_regime(btc, now, settings)
    assert regime.risk_mode == "RISK_OFF"
    signals = evaluate(snapshot(now, Module.ALT), regime, now, settings)
    assert signals and all(s.kind != SignalKind.ENTRY_CANDIDATE for s in signals)
    assert "暂停入场级提醒" in " ".join(signals[-1].contradictions)


def test_short_squeeze_is_hypothesis_not_confirmed_buy(now, settings):
    btc = snapshot(now)
    btc.features.oi_change_5m_pct = -2
    btc.features.funding_rate_pct = -0.01
    signals = evaluate(btc, market_regime(btc, now, settings), now, settings)
    squeeze = next(s for s in signals if s.rule_id == "squeeze-candidate")
    assert squeeze.kind == SignalKind.WATCH
    assert any("不能确认" in text for text in squeeze.contradictions)


def meme(now, risk=None):
    return snapshot(
        now,
        Module.MEME,
        asset_id="dex:base:token:pool",
        symbol="TEST",
        features=Features(liquidity_usd=100000, relative_volume=3, buys_5m=45, sells_5m=10),
        risk=risk or TokenRisk(missing_checks=["LP 锁定"]),
    )


def test_retired_meme_unknown_security_emits_nothing(now, settings):
    btc = snapshot(now)
    signals = evaluate(meme(now), market_regime(btc, now, settings), now, settings)
    assert signals == []


def test_retired_meme_security_block_emits_nothing(now, settings):
    btc = snapshot(now)
    asset = meme(now, TokenRisk(status="BLOCKED", blockers=["蜜罐标记"]))
    signals = evaluate(asset, market_regime(btc, now, settings), now, settings)
    assert signals == []


def test_retired_meme_screened_quote_emits_nothing(now, settings):
    btc = snapshot(now)
    asset = meme(now, TokenRisk(status="SCREENED"))
    asset.quality_issues = ["QUOTE_TIME_UNVERIFIED"]
    assert evaluate(asset, market_regime(btc, now, settings), now, settings) == []


def test_goplus_missing_flags_not_treated_as_safe():
    risk = token_risk({"is_honeypot": "0"}, "base")
    assert risk.status == "NEEDS_VERIFICATION"
    assert risk.missing_checks
    blocked = token_risk({"is_honeypot": "1"}, "base")
    assert blocked.status == "BLOCKED"


def test_incomplete_detail_risk_is_observation_only(now, settings):
    btc = snapshot(now)
    btc.features.funding_rate_pct = None
    btc.features.return_15m_pct = -2
    btc.features.spot_taker_buy_ratio = 0.3
    signals = evaluate(btc, market_regime(btc, now, settings), now, settings)
    assert signals and all(s.kind == SignalKind.WATCH for s in signals)
    assert all(s.entry_zone is None for s in signals)
