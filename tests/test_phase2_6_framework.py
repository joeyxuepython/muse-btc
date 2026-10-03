"""Synthetic/local fixtures only. No research collection or live model training."""

from datetime import timedelta

import httpx
import pytest
from conftest import snapshot
from fastapi.testclient import TestClient

from muse_btc.alerts import publish_alert
from muse_btc.api import create_app
from muse_btc.config import Settings
from muse_btc.context import import_context
from muse_btc.experiments import fit_logistic, predict, train_model
from muse_btc.fusion import enrich_rankings, pre_pump_signals
from muse_btc.intelligence import ContextInput, EvidenceRecord, IntelligenceStore
from muse_btc.macro import MacroEngine, etf_rows, supply_changes
from muse_btc.meme import holder_metrics, token_identity, wallet_clusters
from muse_btc.microstructure import archive_snapshot_trades, cvd_summary
from muse_btc.models import Features, Module, Regime
from muse_btc.providers import Providers
from muse_btc.providers.common import ProviderError
from muse_btc.research import ResearchEngine, ResearchReview, feed_entries
from muse_btc.social import SocialEngine


def evidence(now, kind="macro", key="DGS2", **overrides):
    values = dict(
        kind=kind,
        key=key,
        source="SYNTHETIC_FIXTURE",
        market_time=now,
        available_at=now,
        data={"value": 2},
    )
    values.update(overrides)
    return EvidenceRecord(**values)


def paper(now, text="Bitcoin microstructure dataset methodology with liquidity observations."):
    return {
        "title": "Cryptocurrency market microstructure",
        "url": "https://arxiv.org/abs/2501.00001v1",
        "published": now,
        "updated": now,
        "authors": ["Fixture Author"],
        "body": text,
    }


def review():
    return ResearchReview(
        title_zh="加密市场微观结构研究",
        core_conclusion="这是合成样本研究结论，用于离线验证。",
        key_data_method="使用合成数据与方法样本验证归档流程。",
        trading_implication="交易意义仅为研究假设，不用于实际交易。",
        limitations="合成样本没有真实市场代表性，需要额外验证。",
        incremental_information="本次新增合成数据与方法，具有测试用增量信息。",
        original_excerpts=["Bitcoin microstructure dataset methodology"],
    )


def test_evidence_revisions_are_point_in_time_and_immutable(store, now):
    archive = IntelligenceStore(store)
    first = archive.save(evidence(now))
    archive.save(evidence(now + timedelta(days=1), data={"value": 3}))
    duplicate = archive.save(evidence(now + timedelta(days=2), data={"value": 3}))
    assert archive.records("macro", now)[0].id == first.id
    assert len(archive.records("macro", now + timedelta(days=2), latest=False)) == 2
    assert duplicate.available_at == now + timedelta(days=1)
    with pytest.raises(ValueError):
        archive.save(evidence(now, market_time=now + timedelta(seconds=1)))
    with pytest.raises(ValueError):
        archive.save(evidence(now, raw_ids=["MISSING"]))


def test_partial_threshold_override_preserves_required_defaults():
    config = Settings(_env_file=None, rule_thresholds={"rvol": 3})
    assert config.rule_thresholds["rvol"] == 3
    assert config.rule_thresholds["strong_groups"] >= 3
    assert "funding_hot_pct" in config.rule_thresholds
    with pytest.raises(ValueError):
        Settings(_env_file=None, rule_thresholds={"strong_groups": 2})


def test_trade_archive_deduplicates_and_discloses_sample_coverage(store, now):
    at = int(now.timestamp() * 1000)
    payload = [
        {"a": 1, "T": at - 1000, "p": "100", "q": "2", "m": False},
        {"a": 2, "T": at, "p": "100", "q": "1", "m": True},
        {"a": 3, "T": at + 1000, "p": "100", "q": "10", "m": False},
    ]
    raw = store.save_raw(
        "SYNTHETIC_FIXTURE",
        "https://data-api.binance.vision/api/v3/aggTrades?symbol=BTCUSDT",
        payload,
        now,
    )
    s = snapshot(now, raw_ids=[raw])
    archive_snapshot_trades(store, s, None)
    archive_snapshot_trades(store, s, None)
    result = cvd_summary(store, s.asset_id, now)
    assert result[0]["observed_trades"] == 2
    assert result[0]["observed_cvd_usdt"] == 100
    assert result[0]["full_market_cvd"] is None
    assert result[1]["observed_cvd_usdt"] is None


