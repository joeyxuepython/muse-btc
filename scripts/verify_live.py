# Manual integration check. Uses a new temporary database; never opens data/muse.db.
import asyncio
import json
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from muse_btc.api import create_app
from muse_btc.config import Settings
from muse_btc.models import utc_now
from muse_btc.providers import Providers
from muse_btc.service import Collector
from muse_btc.storage import Store


async def run():
    path = Path("/tmp/muse-v4-acceptance-" + uuid.uuid4().hex[:8] + ".db")
    config = Settings(
        database_path=path, enable_collector=False, max_altcoins=100, detail_batch_size=20
    )
    reports = []
    for cycle in range(2):
        store = Store(path)
        provider = Providers(config, store)
        collector = Collector(config, store, provider)
        collector.initialise_statuses()
        try:
            result = await collector.collect_once()
            snapshots = store.latest_snapshots(utc_now())
            rows = store.rankings()
            detailed = sum(s.detail_updated_at is not None for s in snapshots)
            report = {
                "cycle": cycle + 1,
                "result": result,
                "target": provider.coverage.get("target"),
                "quotes": provider.coverage.get("quotes"),
                "detail_coverage": detailed,
                "ranks": len(rows),
                "members": len((store.universe() or {}).get("entries", [])),
                "oi": sum(s.features.oi_usd is not None for s in snapshots),
                "funding": sum(s.features.funding_rate_pct is not None for s in snapshots),
                "taker": sum(s.features.perp_taker_buy_ratio is not None for s in snapshots),
                "request_metrics": provider.metrics,
                "core": [
                    {
                        "symbol": s.symbol,
                        "funding": s.features.funding_rate_pct,
                        "oi_5m": s.features.oi_change_5m_pct,
                        "basis": s.features.basis_pct,
                        "perp_spread": s.features.perp_spread_bps,
                        "issues": s.quality_issues,
                    }
                    for s in snapshots
                    if s.module in ("BTC", "ETH")
                ],
                "round": store.state("detail_round"),
            }
            reports.append(report)
            print(json.dumps(report, ensure_ascii=False), flush=True)
            if result.get("snapshots") != 102 or len(rows) != 100:
                raise AssertionError("Live coverage/ranking incomplete")
        finally:
            await provider.close()
    # Verify API restart reads against temporary data without new network collection.
    with TestClient(create_app(config)) as client:
        ready = client.get("/ready")
        overview = client.get("/api/overview").json()
        api = {
            "ready_code": ready.status_code,
            "ready": ready.json(),
            "assets": len(overview["assets"]),
            "rankings": len(overview["rankings"]),
            "alerts": len(overview["alerts"]),
        }
    report = {
        "database": str(path),
        "cycles": reports,
        "api": api,
        "checked_at": utc_now().isoformat(),
    }
    Path("/tmp/muse-v4-acceptance-result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    print(json.dumps({"api": api, "database": str(path)}, ensure_ascii=False), flush=True)


asyncio.run(run())
