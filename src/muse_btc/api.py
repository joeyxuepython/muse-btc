import asyncio
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AwareDatetime, BaseModel, Field

from .alerts import (
    DELIVERY_POLICY_VERSION,
    alert_view,
    change_alert,
    market_risk_status,
    notification_projections,
    pre_pump_delivery_status,
    public_scores,
)
from .async_io import run_sync
from .btc_data import BTCDataEngine
from .btc_intelligence import apply_btc_context, btc_assessment
from .config import Settings
from .context import asset_contexts, import_context
from .decision_explanation import VERSION as DECISION_EXPLANATION_VERSION
from .decisions import decide
from .delivery import build_deliveries, notification_message
from .entry_quality import VERSION as ENTRY_QUALITY_VERSION
from .entry_quality import quality_contexts
from .events import EventEngine
from .experiments import paid_evaluation, ranking_report, train_model
from .intelligence import ContextInput, IntelligenceStore
from .macro import MacroEngine
from .microstructure import cvd_summary
from .models import ProviderState, utc_now
from .providers import Providers
from .research import ResearchEngine, ResearchReview
from .rules import market_regime, quote_usable, usable
from .service import Collector, signal_view
from .social import SocialEngine
from .storage import Store
from .strategy_audit import report as strategy_report
from .validation import validation_report
from .worker import IntelligenceWorker, runtime_health

STATIC = Path(__file__).parent / "static"


class ResearchCheckRequest(BaseModel):
    sources: list[Literal["Glassnode", "Coinbase", "CoinShares", "Santiment", "arXiv"]] | None = (
        None
    )


class EmailImportRequest(BaseModel):
    content: str = Field(min_length=20, max_length=2000000)


class TrainRequest(BaseModel):
    horizon_seconds: Literal[14400, 86400, 259200, 604800] = 14400


class FreeDataRequest(BaseModel):
    scope: Literal["all", "onchain", "options", "macro", "liquidations"] = "all"
    liquidation_seconds: int = Field(default=10, ge=1, le=60)


class NotificationReceipt(BaseModel):
    notification_ids: list[str] = Field(min_length=1, max_length=500)
    status: Literal["SENT", "FAILED", "SKIPPED"]
    message_id: str | None = Field(default=None, max_length=300)
    reason: str | None = Field(default=None, max_length=1000)


