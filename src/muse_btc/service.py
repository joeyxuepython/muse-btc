import asyncio
import logging
from datetime import datetime

from .alerts import publish_alert
from .config import Settings
from .fusion import enrich_rankings, pre_pump_signals
from .microstructure import archive_snapshot_trades
from .models import (
    Module,
    ProviderState,
    ProviderStatus,
    Regime,
    Signal,
    SignalEvent,
    SignalKind,
    Snapshot,
    new_id,
    utc_now,
)
from .providers import Providers
from .ranking import rank_assets
from .rules import evaluate, market_regime, usable
from .storage import Store
from .validation import validate_pending

logger = logging.getLogger(__name__)


class Collector:
    def __init__(self, settings: Settings, store: Store, providers: Providers):
        self.settings, self.store, self.providers = settings, store, providers
        self.lock = asyncio.Lock()
        self.task: asyncio.Task | None = None
        self.last_finished_at: datetime | None = None
        self.last_result: dict = {}

    def initialise_statuses(self) -> None:
        for name, coverage in [
            ("Liquidations", "完整清算记录；尚未接入 WebSocket 自行积累"),
            ("Wallet Intelligence", "钱包关联、聪明钱与独立持有人识别"),
            ("Solana Security", "mint/freeze 权限、持仓与 LP 风险；尚未接入 RPC"),
        ]:
            self.store.save_status(
                ProviderStatus(
                    name=name, state=ProviderState.DISABLED, message="首版未启用", coverage=coverage
                )
            )
        for name in ("DEX Screener", "GoPlus"):
            self.providers.status(
                name,
                ProviderState.NEEDS_VERIFICATION
                if self.settings.enable_meme_discovery
                else ProviderState.DISABLED,
                "Phase 4 框架；等待云端按配置采集",
                "独立 Meme 发现池",
            )
        for name, coverage in [
            ("Macro", "公开宏观、ETF、稳定币及版本归档"),
            ("Research", "公开报告 / 邮件导入 / 中文审阅；手动触发检查"),
            ("Social", "X API 或导入；需要凭据与历史样本"),
            ("ML Ranking", "离线训练 / 校准 / 样本外验证；不自动升级"),
        ]:
            self.providers.status(
                name, ProviderState.NEEDS_VERIFICATION, "框架已接入，等待云端数据与验收", coverage
            )
        self.providers.status(
            "Binance Futures", ProviderState.DISABLED, "V4 合约统一使用 OKX；保留旧历史", "历史归档"
        )

    async def start(self) -> None:
        self.initialise_statuses()
        if self.settings.enable_collector:
            self.task = asyncio.create_task(self._loop(), name="muse-collector")

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        await self.providers.close()

    async def _loop(self) -> None:
        while True:
            await self.collect_once()
            await asyncio.sleep(self.settings.poll_seconds)

    async def collect_once(self) -> dict:
        if self.lock.locked():
            return {"status": "BUSY", "message": "采集正在进行，请等待本轮结束"}
        async with self.lock:
            try:
                binance = await self.providers.binance()
                memes = await self.providers.memes() if self.settings.enable_meme_discovery else []
                now = utc_now()
                eth = next((s for s in binance if s.module == Module.ETH), None)
                btc = next((s for s in binance if s.module == Module.BTC), None)
                saved = []

                def aligned(snapshot, core):
                    if (
                        not core
                        or not usable(snapshot, now, self.settings)
                        or not usable(core, now, self.settings)
                    ):
                        return False
                    source = snapshot.component_times.get("candles", snapshot.market_time)
                    reference = core.component_times.get("candles", core.market_time)
                    return (
                        abs((source - reference).total_seconds())
                        <= self.settings.time_alignment_seconds
                    )

                for snapshot in binance + memes:
                    snapshot.decision_at = now
                    snapshot.features.relative_strength_15m_pct = None
                    snapshot.features.relative_strength_eth_15m_pct = None
                    if (
                        snapshot.module == Module.ALT
                        and aligned(snapshot, btc)
                        and snapshot.features.return_15m_pct is not None
                        and btc.features.return_15m_pct is not None
                    ):
                        snapshot.features.relative_strength_15m_pct = (
                            snapshot.features.return_15m_pct - btc.features.return_15m_pct
                        )
                    if (
                        snapshot.module == Module.ALT
                        and aligned(snapshot, eth)
                        and snapshot.features.return_15m_pct is not None
                        and eth.features.return_15m_pct is not None
                    ):
                        snapshot.features.relative_strength_eth_15m_pct = (
                            snapshot.features.return_15m_pct - eth.features.return_15m_pct
                        )
                    try:
                        if self.settings.enable_intelligence and snapshot.module != Module.MEME:
                            try:
                                archive_snapshot_trades(
                                    self.store, snapshot, self.store.universe(now)
                                )
                            except (ValueError, KeyError, TypeError, OverflowError):
                                snapshot.quality_issues.append("TRADE_ARCHIVE_INVALID")
                        self.store.save_snapshot(snapshot)
                        saved.append(snapshot)
                    except ValueError:
                        logger.warning("Rejected snapshot: future timestamp or invalid lineage")
                regime = market_regime(btc if btc in saved else None, now, self.settings)
                self.store.save_regime(regime)
                ranking = rank_assets(saved, self.store.rankings(now), now, self.settings)
                if self.settings.enable_intelligence:
                    ranking = enrich_rankings(ranking, saved, self.store, now, self.settings)
                self.store.save_rankings(ranking, now)
                signal_count = self._process_signals(saved, regime, now)
                outcomes = validate_pending(self.store, now, self.settings)
                self.last_finished_at = now
                self.last_result = {
                    "status": "COMPLETE" if saved else "NO_DATA",
                    "snapshots": len(saved),
                    "signals": signal_count,
                    "outcomes": outcomes,
                    "finished_at": now.isoformat(),
                }
            except Exception:
                logger.exception("Collection cycle failed")
                self.last_result = {"status": "FAILED", "message": "本轮失败，下一轮自动重试"}
            return self.last_result

    def _process_signals(self, snapshots: list[Snapshot], regime: Regime, now: datetime) -> int:
        count = 0
        by_asset = {s.asset_id: s for s in snapshots}
        for old in self.store.signals(limit=100000, as_of=now):
            if old.module == Module.MEME:
                continue
            if old.kind not in (SignalKind.WATCH, SignalKind.ENTRY_CANDIDATE):
                continue
            events = self.store.signal_events(old.id)
            if events and events[-1].state != "ACTIVE":
                continue
            snapshot = by_asset.get(old.asset_id)
            reason = None
            if old.expires_at <= now:
                self.store.add_event(
                    SignalEvent(
                        signal_id=old.id,
                        state="EXPIRED",
                        event_at=old.expires_at,
                        reason="超过信号有效期",
                    )
                )
                continue
            if snapshot and usable(snapshot, now, self.settings):
                if old.invalidation_price and snapshot.price < old.invalidation_price:
                    reason = "价格跌破失效参考位"
                elif snapshot.risk and snapshot.risk.blockers:
                    reason = "代币风险检查出现阻断项"
            if regime.risk_mode in ("RISK_OFF", "LEVERAGE_OVERHEAT"):
                reason = f"BTC 风险升至 {regime.risk_mode}"
            if reason:
                self.store.add_event(
                    SignalEvent(
                        signal_id=old.id,
                        state="INVALIDATED",
                        event_at=now,
                        reason=reason,
                        snapshot_id=snapshot.id if snapshot else None,
                    )
                )
                reference = snapshot or self.store.snapshot(old.snapshot_id)
                if reference:
                    invalidated = old.model_copy(
                        update={
                            "id": new_id(),
                            "kind": SignalKind.INVALIDATED,
                            "emitted_at": now,
                            "snapshot_id": reference.id,
                            "title": "原信号失效",
                            "evidence": [reason],
                            "entry_zone": None,
                            "reference_price": reference.price,
                            "parent_signal_id": old.id,
                            "btc_snapshot_id": regime.btc_snapshot_id,
                        }
                    )
                    self.store.save_signal(invalidated)
                    publish_alert(self.store, invalidated, None, now, self.settings)
                    count += 1
        for snapshot in snapshots:
            candidates = evaluate(snapshot, regime, now, self.settings)
            if self.settings.enable_intelligence:
                candidates += pre_pump_signals(snapshot, regime, now, self.settings, self.store)
            for signal in candidates:
                rank = next(
                    (r for r in self.store.rankings(now) if r["asset_id"] == snapshot.asset_id),
                    None,
                )
                previous = self.store.last_signal_time(signal.asset_id, signal.rule_id, signal.kind)
                if (
                    previous
                    and (now - previous).total_seconds() < self.settings.alert_cooldown_seconds
                ):
                    # Web lifecycle updates independently from legacy signal-row cooldown.
                    existing = self.store.signal(signal.id)
                    if not existing:
                        active = next(
                            (
                                a
                                for a in self.store.alerts()
                                if a["asset_id"] == signal.asset_id
                                and a["rule_id"] == signal.rule_id
                            ),
                            None,
                        )
                        if active:
                            signal.id = active["signal_id"]
                            publish_alert(self.store, signal, rank, now, self.settings)
                    continue
                self.store.save_signal(signal)
                publish_alert(self.store, signal, rank, now, self.settings)
                count += 1
        return count


