"""Independent, bounded forward-label evaluation; never runs research or market requests."""

import asyncio
import logging

from .async_io import run_sync
from .intelligence import IntelligenceStore
from .models import utc_now
from .validation import validation_batch

VERSION = "validation-worker-v1"
logger = logging.getLogger(__name__)


class ValidationWorker:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.progress = {"version": VERSION, "status": "NOT_STARTED"}

    async def run(self):
        archive = IntelligenceStore(self.store)
        token = await run_sync(archive.acquire, "validation-worker", utc_now(), 120)
        if not token:
            self.progress.update(status="BUSY")
            return
        owner = asyncio.current_task()

        async def heartbeat():
            while True:
                await asyncio.sleep(30)
                now = utc_now()
                if not await run_sync(archive.renew, "validation-worker", token, now):
                    owner.cancel()
                    return
                self.progress["heartbeat_at"] = now.isoformat()
                await run_sync(self.store.set_state, "validation_worker", dict(self.progress))

        pulse = asyncio.create_task(heartbeat())
        try:
            while True:
                now = utc_now()
                if not await run_sync(archive.renew, "validation-worker", token, now):
                    self.progress.update(status="LOST_LEASE")
                    return
                self.progress.update(status="RUNNING", heartbeat_at=now.isoformat())
                await run_sync(self.store.set_state, "validation_worker", dict(self.progress))
                try:
                    result = await run_sync(
                        validation_batch,
                        self.store,
                        now,
                        self.settings,
                        limit=self.settings.validation_batch_size,
                        budget_seconds=self.settings.validation_budget_seconds,
                    )
                    self.progress.update(last_batch=result, error_type=None)
                except Exception as exc:
                    logger.exception("Forward validation batch failed")
                    self.progress.update(error_type=type(exc).__name__)
                self.progress["queue"] = await run_sync(self.store.validation_queue, utc_now())
                self.progress["heartbeat_at"] = utc_now().isoformat()
                await run_sync(self.store.set_state, "validation_worker", dict(self.progress))
                await asyncio.sleep(self.settings.validation_tick_seconds)
        finally:
            pulse.cancel()
            try:
                await pulse
            except asyncio.CancelledError:
                pass
            await run_sync(archive.release, "validation-worker", token)
            self.progress.update(status="STOPPED", heartbeat_at=utc_now().isoformat())
            await run_sync(self.store.set_state, "validation_worker", dict(self.progress))