def test_cross_process_lease_excludes_and_recovers_expired_owner(store, now):
    first, second = IntelligenceStore(store), IntelligenceStore(store)
    token = first.acquire("research", now, 30)
    assert token and second.acquire("research", now) is None
    second.release("research", "wrong-token")
    assert second.acquire("research", now) is None
    assert second.acquire("research", now + timedelta(seconds=31))


def test_first_check_is_quiet_new_document_requires_chinese_review(store, settings, now):
    engine = ResearchEngine(store, settings, None)
    baseline = engine.save_document("arXiv", paper(now), [], now, None)
    assert not engine.review(baseline.id, review(), now)["notice_created"]
    updated = paper(now + timedelta(hours=1))
    updated["url"] = "https://arxiv.org/abs/2501.00002v1"
    doc = engine.save_document("arXiv", updated, [], now + timedelta(hours=1), now)
    changed_review = review().model_copy(
        update={"key_data_method": "本次使用不同的合成方法与数据做增量验证。"}
    )
    assert engine.review(doc.id, changed_review, now + timedelta(hours=1))["notice_created"]
    assert not engine.review(doc.id, changed_review, now + timedelta(hours=2))["notice_created"]
    assert len(engine.notices()) == 1


def test_revision_identity_and_unverified_excerpts(store, settings, now):
    engine = ResearchEngine(store, settings, None)
    first = engine.save_document("arXiv", paper(now), [], now, None)
    second_paper = paper(now + timedelta(hours=1), paper(now)["body"] + "x")
    second_paper["url"] = "https://arxiv.org/abs/2501.00001v2"
    second = engine.save_document("arXiv", second_paper, [], now + timedelta(hours=1), now)
    assert second.key == first.key
    assert second.data["novelty"] == "COSMETIC_REVISION"
    bad = review().model_copy(
        update={"original_excerpts": ["Invented conclusion absent from the original paper"]}
    )
    with pytest.raises(ValueError):
        engine.review(second.id, bad, now + timedelta(hours=1))
    with pytest.raises(ValueError):
        ResearchReview(**{**review().model_dump(), "limitations": "Not Chinese"})


@pytest.mark.asyncio
async def test_failed_source_keeps_successful_checkpoint(store, settings, now):
    class FailedPublic:
        async def fetch(self, *args):
            raise ProviderError("fixture 503")

    settings.research_feeds = {"arXiv": "https://export.arxiv.org/api/query"}
    engine = ResearchEngine(store, settings, FailedPublic())
    engine.archive.checked("arXiv", now, True, "baseline")
    result = await engine.check()
    assert result["status"] == "DEGRADED"
    assert engine.archive.checkpoint("arXiv") == now


@pytest.mark.asyncio
async def test_public_redirect_cannot_access_local_network(store, settings):
    paths = []

    def handler(request):
        paths.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1:9000/private"})

    providers = Providers(settings, store, httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError):
            await providers.public.fetch(
                "Glassnode", "https://research.glassnode.com/rss/", {"research.glassnode.com"}
            )
        assert len(paths) == 1
    finally:
        await providers.close()


def test_atom_parser_preserves_authors_revision_and_scope():
    xml = (
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/1v2</id>'
        "<title>Bitcoin</title><published>2025-01-01T00:00:00Z</published>"
        "<updated>2025-01-02T00:00:00Z</updated><author><name>A</name></author>"
        "<summary>microstructure</summary></entry></feed>"
    )
    row = feed_entries(xml, "arXiv", "https://export.arxiv.org/api/query")[0]
    assert row["url"] == "https://arxiv.org/abs/1v2"
    assert row["authors"] == ["A"]
    assert row["updated"] > row["published"]