def signal_view(
    store: Store,
    signal: Signal,
    now: datetime,
    settings: Settings,
    current_assets: dict[str, Snapshot] | None = None,
) -> dict:
    result = signal.model_dump(mode="json")
    events = store.signal_events(signal.id)
    state = events[-1].state if events else "ACTIVE"
    if state == "ACTIVE" and signal.expires_at <= now:
        state = "EXPIRED"
    result["lifecycle_state"] = state
    if current_assets is None:
        current_assets = {s.asset_id: s for s in store.latest_snapshots(now)}
    current = current_assets.get(signal.asset_id)
    data_current = bool(current and usable(current, now, settings))
    if signal.kind == SignalKind.ENTRY_CANDIDATE:
        regime = market_regime(current_assets.get("binance:BTCUSDT"), now, settings)
        data_current = data_current and regime.risk_mode == "NORMAL"
    if signal.module == Module.MEME:
        data_current = False
        state = "ARCHIVED"
    result["data_current"] = data_current
    if (
        state == "ACTIVE"
        and not data_current
        and signal.kind in (SignalKind.WATCH, SignalKind.ENTRY_CANDIDATE)
    ):
        state = "PAUSED"
    result["state"] = state
    result["events"] = [event.model_dump(mode="json") for event in events]
    return result