def create_app(settings: Settings | None = None, providers_factory=Providers) -> FastAPI:
    config = settings or Settings()
    store = Store(config.database_path)
    providers = providers_factory(config, store)
    collector = Collector(config, store, providers)
    archive = IntelligenceStore(store)
    # Custom provider factories used by integration tests may implement only market APIs.
    public = getattr(providers, "public", None)
    research = ResearchEngine(store, config, public)
    macro = MacroEngine(store, config, public)
    btc_data = BTCDataEngine(store, config, public)
    social = SocialEngine(store, config, providers)

    def ranking_views(now):
        rows = store.rankings(now)
        for row in rows:
            snapshot = store.snapshot(row["snapshot_id"])
            row["as_of_data_ready"] = row["data_ready"]
            row["data_ready"] = bool(snapshot and usable(snapshot, now, config))
            row["quote_fresh"] = bool(snapshot and quote_usable(snapshot, now, config))
        return rows

    def current_regime(btc, now):
        regime = market_regime(btc, now, config)
        return (
            apply_btc_context(regime, store, config, now, btc)
            if config.enable_intelligence
            else regime
        )

    def alert_responses(alerts, now, regime=None, latest=None):
        if not alerts:
            return []
        snapshots = store.snapshots_by_ids([a["snapshot_id"] for a in alerts])
        if latest is None:
            latest = {
                s.asset_id: s
                for s in store.latest_snapshots(
                    now, asset_ids=list({a["asset_id"] for a in alerts} | {"binance:BTCUSDT"})
                )
            }
        if regime is None and any(a["level"] == "STRONG" for a in alerts):
            # One BTC assessment per request, independent of the number of alerts.
            regime = current_regime(latest.get("binance:BTCUSDT"), now)
        results = []
        guarded_assets = {
            a["asset_id"]
            for a in alerts
            if a["level"] == "STRONG" and a["rule_id"] in ("spot-led-momentum", "pre-pump-fusion")
        }
        contexts = (
            asset_contexts(archive, guarded_assets, now)
            if guarded_assets and config.enable_intelligence
            else {}
        )
        qualities = (
            quality_contexts(store, now, config, list(latest.values()))
            if guarded_assets
            and (config.enable_entry_quality or config.market_confirmation_mode == "require")
            else {}
        )
        current_candidates = {}
        for asset_id in guarded_assets:
            current = latest.get(asset_id)
            if current and qualities:
                current_candidates[asset_id] = {
                    s.rule_id: s
                    for s in decide(
                        current,
                        regime,
                        now,
                        config,
                        store,
                        context=contexts.get(asset_id),
                        quality=qualities.get(asset_id),
                    )
                }
        for alert in notification_projections(store, alerts, now):
            result = alert_view(alert, now)
            snapshot = snapshots.get(alert["snapshot_id"])
            current = latest.get(alert["asset_id"])
            result["data_current"] = bool(snapshot and usable(snapshot, now, config))
            if alert["level"] == "STRONG":
                result["data_current"] &= bool(
                    current and usable(current, now, config) and regime.risk_mode == "NORMAL"
                )
            result["lifecycle_state"] = result["state"]
            fresh = bool(current and quote_usable(current, now, config))
            result["current_price"] = current.price if fresh else None
            result["current_price_market_time"] = (
                current.market_time.isoformat() if current else None
            )
            result["current_quote_fresh"] = fresh
            result["current_risk_status"] = (
                market_risk_status(alert, current, now, config)
                if alert["notification_class"] == "MARKET_RISK"
                else None
            )
            if alert["notification_class"] in ("MARKET_RISK", "CANCELLATION"):
                result["data_current"] &= bool(current and usable(current, now, config))
            result["current_risk_inputs"] = (
                {
                    k: getattr(current.features, k)
                    for k in (
                        "return_15m_pct",
                        "spot_taker_buy_ratio",
                        "funding_rate_pct",
                        "oi_change_5m_pct",
                    )
                }
                if current and result["data_current"]
                else {}
            )
            price = alert.get("notification_price")
            result["price_change_since_notification_pct"] = (
                round((current.price / price - 1) * 100, 6) if fresh and price else None
            )
            result["delivery_guard"] = (
                pre_pump_delivery_status(alert, result["current_price"], now)
                if alert["level"] == "STRONG" and alert["rule_id"] == "pre-pump-fusion"
                else None
            )
            if (
                alert["level"] == "STRONG"
                and fresh
                and alert.get("invalidation_price")
                and current.price < alert["invalidation_price"]
            ):
                result["delivery_guard"] = "SKIP_INVALIDATION_REACHED"
            result["current_asset_context"] = (
                contexts.get(alert["asset_id"])
                if alert["level"] == "STRONG"
                and alert["rule_id"] in ("spot-led-momentum", "pre-pump-fusion")
                else None
            )
            if result["current_asset_context"] and result["current_asset_context"]["risks"]:
                result["delivery_guard"] = "SKIP_CONTEXT_BLOCKED"
            if alert["asset_id"] in current_candidates and alert["level"] == "STRONG":
                candidate = current_candidates[alert["asset_id"]].get(alert["rule_id"])
                result["current_entry_assessment"] = qualities.get(alert["asset_id"])
                if (candidate is None or candidate.kind != "ENTRY_CANDIDATE") and result[
                    "delivery_guard"
                ] in (None, "READY"):
                    result["delivery_guard"] = "SKIP_ENTRY_NOT_CONFIRMED"
            if result["state"] == "ACTIVE" and (
                not result["data_current"]
                or result["delivery_guard"] not in (None, "READY")
                or result["current_risk_status"] not in (None, "READY")
                or result.get("cancellation_delivery_status") not in (None, "READY")
            ):
                result["state"] = "PAUSED"
                result["unread"] = False
            deadline = datetime.fromisoformat(
                alert.get("notification_expires_at", alert["expires_at"])
            )
            if alert["notification_class"] in ("MARKET_RISK", "CANCELLATION"):
                deadline = min(
                    deadline,
                    datetime.fromisoformat(alert.get("notification_at", alert["first_seen"]))
                    + timedelta(seconds=config.risk_notification_max_age_seconds),
                )
            result["notification_eligible"] = bool(
                result["state"] == "ACTIVE"
                and deadline > now
                and alert["notification_class"] != "ARCHIVE"
                and alert.get("notification_id")
                and alert.get("price_provenance") != "LEGACY_UNKNOWN"
            )
            category = {
                "OPPORTUNITY": "LONG",
                "MARKET_RISK": "RISK",
                "CANCELLATION": "CANCELLATION",
            }.get(alert["notification_class"])
            result["message_zh"] = (
                notification_message([result], category)
                if category and result["notification_eligible"]
                else None
            )
            results.append(result)
        return results

    def alert_response(alert, now, regime=None):
        return alert_responses([alert], now, regime)[0]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await collector.start()
        worker_providers = (
            providers_factory(config, store) if config.enable_background_intelligence else None
        )
        task = (
            asyncio.create_task(IntelligenceWorker(store, config, worker_providers).run())
            if worker_providers
            else None
        )
        try:
            yield
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if worker_providers:
                await worker_providers.close()
            await collector.stop()

    app = FastAPI(title="Muse Crypto Intelligence", version="0.2.0", lifespan=lifespan)
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
    async def health():
        return {
            "status": "ok",
            "database": "NOT_CHECKED",
            "scope": "PROCESS_LIVENESS",
            "collector": collector.progress(),
            "version": "0.2.0",
            "delivery_policy_version": DELIVERY_POLICY_VERSION,
            "decision_explanation_version": DECISION_EXPLANATION_VERSION,
            "entry_quality_version": ENTRY_QUALITY_VERSION,
            "collector_running": bool(collector.task and not collector.task.done()),
        }

    @app.get("/ready")
    def ready():
        now = utc_now()
        snapshots = store.latest_snapshots(now)
        statuses = {s.name: s for s in store.statuses()}
        required = ["Binance Spot"]
        available = all(
            name in statuses
            and statuses[name].state == ProviderState.READY
            and (now - statuses[name].checked_at).total_seconds() <= config.stale_seconds
            for name in required
        )
        btc = next((s for s in snapshots if s.asset_id == "binance:BTCUSDT"), None)
        eth = next((s for s in snapshots if s.asset_id == "binance:ETHUSDT"), None)
        fresh_quotes = [
            s
            for s in snapshots
            if s.symbol in providers.universe_symbols
            and s.source == "Binance"
            and s.market_time <= now
            and (now - s.market_time).total_seconds() <= config.stale_seconds
        ]
        if (
            not available
            or not btc
            or not usable(btc, now, config)
            or not eth
            or not usable(eth, now, config)
            or len(fresh_quotes) < config.max_altcoins + 2
            or sum(usable(s, now, config) for s in fresh_quotes) < config.max_altcoins + 2
            or (
                config.enable_background_intelligence
                and runtime_health(store, now)["worker"]["status"] != "RUNNING"
            )
        ):
            return JSONResponse(
                {
                    "status": "degraded",
                    "market_data_ready": False,
                    "fresh_quotes": len(fresh_quotes),
                    "target": config.max_altcoins + 2,
                    "coverage": providers.coverage,
                },
                status_code=503,
            )
        derivatives = statuses.get("OKX Futures") or statuses.get("Binance Futures")
        return {
            "status": "ready",
            "market_data_ready": True,
            "scope": "USDT_SPOT_MONITORING",
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
        regime = current_regime(btc, now)
        assets = []
        active_symbols = set(providers.universe_symbols)
        for snapshot in snapshots:
            if (
                active_symbols
                and snapshot.source == "Binance"
                and snapshot.symbol not in active_symbols
            ):
                continue
            item = snapshot.model_dump(mode="json", exclude={"candles"})
            item["age_seconds"] = max(0, (now - snapshot.available_at).total_seconds())
            item["data_usable"] = usable(snapshot, now, config)
            item["quote_fresh"] = quote_usable(snapshot, now, config)
            item["data_health"] = (
                "FRESH" if item["data_usable"] else "DELAYED" if item["quote_fresh"] else "STALE"
            )
            item["monitoring_active"] = snapshot.module != "MEME" or config.enable_meme_discovery
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
            "coverage": providers.coverage,
            "universe": store.universe(now),
            "rankings": ranking_views(now),
            "alerts": alert_responses(
                store.alerts(limit=200), now, regime, {s.asset_id: s for s in snapshots}
            ),
            "request_metrics": providers.metrics,
            "as_of": now.isoformat(),
            "regime": regime,
            "assets": assets,
            "signals": [
                signal_view(
                    store, s, now, config, {item.asset_id: item for item in snapshots}, regime
                )
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

    @app.get("/api/universe")
    def universe():
        return store.universe()

    @app.get("/api/rankings")
    def rankings():
        return ranking_views(utc_now())

    @app.get("/api/alerts")
    def alerts(
        level: Literal["INFO", "WATCH", "SETUP", "STRONG", "CRITICAL_RISK"] | None = None,
        unread: bool = False,
        pinned: bool = False,
        limit: int = Query(default=200, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        since: AwareDatetime | None = None,
    ):
        now = utc_now()
        items = store.alerts(
            level=level,
            limit=limit,
            offset=offset,
            since=since,
            unread=unread,
            pinned=pinned,
            now=now,
        )
        return [a for a in alert_responses(items, now) if not unread or a["unread"]]

    @app.get("/api/alerts/notifications")
    def alert_notifications(
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=500),
        through: int | None = Query(default=None, ge=0),
        generation: str | None = None,
    ):
        now = utc_now()
        try:
            page = store.notification_page(after, limit, through, generation)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        latest_alerts = store.alerts_by_ids([a["id"] for a in page["items"]])
        page["items"] = notification_projections(
            store, [public_scores(a) for a in page["items"]], now
        )
        receipts = store.notification_receipts([a["notification_id"] for a in page["items"]])
        current = {a["id"]: a for a in alert_responses(list(latest_alerts.values()), now)}
        for item in page["items"]:
            item["receipt"] = receipts.get(item["notification_id"], {})
            live = current.get(item["id"])
            item["current_alert_state"] = live["state"] if live else "MISSING"
            item["current_level"] = live["level"] if live else None
            item["current_price"] = live["current_price"] if live else None
            item["current_price_market_time"] = live["current_price_market_time"] if live else None
            item["data_current"] = bool(live and live["data_current"])
            item["current_quote_fresh"] = bool(live and live["current_quote_fresh"])
            item["current_risk_inputs"] = live.get("current_risk_inputs", {}) if live else {}
            item["current_asset_context"] = live.get("current_asset_context") if live else None
            item["current_entry_assessment"] = (
                live.get("current_entry_assessment") if live else None
            )
            price = item.get("notification_price")
            gain = (
                round((item["current_price"] / price - 1) * 100, 6)
                if item["current_price"] and price
                else None
            )
            item["price_change_since_notification_pct"] = gain
            item["notification_age_seconds"] = max(
                0, (now - datetime.fromisoformat(item["notification_at"])).total_seconds()
            )
            deadline = item.get("notification_expires_at", item["expires_at"])
            if item["notification_class"] in ("MARKET_RISK", "CANCELLATION"):
                deadline = min(
                    datetime.fromisoformat(deadline),
                    datetime.fromisoformat(item["notification_at"])
                    + timedelta(seconds=config.risk_notification_max_age_seconds),
                ).isoformat()
                item["notification_expires_at"] = deadline
            status = "READY"
            if not live:
                status = "SKIP_MISSING"
            elif live["lifecycle_state"] != "ACTIVE":
                status = "SKIP_" + live["lifecycle_state"]
            elif live.get("notification_id") and live["notification_id"] != item["notification_id"]:
                status = "SKIP_SUPERSEDED"
            elif live["level"] != item["level"]:
                status = "SKIP_LEVEL_CHANGED"
            elif datetime.fromisoformat(deadline) <= now:
                status = "SKIP_EXPIRED"
            elif item.get("price_provenance") == "LEGACY_UNKNOWN":
                status = "LEGACY_REVIEW_REQUIRED"
            elif item["notification_class"] == "ARCHIVE":
                status = "SKIP_ARCHIVE_ONLY"
            elif live.get("delivery_guard") == "SKIP_CONTEXT_BLOCKED":
                status = "SKIP_CONTEXT_BLOCKED"
            elif live.get("delivery_guard") == "SKIP_ENTRY_NOT_CONFIRMED":
                status = "SKIP_ENTRY_NOT_CONFIRMED"
            elif not item["data_current"] or not item["current_quote_fresh"]:
                status = "SKIP_STALE_DATA"
            elif item["notification_class"] == "CANCELLATION":
                status = item["cancellation_delivery_status"]
            elif item["notification_class"] == "MARKET_RISK":
                status = live["current_risk_status"]
            elif (
                item.get("invalidation_price")
                and item["current_price"] < item["invalidation_price"]
            ):
                status = "SKIP_INVALIDATION_REACHED"
            elif item["level"] == "STRONG" and item["rule_id"] == "pre-pump-fusion":
                status = pre_pump_delivery_status(item, item["current_price"], now)
            item["delivery_status"] = status
        page["as_of"] = now.isoformat()
        page["delivery_policy_version"] = DELIVERY_POLICY_VERSION
        page["decision_explanation_version"] = DECISION_EXPLANATION_VERSION
        page["delivery_groups_preview"] = build_deliveries(page["items"])
        page["grouping_scope"] = "PAGE_PREVIEW_ONLY_DRAIN_BATCH_BEFORE_GROUPING"
        return page

    @app.post("/api/alerts/notifications/receipts")
    def notification_receipts(receipt: NotificationReceipt):
        try:
            store.save_notification_receipt(
                receipt.notification_ids,
                receipt.status,
                receipt.message_id,
                receipt.reason,
                utc_now(),
            )
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return store.notification_receipts(receipt.notification_ids)

    @app.get("/api/strategies")
    def strategies(asset_id: str | None = None, limit: int = Query(default=100, ge=1, le=5000)):
        return strategy_report(store, config, utc_now(), asset_id=asset_id, limit=limit)

    @app.get("/api/alerts/{alert_id}")
    def alert_detail(alert_id: str):
        alert = store.alert(alert_id)
        if not alert:
            raise HTTPException(404, "预警不存在")
        snapshot = store.snapshot(alert["snapshot_id"])
        return {
            "alert": alert_response(alert, utc_now()),
            "snapshot": snapshot,
            "events": store.alert_events(alert_id),
        }

    @app.post("/api/alerts/{alert_id}/{action}")
    def update_alert(alert_id: str, action: Literal["read", "unread", "pin", "unpin", "resolve"]):
        result = change_alert(store, alert_id, action, utc_now())
        if not result:
            raise HTTPException(404, "预警不存在")
        return result

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

    @app.get("/api/assets/{asset_id}/cvd")
    def cvd(asset_id: str, window_seconds: int = 3600):
        if not 60 <= window_seconds <= 86400:
            raise HTTPException(422, "CVD 窗口必须为 60–86400 秒")
        return cvd_summary(store, asset_id, utc_now(), window_seconds)

    @app.get("/api/raw/{raw_id}")
    def raw_detail(raw_id: str):
        raw = store.raw(raw_id)
        if not raw:
            raise HTTPException(404, "原始观察不存在")
        return raw

    @app.get("/api/validation")
    def validation():
        return validation_report(store, config)

    @app.get("/api/quality")
    def entry_quality():
        now = utc_now()
        return {
            "version": ENTRY_QUALITY_VERSION,
            "as_of": now.isoformat(),
            "enabled": config.enable_entry_quality,
            "market_confirmation_mode": config.market_confirmation_mode,
            "assets": quality_contexts(store, now, config),
            "production_accuracy": "NOT_ESTABLISHED",
        }

    @app.get("/api/intelligence")
    def intelligence_overview():
        now = utc_now()
        meme_engine = getattr(providers, "meme_engine", None)
        return {
            "as_of": now.isoformat(),
            "macro": macro.summary(now),
            "btc": btc_data.summary(now),
            "social": social.summary(now),
            "meme": meme_engine.dashboard(now)
            if meme_engine
            else {"tokens": [], "holders": [], "early_buyers": [], "wallets": []},
            "research_documents": [
                r.model_dump(mode="json", exclude={"data": {"body"}})
                for r in archive.records("research", now, limit=100)
            ],
            "research_notices": research.notices(),
            "research_checks": archive.checks(),
            "models": [
                r.model_dump(
                    mode="json", exclude={"data": {"training_snapshot_ids", "test_snapshot_ids"}}
                )
                for r in archive.records("model", now, limit=20)
            ],
            "phase_status": {
                "2": "FRAMEWORK_REQUIRES_SOURCE_ACCEPTANCE",
                "3": "RULES_RESEARCH_ONLY",
                "4": "PARTIAL_PUBLIC_DISCOVERY",
                "5": "REQUIRES_CREDENTIALS_OR_IMPORT",
                "6": "REQUIRES_MATURE_HISTORY",
            },
            "scheduled_research": config.enable_background_intelligence
            and "research" in config.background_scopes,
            "btc_assessment": btc_assessment(store, config, now),
            "strategies": strategy_report(store, config, now, limit=1500),
            "events": EventEngine(store, config, public).calendar(now),
            "event_reactions": macro.event_reactions(now),
            "event_checks": store.state("event_checks"),
            "runtime": runtime_health(store, now),
            "meme_collection_enabled": config.enable_meme_discovery,
            "social_collection_enabled": config.enable_social,
        }

    @app.get("/api/evidence/{kind}")
    def evidence(kind: str, as_of: datetime | None = None, limit: int = 100):
        if as_of and (not as_of.tzinfo or as_of > utc_now()):
            raise HTTPException(422, "as_of 必须含时区且不能在未来")
        return archive.records(kind, as_of or utc_now(), limit=limit)

    @app.post("/api/context/import")
    def context_import(item: ContextInput):
        try:
            return import_context(store, item, utc_now())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/intelligence/collect/{scope}")
    async def collect_intelligence(scope: Literal["macro", "events", "social", "meme", "btc"]):
        if not public:
            raise HTTPException(503, "当前 provider 未提供公开研究接口")
        if scope == "macro":
            return await macro.collect()
        if scope == "events":
            return await EventEngine(store, config, public).collect()
        if scope == "btc":
            return await btc_data.collect()
        if scope == "social":
            return await social.collect()
        snapshots = await providers.memes()
        for snapshot in snapshots:
            await run_sync(store.save_snapshot, snapshot)
        return {"snapshots": len(snapshots), "enabled": config.enable_meme_discovery}

    @app.get("/api/btc")
    def btc_free_data():
        return btc_data.summary(utc_now())

    @app.get("/api/btc/onchain/{metric}")
    def btc_onchain_series(metric: str, source: Literal["Coin Metrics", "BGeometrics"]):
        return btc_data.series(utc_now(), source, metric)

    @app.post("/api/btc/collect")
    async def collect_btc_free_data(item: FreeDataRequest):
        if not public:
            raise HTTPException(503, "当前 provider 未提供公开数据接口")
        return await btc_data.collect(item.scope, item.liquidation_seconds)

    @app.post("/api/research/check")
    async def check_research(item: ResearchCheckRequest):
        if not public:
            raise HTTPException(503, "当前 provider 未提供研究接口")
        return await research.check(item.sources)

    @app.get("/api/research/documents/{document_id}")
    def research_document(document_id: str):
        result = archive.by_id(document_id, utc_now())
        if not result or result.kind != "research":
            raise HTTPException(404, "研究文档不存在")
        return result

    @app.post("/api/research/documents/{document_id}/review")
    def review_research(document_id: str, item: ResearchReview):
        try:
            return research.review(document_id, item, utc_now())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/research/import-email")
    def email_import(item: EmailImportRequest):
        try:
            return research.import_email(item.content.encode(), utc_now())
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, "邮件格式、来源或原始链接无法验证") from exc

    @app.get("/api/research/queue")
    def research_queue():
        return research.queue(utc_now())

    @app.get("/api/runtime")
    def runtime_status():
        return runtime_health(store, utc_now())

    @app.get("/api/btc/assessment")
    def assessment():
        return btc_assessment(store, config, utc_now())

    @app.get("/api/macro/calendar")
    def calendar():
        return EventEngine(store, config, public).calendar(utc_now())

    @app.get("/api/macro/reactions")
    def macro_reactions():
        return macro.event_reactions(utc_now())

    @app.get("/api/experiments/report")
    async def experiment_report():
        return await asyncio.to_thread(ranking_report, store, utc_now(), config)

    @app.post("/api/experiments/train")
    async def experiment_train(item: TrainRequest):
        at = utc_now()
        token = await run_sync(archive.acquire, "model-training", at, 3600)
        if not token:
            return {"status": "BUSY"}
        try:
            return await run_sync(train_model, store, at, config, item.horizon_seconds)
        finally:
            await run_sync(archive.release, "model-training", token)

    @app.post("/api/experiments/paid-evaluation")
    def evaluate_paid_source(item: dict):
        try:
            return paid_evaluation(store, item, utc_now())
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(422, "付费来源评估格式无效或样本不足") from exc

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

    @app.websocket("/api/live")
    async def live(websocket: WebSocket):
        origin = websocket.headers.get("origin")
        scheme = "https" if websocket.url.scheme == "wss" else "http"
        if origin and origin != f"{scheme}://{websocket.headers.get('host')}":
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            auth = await asyncio.wait_for(websocket.receive_json(), timeout=10)
            supplied = auth.get("token", "") if isinstance(auth, dict) else ""
            if config.api_token and (
                not isinstance(supplied, str)
                or not secrets.compare_digest(supplied, config.api_token.get_secret_value())
            ):
                await websocket.close(code=1008)
                return
            last_update = object()
            while True:
                finished = collector.last_result.get("finished_at")
                if last_update != finished:
                    await websocket.send_json({"type": "update", "finished_at": finished})
                    last_update = finished
                try:
                    message = await asyncio.wait_for(websocket.receive(), timeout=3)
                    if message["type"] == "websocket.disconnect":
                        return
                except TimeoutError:
                    pass
        except (WebSocketDisconnect, TimeoutError):
            return

    return app