def test_stablecoin_missing_and_etf_missing_are_not_zero():
    row = supply_changes({"circulating": {"peggedUSD": 100}})
    assert row["change_7d_pct"] is None
    assert row["supply_usd"] == 100
    html = (
        "<table><tr><th>Date</th><th>Fund</th><th>Total</th></tr>"
        "<tr><td>01 Jan 2025</td><td>-</td><td>-</td></tr>"
        "<tr><td>02 Jan 2025</td><td>0</td><td>(10.5)</td></tr></table>"
    )
    rows = etf_rows(html)
    assert len(rows) == 1 and rows[0][1] == -10500000


def test_macro_liquidity_units_and_revision_visibility(store, settings, now):
    archive = IntelligenceStore(store)
    for key, value in (("WALCL", 7000000), ("WTREGEN", 500000), ("RRPONTSYD", 100)):
        archive.save(evidence(now, key=key, data={"value": value}))
    summary = MacroEngine(store, settings, None).summary(now)
    assert summary["net_liquidity_proxy_usd"] == 6400000 * 1000000
    assert summary["yield_spread_10y_2y_pct"] is None


def test_import_time_and_holder_roles(store, now):
    item = ContextInput(
        kind="holder",
        key="ignored",
        source_url="https://example.org/dataset",
        market_time=now,
        data={
            "chain": "base",
            "address": "0x" + "a" * 40,
            "total_supply": 1000,
            "complete": False,
            "holders": [
                {"wallet": "LP", "balance": 500, "role": "pool"},
                {"wallet": "A", "balance": 100, "role": "ordinary"},
            ],
        },
    )
    record = import_context(store, item, now)
    assert record.key == "base:0x" + "a" * 40
    assert holder_metrics(record)["top10_pct"] is None
    assert holder_metrics(record)["observed_top10_pct"] == 10
    assert holder_metrics(record)["observed_unique_holders"] == 1
    with pytest.raises(ValueError):
        import_context(
            store, item.model_copy(update={"market_time": now + timedelta(seconds=1)}), now
        )


def test_case_sensitive_solana_and_same_block_not_common_owner(store, now):
    assert token_identity("solana", "A" * 32) != token_identity("solana", "a" * 32)
    links = [
        evidence(
            now,
            kind="wallet_link",
            key="same",
            data={
                "chain": "base",
                "wallet_a": "a",
                "wallet_b": "b",
                "relationship": "same_block",
                "confidence": 0.99,
            },
        )
    ]
    assert wallet_clusters(links, now) == []
    links.append(
        evidence(
            now + timedelta(hours=1),
            kind="wallet_link",
            key="future",
            data={
                "chain": "base",
                "wallet_a": "a",
                "wallet_b": "b",
                "relationship": "common_funder",
                "confidence": 0.99,
            },
        )
    )
    assert wallet_clusters(links, now) == []


def test_fusion_setup_is_reachable_and_risk_blocks_strong(store, settings, now):
    s = snapshot(now, module=Module.ALT, asset_id="binance:ALTUSDT", symbol="ALTUSDT")
    s.features.return_15m_pct = 0.1
    s.features.relative_strength_15m_pct = 0.1
    regime = Regime(as_of=now, risk_mode="CAUTION")
    store.save_snapshot(s)
    signals = pre_pump_signals(s, regime, now, settings, store)
    assert signals
    # Volume, price and spot flow are distinct; BTC caution prevents STRONG.
    alert = publish_alert(store, signals[0], None, now, settings)
    assert alert["level"] == "SETUP"
    assert signals[0].kind == "WATCH"


