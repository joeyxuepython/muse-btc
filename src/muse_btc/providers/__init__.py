import asyncio
import os
import ssl
import time
from datetime import datetime, timedelta
from typing import Any

import httpx

from ..config import Settings
from ..models import (
    Candle,
    ProviderState,
    ProviderStatus,
    Snapshot,
    TokenRisk,
    utc_now,
)
from ..storage import Store
from .binance_spot import BinanceSpotProvider
from .common import ProviderError, milliseconds, number
from .okx_derivatives import OKXDerivativesProvider


def candles_from_binance(rows: list) -> list[Candle]:
    return [
        Candle(
            open_time=milliseconds(row[0]),
            close_time=milliseconds(row[6]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            quote_volume=float(row[7]),
            taker_buy_quote_volume=float(row[10]),
        )
        for row in rows
    ]


def token_risk(data: dict | None, chain: str) -> TokenRisk:
    if not data:
        return TokenRisk(
            missing_checks=["合约卖出限制", "增发与管理权限", "持仓集中度", "LP 锁定"],
            warnings=[f"{chain} 的合约风险数据尚不可用"],
        )
    blockers, warnings, missing = [], [], []
    checks = {}
    for field, label in [
        ("is_honeypot", "蜜罐标记"),
        ("cannot_sell_all", "无法全部卖出"),
        ("is_blacklisted", "黑名单机制"),
    ]:
        value = str(data.get(field, "unknown"))
        checks[label] = value
        if value == "1":
            blockers.append(label)
        elif value != "0":
            missing.append(label)
    for field, label in [
        ("is_mintable", "可增发"),
        ("is_proxy", "可升级代理合约"),
        ("can_take_back_ownership", "可收回所有权"),
        ("owner_change_balance", "管理者可修改余额"),
    ]:
        value = str(data.get(field, "unknown"))
        checks[label] = value
        if value == "1":
            blockers.append(label)
        elif value != "0":
            missing.append(label)
    for field, label in [("buy_tax", "买入税"), ("sell_tax", "卖出税")]:
        tax = number(data.get(field))
        checks[label] = f"{tax:.1%}" if tax is not None else "unknown"
        if tax is None:
            missing.append(label)
        elif tax > 0.10:
            blockers.append(f"{label}超过 10%")
    if str(data.get("is_open_source", "")) != "1":
        missing.append("合约源码公开")
    holders = data.get("holders")
    top10 = None
    if isinstance(holders, list) and holders:
        ordinary = [
            h
            for h in holders
            if str(h.get("is_contract", "0")) != "1"
            and str(h.get("address", "")).lower()
            not in {
                "0x0000000000000000000000000000000000000000",
                "0x000000000000000000000000000000000000dead",
            }
        ]
        portions = [number(h.get("percent")) for h in ordinary[:10]]
        if portions and all(value is not None for value in portions):
            top10 = sum(value for value in portions if value is not None) * 100
            if top10 > 50:
                blockers.append(f"公开列表前十个非合约地址占比 {top10:.1f}%")
        else:
            missing.append("持仓集中度")
    else:
        missing.append("持仓集中度")
    lp_holders = data.get("lp_holders")
    locked = None
    if isinstance(lp_holders, list) and lp_holders:
        portions = [
            number(h.get("percent"))
            for h in lp_holders
            if str(h.get("is_locked", "0")) == "1"
            or str(h.get("address", "")).lower().endswith("000000dead")
        ]
        if portions and all(v is not None for v in portions):
            locked = sum(v for v in portions if v is not None)
    if locked is None:
        missing.append("LP 锁定／销毁比例")
    elif locked < 0.8:
        blockers.append("公开 LP 锁定／销毁比例低于 80%")
    else:
        checks["LP 锁定／销毁比例"] = f"{locked:.0%}"
    warnings.append("公开安全检查不能排除关联地址、刷量或未来权限变化")
    return TokenRisk(
        status="BLOCKED" if blockers else ("NEEDS_VERIFICATION" if missing else "SCREENED"),
        blockers=blockers,
        warnings=warnings,
        missing_checks=missing,
        checks=checks,
        top10_holder_pct=top10,
    )


def okx_inst_id(symbol: str) -> str | None:
    """Binance 现货符号映射到 OKX 永续合约，如 BTCUSDT -> BTC-USDT-SWAP。"""
    if not symbol.endswith("USDT") or len(symbol) <= 4 or not symbol[:-4].isalnum():
        return None
    return f"{symbol[:-4]}-USDT-SWAP"


class Providers:
    def __init__(
        self, settings: Settings, store: Store, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.settings, self.store = settings, store
        # Retain normal certificate verification, including platform-injected CA trust.
        context = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE"))
        self.client = httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            verify=context,
            transport=transport,
            mounts=self._proxy_mounts(settings, context),
            follow_redirects=False,
        )
        self.semaphore = asyncio.Semaphore(4)
        self.request_locks: dict[str, asyncio.Lock] = {}
        self.next_request: dict[str, float] = {}
        self.metrics: dict[str, dict] = {}
        self.backoff: dict[str, datetime] = {}
        self.exchange_cache: tuple | None = None
        self.universe_symbols: list[str] = []
        self.universe_raw_id: str | None = None
        self.detail_cursor = 0
        self.coverage: dict = {}
        self.futures_errors: set[str] = set()
        self.futures_source_used: set[str] = set()
        self.derivatives = OKXDerivativesProvider(settings, store, self.get)
        self.spot = BinanceSpotProvider(settings, store, self.get, self.derivatives)
        from ..meme import MemeEngine
        from .public_intelligence import PublicIntelligence

        self.public = PublicIntelligence(self)
        self.meme_engine = MemeEngine(self)
        if self.spot.selection:
            self.universe_symbols = [r["binance_symbol"] for r in self.spot.selection["entries"]]

    @staticmethod
    def _proxy_mounts(
        settings: Settings, context: ssl.SSLContext
    ) -> dict[str, httpx.AsyncBaseTransport] | None:
        """Binance 域名走独立代理（如新加坡节点），其余域名保持默认出口。

        httpx 会把这里的 mounts 合并到环境代理的默认路由表之上，
        因此未命中的域名不受影响。代理地址以 SecretStr 保存，不进入日志。
        """
        raw = settings.binance_proxy.get_secret_value().strip() if settings.binance_proxy else ""
        if not raw:
            return None
        mounts: dict[str, httpx.AsyncBaseTransport] = {}
        for base in {settings.binance_spot_url, settings.binance_futures_url}:
            host = httpx.URL(base).host
            if host:
                mounts[f"https://{host}"] = httpx.AsyncHTTPTransport(proxy=raw, verify=context)
        return mounts or None

    async def close(self) -> None:
        await self.client.aclose()

    def status(self, name: str, state: ProviderState, message: str, coverage: str) -> None:
        self.store.save_status(
            ProviderStatus(name=name, state=state, message=message, coverage=coverage)
        )

    def futures_label(self) -> str:
        return "OKX Futures"

    async def get(
        self, source: str, base: str, path: str, params: dict | None = None
    ) -> tuple[Any, str, datetime]:
        if source in self.backoff and self.backoff[source] > utc_now():
            raise ProviderError("请求已进入限流退避，稍后自动重试")
        async with self.semaphore:
            if source in self.backoff and self.backoff[source] > utc_now():
                raise ProviderError("请求已进入限流退避，稍后自动重试")
            instrument = (params or {}).get("instId", "")
            key = base + path + str(instrument)
            interval = 0.4 if "/rubik/" in path else 0.1
            lock = self.request_locks.setdefault(key, asyncio.Lock())
            async with lock:
                delay = max(0, self.next_request.get(key, 0) - time.monotonic())
                if delay:
                    await asyncio.sleep(delay)
                self.next_request[key] = time.monotonic() + interval
            metric = self.metrics.setdefault(source, {"requests": 0, "errors": 0})
            metric["requests"] += 1
            started = time.monotonic()
            try:
                response = await self.client.get(base + path, params=params)
            except httpx.HTTPError as exc:
                metric["errors"] += 1
                if "403" in str(exc):
                    raise ProviderError("云网络代理拒绝访问（403），请检查允许域名") from exc
                raise ProviderError(f"连接失败：{type(exc).__name__}") from exc
            metric["last_latency_seconds"] = round(time.monotonic() - started, 4)
            metric["last_http_status"] = response.status_code
            metric["last_checked_at"] = utc_now().isoformat()
            if response.status_code in (418, 429):
                metric["errors"] += 1
                retry = number(response.headers.get("Retry-After")) or 120
                self.backoff[source] = utc_now() + timedelta(seconds=min(max(retry, 30), 3600))
                raise ProviderError("数据源限流，已暂停该源请求并安排退避")
            if response.status_code in (403, 451):
                metric["errors"] += 1
                raise ProviderError(f"访问受限 HTTP {response.status_code}（网络或地区策略）")
            if not response.is_success:
                metric["errors"] += 1
                raise ProviderError(f"公开接口返回 HTTP {response.status_code}")
            try:
                payload = response.json()
                received_at = utc_now()
                raw_id = self.store.save_raw(source, str(response.url), payload, received_at)
            except (ValueError, TypeError) as exc:
                metric["errors"] += 1
                raise ProviderError("接口未返回有效 JSON 数据") from exc
            if isinstance(payload, dict) and isinstance(payload.get("code"), int):
                if payload["code"] < 0:
                    metric["errors"] += 1
                    raise ProviderError(f"数据源错误代码 {payload['code']}")
            if isinstance(payload, dict) and payload.get("code") in ("50011", "50040"):
                metric["errors"] += 1
                self.backoff[source] = utc_now() + timedelta(seconds=120)
                raise ProviderError("OKX 已返回限流代码，安排退避")
            if source == "OKX Futures" and isinstance(payload, dict) and payload.get("code") != "0":
                metric["errors"] += 1
                raise ProviderError("OKX 返回错误代码或无效数据结构")
            return payload, raw_id, received_at

    async def binance(self) -> list[Snapshot]:
        self.coverage = {
            "target": self.settings.max_altcoins + 2,
            "quotes": 0,
            "details": 0,
            "updated_at": None,
            "missing_quotes": self.universe_symbols,
        }
        try:
            snapshots = await self.spot.collect()
            self.coverage = self.spot.coverage
            self.universe_symbols = self.coverage["symbols"]
            self.futures_source_used = {"OKX"}
            self.status(
                "Binance Spot",
                ProviderState.READY
                if len(snapshots) == self.settings.max_altcoins + 2
                else ProviderState.DEGRADED,
                (
                    f"报价 {len(snapshots)}/{self.settings.max_altcoins + 2}；"
                    f"详细数据 {self.coverage['details']}"
                ),
                "BTC/ETH 核心 + 动态 100 山寨现货；分层采集",
            )
            complete = sum(
                s.features.oi_change_5m_pct is not None and s.features.funding_rate_pct is not None
                for s in snapshots
            )
            self.status(
                "OKX Futures",
                ProviderState.READY
                if complete == len(snapshots) and snapshots
                else ProviderState.DEGRADED
                if self.derivatives.open_interest
                else ProviderState.UNAVAILABLE,
                (
                    f"当前 OI {len(self.derivatives.open_interest)} 个合约；"
                    f"OI/Funding 特征 {complete}/{len(snapshots)}"
                ),
                "OKX USDT SWAP；公开 OI 历史、Funding、Taker、普通/精英多空比；实际清算总额不可用",
            )
            return snapshots
        except (ProviderError, ValueError, KeyError, TypeError) as exc:
            message = str(exc) if isinstance(exc, ProviderError) else "现货或监控池验证失败"
            self.status("Binance Spot", ProviderState.UNAVAILABLE, message, "V4 BTC/ETH + 山寨现货")
            return []

    async def memes(self) -> list[Snapshot]:
        return await self.meme_engine.collect()

    async def _meme_asset(self, chain: str, address: str, profile_raw: str) -> Snapshot | None:
        return None
