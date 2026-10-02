import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .models import ProviderState, utc_now
from .providers import Providers
from .rules import market_regime, usable
from .service import Collector, signal_view
from .storage import Store
from .validation import validation_report

STATIC = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None, providers_factory=Providers) -> FastAPI:
    config = settings or Settings()
    store = Store(config.database_path)
    providers = providers_factory(config, store)
    collector = Collector(config, store, providers)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await collector.start()
        yield
        await collector.stop()

    app = FastAPI(title="Muse Crypto Intelligence", version="0.1.0", lifespan=lifespan)
    app.state.store, app.state.collector, app.state.settings = store, collector, config

    @app.middleware("http")
    async def protect_api(request: Request, call_next):
        if request.url.path.startswith("/api/") and config.api_token:
            expected = "Bearer " + config.api_token.get_secret_value()
            if not secrets.compare_digest(request.headers.get("authorization", ""), expected):
                return JSONResponse({"detail": "需要访问口令"}, status_code=401)
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "拒绝跨站请求"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "database": "ok",
            "version": "0.1.0",
            "collector_running": bool(collector.task and not collector.task.done()),
        }

    @app.get("/ready")
    def ready():
        now = utc_now()
        snapshots = store.latest_snapshots(now)
        statuses = {s.name: s for s in store.statuses()}
        required = ["Binance Spot", "DEX Screener"]
        available = all(
            name in statuses
            and statuses[name].state == ProviderState.READY
            and (now - statuses[name].checked_at).total_seconds() <= config.stale_seconds
            for name in required
        )
        btc = next((s for s in snapshots if s.asset_id == "binance:BTCUSDT"), None)
        if not available or not btc or not usable(btc, now, config):
            return JSONResponse({"status": "degraded", "market_data_ready": False}, status_code=503)
        derivatives = statuses.get("Binance Futures")
        return {
            "status": "ready",
            "market_data_ready": True,
            "scope": "SPOT_AND_DEX_MONITORING",
            "derivatives_ready": bool(
                derivatives
                and derivatives.state == ProviderState.READY
                and (now - derivatives.checked_at).total_seconds() <= config.stale_seconds
            ),
        }

    @app.get("/api/overview")
    def overview():
        now = utc_now()
        snapshots = store.latest_snapshots(now)
        btc = next((s for s in snapshots if s.asset_id == "binance:BTCUSDT"), None)
        assets = []
        for snapshot in snapshots:
            item = snapshot.model_dump(mode="json", exclude={"candles"})
            item["age_seconds"] = max(0, (now - snapshot.available_at).total_seconds())
            item["data_usable"] = usable(snapshot, now, config)
            item["trend"] = [c.close for c in snapshot.candles[-60:]]
            assets.append(item)
        statuses = []
        for status in store.statuses():
            item = status.model_dump(mode="json")
            item["stale"] = (now - status.checked_at).total_seconds() > config.stale_seconds
            if item["stale"] and status.state == ProviderState.READY:
                item["state"] = ProviderState.DEGRADED
                item["message"] = "检查结果已过期，等待本轮重新验证"
            statuses.append(item)
        return {
            "as_of": now.isoformat(),
            "regime": market_regime(btc, now, config),
            "assets": assets,
            "signals": [
                signal_view(store, s, now, config, {item.asset_id: item for item in snapshots})
                for s in store.signals()
            ],
            "providers": statuses,
            "counts": store.counts(),
            "collector": {
                "enabled": config.enable_collector,
                "busy": collector.lock.locked(),
                "poll_seconds": config.poll_seconds,
                "last_result": collector.last_result,
            },
            "validation_status": "OBSERVATION_ONLY",
        }

    @app.get("/api/signals")
    def signals(module: str | None = None, kind: str | None = None, limit: int = 200):
        now = utc_now()
        items = store.signals(limit=min(max(limit, 1), 1000))
        return [
            signal_view(store, s, now, config)
            for s in items
            if (not module or s.module == module) and (not kind or s.kind == kind)
        ]

    @app.get("/api/signals/{signal_id}")
    def signal_detail(signal_id: str):
        signal = store.signal(signal_id)
        if not signal:
            raise HTTPException(404, "提醒不存在")
        snapshot = store.snapshot(signal.snapshot_id)
        return {
            "signal": signal_view(store, signal, utc_now(), config),
            "snapshot": snapshot,
            "outcomes": [o for o in store.outcomes() if o.signal_id == signal.id],
        }

    @app.get("/api/assets/{asset_id}")
    def asset_detail(asset_id: str):
        snapshot = next(
            (s for s in store.latest_snapshots(utc_now()) if s.asset_id == asset_id), None
        )
        if not snapshot:
            raise HTTPException(404, "标的不存在")
        return snapshot

    @app.get("/api/raw/{raw_id}")
    def raw_detail(raw_id: str):
        raw = store.raw(raw_id)
        if not raw:
            raise HTTPException(404, "原始观察不存在")
        return raw

    @app.get("/api/validation")
    def validation():
        return validation_report(store, config)

    @app.get("/api/export")
    def export():
        response = JSONResponse(
            {
                "exported_at": utc_now().isoformat(),
                "signals": [s.model_dump(mode="json") for s in store.signals(limit=100000)],
                "outcomes": [o.model_dump(mode="json") for o in store.outcomes()],
            }
        )
        response.headers["Content-Disposition"] = 'attachment; filename="muse-signals.json"'
        return response

    @app.post("/api/collect")
    async def collect():
        return await collector.collect_once()

    return app