def test_cross_section_ties_and_missing_are_visible(store, settings, now):
    one = snapshot(now, module=Module.ALT, asset_id="a", symbol="A")
    two = snapshot(now, module=Module.ALT, asset_id="b", symbol="B")
    rows = [
        {"asset_id": s.asset_id, "canonical_asset_id": s.asset_id, "rank": i, "data_ready": True}
        for i, s in enumerate([one, two], 1)
    ]
    enriched = enrich_rankings(rows, [one, two], store, now, settings)
    assert [r["component_ranks"]["relative_volume"] for r in enriched] == [1, 1]
    assert "oi_acceleration" not in enriched[0]["component_ranks"]


def test_social_duplicate_text_and_future_post_exclusion(store, settings, now):
    for i in range(3):
        import_context(
            store,
            ContextInput(
                kind="social",
                key=str(i),
                source_url=f"https://x.com/i/status/{i}",
                market_time=now - timedelta(minutes=10),
                data={
                    "platform": "X",
                    "post_id": str(i),
                    "author_id": str(i),
                    "text": "AI agent Bitcoin liquidity",
                    "assets": ["BTC"],
                },
            ),
            now,
        )
    metrics = SocialEngine(store, settings, None).summary(now)
    assert metrics["unique_authors"] == 3
    assert metrics["text_similarity_repeat_ratio"] == pytest.approx(2 / 3)
    assert metrics["bot_probability"] is None


def test_model_history_gate_and_logistic_direction(store, settings, now):
    assert train_model(store, now, settings)["status"] == "INSUFFICIENT_HISTORY"
    x = [[-2], [-1], [1], [2]]
    weights = fit_logistic(x, [0, 0, 1, 1])
    probabilities = predict(weights, x)
    assert probabilities[0] < 0.5 < probabilities[-1]


def test_purged_model_training_never_promotes(store, settings, now):
    settings.ml_min_samples = 40
    settings.stale_seconds = 3600
    settings.poll_seconds = 3600
    first = now - timedelta(days=14)
    for day in range(12):
        at = first + timedelta(days=day)
        rows = []
        for asset in range(5):
            asset_id = f"binance:A{asset}USDT"
            s = snapshot(at, module=Module.ALT, asset_id=asset_id, symbol=f"A{asset}USDT")
            s.features = Features(
                relative_volume=asset + 1,
                relative_strength_15m_pct=asset / 10,
                spot_taker_buy_ratio=0.5 + asset / 50,
                oi_acceleration_pct=asset / 10,
                funding_rate_pct=0.01,
                perp_taker_buy_ratio=0.5,
                spread_bps=2,
                depth_imbalance=0.1,
                realized_volatility_pct=0.1,
            )
            store.save_snapshot(s)
            rows.append(
                {
                    "rank": asset + 1,
                    "canonical_asset_id": asset_id,
                    "asset_id": asset_id,
                    "snapshot_id": s.id,
                    "data_ready": True,
                }
            )
            for hour in range(1, 5):
                end = s.model_copy(
                    update={
                        "id": f"fixture-{day}-{asset}-{hour}",
                        "market_time": at + timedelta(hours=hour),
                        "available_at": at + timedelta(hours=hour),
                        "price": 100 + asset * hour,
                    }
                )
                store.save_snapshot(end)
        store.save_rankings(rows, at)
    result = train_model(store, now, settings)
    assert result["status"] == "EXPERIMENTAL"
    assert result["samples"]["validation"] >= 10
    assert not result["production_promotion"]
    assert not set(result["training_snapshot_ids"]) & set(result["test_snapshot_ids"])


def test_framework_api_auth_and_empty_start(settings):
    settings.api_token = "FRAMEWORK_TEST_TOKEN"
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/intelligence").status_code == 401
        headers = {"Authorization": "Bearer FRAMEWORK_TEST_TOKEN"}
        result = client.get("/api/intelligence", headers=headers)
        assert result.status_code == 200
        assert result.json()["research_notices"] == []
        assert result.json()["scheduled_research"] is False
        trained = client.post("/api/experiments/train", headers=headers, json={})
        assert trained.json()["status"] == "INSUFFICIENT_HISTORY"
        future = client.get("/api/evidence/macro?as_of=2099-01-01T00:00:00Z", headers=headers)
        assert future.status_code == 422
