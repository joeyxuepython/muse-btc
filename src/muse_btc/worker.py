"""Opt-in application worker with durable run state and renewable process ownership."""

import asyncio
import math
from datetime import datetime

from .async_io import run_sync
from .btc_data import BTCDataEngine
from .events import EventEngine
from .intelligence import EvidenceRecord, IntelligenceStore
from .macro import MacroEngine
from .models import utc_now
from .providers.common import milliseconds
from .research import ResearchEngine
from .social import SocialEngine


class IntelligenceWorker:
    def __init__(self, store, settings, providers):
        self.store, self.settings, self.providers = store, settings, providers
        self.archive = IntelligenceStore(store)
        public = getattr(providers, "public", None)
        btc = BTCDataEngine(store, settings, public)
        self.jobs = {
            "macro": (
                settings.intelligence_refresh_seconds,
                lambda: MacroEngine(store, settings, public).collect(),
            ),
            "events": (
                settings.event_refresh_seconds,
                lambda: EventEngine(store, settings, public).collect(),
            ),
            "onchain": (settings.btc_onchain_refresh_seconds, lambda: btc.collect("onchain")),
            "options": (settings.btc_options_refresh_seconds, lambda: btc.collect("options")),
            "research": (3600, lambda: ResearchEngine(store, settings, public).check()),
            "social": (300, lambda: SocialEngine(store, settings, providers).collect()),
            "event_quotes": (30, self.event_quote),
        }
        self.running = {}

    async def event_quote(self):
        payload, raw, at = await self.providers.get(
            "Binance Spot",
            self.settings.binance_spot_url,
            "/api/v3/ticker/24hr",
            {"symbol": "BTCUSDT"},
        )
        market = milliseconds(payload["closeTime"])
        price = float(payload["lastPrice"])
        if (
            not math.isfinite(price)
            or price <= 0
            or not 0
            <= (at - market).total_seconds()
            <= self.settings.event_reaction_tolerance_seconds
        ):
            return {"status": "DEGRADED", "reason": "BTC quote stale"}
        await run_sync(
            self.archive.save,
            EvidenceRecord(
                kind="btc_event_quote",
                key=market.isoformat(),
                source="Binance Spot",
                market_time=market,
                available_at=at,
                raw_ids=[raw],
                data={"price": price},
            ),
        )
        return {"status": "COMPLETE"}

    async def execute(self, name):
        started = utc_now()
        prior = await run_sync(self.store.state, "job:" + name) or {}
        state = {**prior, "name": name, "started_at": started.isoformat(), "status": "RUNNING"}
        await run_sync(self.store.set_state, "job:" + name, state)
        try:
            result = await self.jobs[name][1]()
            state.update(status=result.get("status", "DEGRADED"), result=result)
            if state["status"] == "COMPLETE":
                state["last_success_at"] = utc_now().isoformat()
        except asyncio.CancelledError:
            state["status"] = "INTERRUPTED"
            raise
        except Exception as exc:
            state.update(status="FAILED", result={"error_type": type(exc).__name__})
        finally:
            state["finished_at"] = utc_now().isoformat()
            state["failures"] = 0 if state["status"] == "COMPLETE" else prior.get("failures", 0) + 1
            await run_sync(self.store.set_state, "job:" + name, state)

    async def tick(self):
        scopes = list(self.settings.background_scopes)
        if "events" in scopes:
            scopes.append("event_quotes")
        for name in scopes:
            if name in self.running and not self.running[name].done():
                continue
            prior = await run_sync(self.store.state, "job:" + name) or {}
            anchor = prior.get("started_at" if prior.get("status") == "COMPLETE" else "finished_at")
            elapsed = (
                (utc_now() - datetime.fromisoformat(anchor)).total_seconds()
                if anchor
                else float("inf")
            )
            wait = (
                self.jobs[name][0]
                if prior.get("status") == "COMPLETE"
                else min(900, 30 * 2 ** min(prior.get("failures", 0), 5))
            )
            if elapsed >= wait:
                self.running[name] = asyncio.create_task(self.execute(name))

    async def run(self):
        token = await run_sync(self.archive.acquire, "intelligence-worker", utc_now(), 120)
        if not token:
            return {"status": "BUSY"}
        try:
            while True:
                if not await run_sync(self.archive.renew, "intelligence-worker", token, utc_now()):
                    return {"status": "LOST_LEASE"}
                await run_sync(
                    self.store.set_state,
                    "intelligence_worker",
                    {
                        "heartbeat_at": utc_now().isoformat(),
                        "status": "RUNNING",
                        "scopes": self.settings.background_scopes,
                    },
                )
                await self.tick()
                await asyncio.sleep(min(self.settings.worker_tick_seconds, 30))
        finally:
            for task in self.running.values():
                task.cancel()
            await asyncio.gather(*self.running.values(), return_exceptions=True)
            await run_sync(self.archive.release, "intelligence-worker", token)
            await run_sync(
                self.store.set_state,
                "intelligence_worker",
                {
                    "heartbeat_at": utc_now().isoformat(),
                    "status": "STOPPED",
                    "scopes": self.settings.background_scopes,
                },
            )


def runtime_health(store, now):
    worker = store.state("intelligence_worker") or {"status": "NOT_STARTED"}
    if (
        worker.get("status") == "RUNNING"
        and (now - datetime.fromisoformat(worker["heartbeat_at"])).total_seconds() > 120
    ):
        worker = worker | {"status": "STALE"}
    market = store.state("market_heartbeat")
    return {
        "worker": worker,
        "market_heartbeat": market,
        "market_heartbeat_stale": market is None
        or (now - datetime.fromisoformat(market)).total_seconds() > 120,
        "market_last_result": store.state("market_last_result"),
        "jobs": [
            store.state("job:" + name)
            for name in (
                "macro",
                "events",
                "onchain",
                "options",
                "research",
                "social",
                "event_quotes",
            )
            if store.state("job:" + name)
        ],
    }
