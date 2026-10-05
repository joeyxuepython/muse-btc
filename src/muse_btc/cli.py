import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

import uvicorn

from .async_io import run_sync
from .btc_data import BTCDataEngine
from .config import Settings
from .context import import_context
from .entry_quality import quality_contexts
from .events import EventEngine
from .experiments import ranking_report, train_model
from .intelligence import ContextInput, IntelligenceStore
from .macro import MacroEngine
from .models import utc_now
from .providers import Providers
from .research import ResearchEngine, ResearchReview
from .service import Collector
from .social import SocialEngine
from .storage import Store
from .validation import replay, validate_pending, validation_report
from .worker import IntelligenceWorker, runtime_health


def parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise argparse.ArgumentTypeError("时间必须包含时区，例如 2026-10-01T00:00:00Z")
    return result


async def collect_once(settings: Settings, store: Store) -> dict:
    providers = Providers(settings, store)
    collector = Collector(settings, store, providers)
    await run_sync(collector.initialise_statuses)
    try:
        return await collector.collect_once()
    finally:
        await providers.close()


async def intelligence_once(settings, store, scope, sources=None):
    providers = Providers(settings, store)
    try:
        if scope == "research":
            return await ResearchEngine(store, settings, providers.public).check(sources)
        if scope == "events":
            return await EventEngine(store, settings, providers.public).collect()
        if scope == "macro":
            return await MacroEngine(store, settings, providers.public).collect()
        if scope == "btc":
            return await BTCDataEngine(store, settings, providers.public).collect()
        if scope == "social":
            return await SocialEngine(store, settings, providers).collect()
        snapshots = await providers.memes()
        for snapshot in snapshots:
            await run_sync(store.save_snapshot, snapshot)
        return {"snapshots": len(snapshots), "enabled": settings.enable_meme_discovery}
    finally:
        await providers.close()


