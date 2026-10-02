import argparse
import asyncio
import json
from datetime import datetime

import uvicorn

from .config import Settings
from .models import utc_now
from .providers import Providers
from .service import Collector
from .storage import Store
from .validation import replay, validate_pending, validation_report


def parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise argparse.ArgumentTypeError("时间必须包含时区，例如 2026-10-01T00:00:00Z")
    return result


async def collect_once(settings: Settings, store: Store) -> dict:
    providers = Providers(settings, store)
    collector = Collector(settings, store, providers)
    collector.initialise_statuses()
    try:
        return await collector.collect_once()
    finally:
        await providers.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Muse BTC / Altcoin / Meme monitoring")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="启动网页、API 和后台采集")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("collect", help="执行一轮真实公开数据采集")
    sub.add_parser("validate", help="计算已到期提醒的前瞻表现")
    backtest = sub.add_parser("replay", help="对真实归档快照进行时间点重放")
    backtest.add_argument("--start", required=True, type=parse_time)
    backtest.add_argument("--end", required=True, type=parse_time)
    args = parser.parse_args()
    settings = Settings()
    if args.command == "serve":
        from .api import create_app

        if args.host not in ("127.0.0.1", "localhost", "::1") and not settings.api_token:
            parser.error("对外监听时请在 .env 设置 MUSE_API_TOKEN")
        uvicorn.run(create_app(settings), host=args.host, port=args.port)
        return
    store = Store(settings.database_path)
    if args.command == "collect":
        result = asyncio.run(collect_once(settings, store))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result["status"] not in ("COMPLETE", "BUSY"):
            raise SystemExit(1)
    elif args.command == "validate":
        validate_pending(store, utc_now(), settings)
        print(json.dumps(validation_report(store, settings), ensure_ascii=False, indent=2))
    elif args.command == "replay":
        print(
            json.dumps(replay(store, args.start, args.end, settings), ensure_ascii=False, indent=2)
        )


if __name__ == "__main__":
    main()