async def free_data_once(settings, store, scope, seconds):
    providers = Providers(settings, store)
    try:
        return await BTCDataEngine(store, settings, providers.public).collect(scope, seconds)
    finally:
        await providers.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Muse BTC / Altcoin / Meme monitoring")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="启动网页、API 和后台采集")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("collect", help="执行一轮真实公开数据采集")
    validate = sub.add_parser("validate", help="计算已到期提醒的前瞻表现")
    validate.add_argument(
        "--retry-missing", action="store_true", help="导入缺失历史数据后，重新检查已关闭的缺失窗口"
    )
    sub.add_parser("validation-worker", help="独立运行限量历史评估；不采集行情或研究")
    sub.add_parser("quality-report", help="只读取归档，检查入场质量和市场买盘覆盖")
    backtest = sub.add_parser("replay", help="对真实归档快照进行时间点重放")
    backtest.add_argument("--start", required=True, type=parse_time)
    backtest.add_argument("--end", required=True, type=parse_time)
    intelligence = sub.add_parser("intelligence", help="手动采集扩展来源，不创建定时任务")
    intelligence.add_argument(
        "--scope", required=True, choices=["macro", "events", "meme", "social", "research", "btc"]
    )
    intelligence.add_argument(
        "--sources",
        nargs="+",
        choices=["Glassnode", "Coinbase", "CoinShares", "Santiment", "arXiv"],
    )
    free_data = sub.add_parser("free-data", help="手动归档免费链上、期权、宏观与采样清算")
    free_data.add_argument(
        "--scope", choices=["all", "onchain", "options", "macro", "liquidations"], default="all"
    )
    free_data.add_argument("--seconds", type=int, default=10, help="清算实际监听秒数，1–3600")
    imported = sub.add_parser("import-context", help="导入有来源和时间的 JSON 证据")
    imported.add_argument("path", type=Path)
    email = sub.add_parser("import-email", help="导入研究邮件 EML；附件仅登记")
    email.add_argument("path", type=Path)
    review = sub.add_parser("review-research", help="提交中文研究结构与原文证据摘录")
    review.add_argument("document_id")
    review.add_argument("path", type=Path)
    sub.add_parser("experiment-report", help="归档 TopK / Lead Time / 误报分析")
    train = sub.add_parser("train", help="按时间分割与隔离标签窗口训练研究模型")
    train.add_argument("--horizon", type=int, default=14400, choices=[14400, 86400, 259200, 604800])
    sub.add_parser("framework", help="检查数据库与模块框架；不采集网络数据")
    sub.add_parser("worker", help="在当前进程持续采集配置的扩展来源")
    sub.add_parser("runtime", help="查看采集心跳、失败和最近成功记录")
    backup = sub.add_parser("backup", help="创建 SQLite 在线一致性备份")
    backup.add_argument("path", type=Path)
    sub.add_parser("storage-report", help="查看存储增长；保留原始证据")
    sub.add_parser("research-queue", help="供 Muse 已有邮件/分析流程获取待处理研究")
    args = parser.parse_args()
    settings = Settings()
    if args.command == "serve":
        from .api import create_app

        if args.host not in ("127.0.0.1", "localhost", "::1") and not settings.api_token:
            parser.error("对外监听时请在 .env 设置 MUSE_API_TOKEN")
        uvicorn.run(create_app(settings), host=args.host, port=args.port)
        return
    store = Store(settings.database_path)
    if args.command == "validation-worker":
        from .validation_worker import ValidationWorker

        try:
            asyncio.run(ValidationWorker(store, settings).run())
        except KeyboardInterrupt:
            pass
    elif args.command == "worker":

        async def run_worker():
            providers = Providers(settings, store)
            try:
                return await IntelligenceWorker(store, settings, providers).run()
            finally:
                await providers.close()

        try:
            asyncio.run(run_worker())
        except KeyboardInterrupt:
            pass
    elif args.command == "backup":
        if args.path.exists() or args.path.resolve() == store.path.resolve():
            parser.error("备份目标必须是尚不存在的新路径")
        store.backup(args.path)
        print(json.dumps({"status": "COMPLETE", "path": str(args.path)}))
    elif args.command == "storage-report":
        print(json.dumps(store.diagnostics(), indent=2))
    elif args.command == "runtime":
        print(json.dumps(runtime_health(store, utc_now()), indent=2))
    elif args.command == "research-queue":
        print(
            json.dumps(
                ResearchEngine(store, settings, None).queue(utc_now()), ensure_ascii=False, indent=2
            )
        )
    elif args.command == "collect":
        result = asyncio.run(collect_once(settings, store))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result["status"] not in ("COMPLETE", "BUSY"):
            raise SystemExit(1)
    elif args.command == "validate":
        validate_pending(store, utc_now(), settings, retry_missing=args.retry_missing)
        print(json.dumps(validation_report(store, settings), ensure_ascii=False, indent=2))
    elif args.command == "quality-report":
        print(
            json.dumps(quality_contexts(store, utc_now(), settings), ensure_ascii=False, indent=2)
        )
    elif args.command == "replay":
        print(
            json.dumps(replay(store, args.start, args.end, settings), ensure_ascii=False, indent=2)
        )
    elif args.command == "intelligence":
        result = asyncio.run(intelligence_once(settings, store, args.scope, args.sources))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "free-data":
        if not 1 <= args.seconds <= 3600:
            parser.error("--seconds 必须为 1–3600")
        result = asyncio.run(free_data_once(settings, store, args.scope, args.seconds))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result["status"] not in {"COMPLETE", "BUSY"}:
            raise SystemExit(1)
    elif args.command == "import-context":
        if args.path.stat().st_size > settings.research_max_bytes:
            parser.error("输入文件过大")
        item = ContextInput.model_validate_json(args.path.read_text())
        print(import_context(store, item, utc_now()).model_dump_json(indent=2))
    elif args.command in {"import-email", "review-research"}:
        engine = ResearchEngine(store, settings, None)
        if args.path.stat().st_size > settings.research_max_bytes:
            parser.error("输入文件过大")
        if args.command == "import-email":
            result = engine.import_email(args.path.read_bytes(), utc_now())
        else:
            result = engine.review(
                args.document_id,
                ResearchReview.model_validate_json(args.path.read_text()),
                utc_now(),
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "experiment-report":
        print(json.dumps(ranking_report(store, utc_now(), settings), ensure_ascii=False, indent=2))
    elif args.command == "train":
        archive = IntelligenceStore(store)
        at = utc_now()
        token = archive.acquire("model-training", at, 3600)
        if not token:
            parser.error("模型训练正在进行")
        try:
            result = train_model(store, at, settings, args.horizon)
        finally:
            archive.release("model-training", token)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "framework":
        with store.connect() as db:
            schema = db.execute("PRAGMA user_version").fetchone()[0]
        print(
            json.dumps(
                {
                    "schema_version": schema,
                    "phases": [2, 3, 4, 5, 6],
                    "research_trigger": "MANUAL_ONLY",
                    "network_collection_performed": False,
                    "meme_enabled": settings.enable_meme_discovery,
                    "social_enabled": settings.enable_social,
                    "models": "REQUIRES_MATURE_HISTORY",
                    "database": str(store.path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
