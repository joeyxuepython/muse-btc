import asyncio
import json
import os
import ssl
import statistics
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .config import Settings
from .features import candle_features, depth_features, derivatives_features
from .models import (
    Candle,
    Features,
    Module,
    ProviderState,
    ProviderStatus,
    Snapshot,
    TokenRisk,
    utc_now,
)
from .storage import Store


class ProviderError(Exception):
    """Sanitized public provider failure, safe to expose in the dashboard."""


def milliseconds(value: int | float) -> datetime:
    return datetime.fromtimestamp(value / 1000, UTC)


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


def number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
        return result if float("-inf") < result < float("inf") else None
    except (TypeError, ValueError):
        return None


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
        self.backoff: dict[str, datetime] = {}
        self.exchange_cache: tuple | None = None
        self.universe_symbols: list[str] = []
        self.universe_raw_id: str | None = None
        self.futures_errors: set[str] = set()
        self.futures_source_used: set[str] = set()

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
        """本轮实际提供衍生品数据的来源，如 'OKX Futures'；无数据时用配置名。"""
        used = sorted(self.futures_source_used)
        if not used:
            fallback = {"binance": "Binance", "okx": "OKX", "auto": "Binance/OKX"}
            used = [fallback.get(self.settings.futures_source, "OKX")]
        return " + ".join(f"{name} Futures" for name in used)

    async def get(
        self, source: str, base: str, path: str, params: dict | None = None
    ) -> tuple[Any, str, datetime]:
        if source in self.backoff and self.backoff[source] > utc_now():
            raise ProviderError("请求已进入限流退避，稍后自动重试")
        async with self.semaphore:
            if source in self.backoff and self.backoff[source] > utc_now():
                raise ProviderError("请求已进入限流退避，稍后自动重试")
            try:
                response = await self.client.get(base + path, params=params)
            except httpx.HTTPError as exc:
                if "403" in str(exc):
                    raise ProviderError("云网络代理拒绝访问（403），请检查允许域名") from exc
                raise ProviderError(f"连接失败：{type(exc).__name__}") from exc
            if response.status_code in (418, 429):
                from datetime import timedelta

                retry = number(response.headers.get("Retry-After")) or 120
                self.backoff[source] = utc_now() + timedelta(seconds=min(max(retry, 30), 3600))
                raise ProviderError("数据源限流，已暂停该源请求并安排退避")
            if response.status_code in (403, 451):
                raise ProviderError(f"访问受限 HTTP {response.status_code}（网络或地区策略）")
            if not response.is_success:
                raise ProviderError(f"公开接口返回 HTTP {response.status_code}")
            try:
                payload = response.json()
                received_at = utc_now()
                raw_id = self.store.save_raw(source, str(response.url), payload, received_at)
            except (ValueError, TypeError) as exc:
                raise ProviderError("接口未返回有效 JSON 数据") from exc
            if isinstance(payload, dict) and isinstance(payload.get("code"), int):
                if payload["code"] < 0:
                    raise ProviderError(f"数据源错误代码 {payload['code']}")
            return payload, raw_id, received_at

    async def binance(self) -> list[Snapshot]:
        try:
            self.futures_errors.clear()
            self.futures_source_used.clear()
            refresh = (
                not self.exchange_cache
                or not self.universe_symbols
                or (utc_now() - self.exchange_cache[2]).total_seconds()
                >= self.settings.universe_refresh_seconds
            )
            if refresh:
                ticker_result, exchange_result = await asyncio.gather(
                    self.get("Binance Spot", self.settings.binance_spot_url, "/api/v3/ticker/24hr"),
                    self.get(
                        "Binance Spot", self.settings.binance_spot_url, "/api/v3/exchangeInfo"
                    ),
                )
            else:
                exchange_result = self.exchange_cache
                ticker_result = await self.get(
                    "Binance Spot",
                    self.settings.binance_spot_url,
                    "/api/v3/ticker/24hr",
                    {"symbols": json.dumps(self.universe_symbols, separators=(",", ":"))},
                )
            tickers, ticker_raw, _ = ticker_result
            info, exchange_raw, _ = exchange_result
            if not isinstance(tickers, list) or not isinstance(info.get("symbols"), list):
                raise ProviderError("交易所返回的数据结构不符合预期")
            stable = {
                "USDC",
                "FDUSD",
                "TUSD",
                "USDP",
                "DAI",
                "BUSD",
                "USD1",
                "USDE",
                "USDS",
                "USDD",
                "PYUSD",
            }
            leveraged = {
                coin + suffix
                for coin in (
                    "BTC",
                    "ETH",
                    "BNB",
                    "ADA",
                    "XRP",
                    "LINK",
                    "DOT",
                    "EOS",
                    "LTC",
                    "TRX",
                    "UNI",
                    "YFI",
                )
                for suffix in ("UP", "DOWN", "BULL", "BEAR")
            }
            symbols = {
                row["symbol"]
                for row in info["symbols"]
                if row.get("status") == "TRADING"
                and row.get("quoteAsset") == "USDT"
                and row.get("baseAsset") not in stable
                and row.get("isSpotTradingAllowed", True)
                and row.get("baseAsset") not in leveraged
            }
            eligible = [row for row in tickers if row.get("symbol") in symbols]
            by_name = {row["symbol"]: row for row in eligible}
            alt = sorted(
                (row for row in eligible if row["symbol"] != "BTCUSDT"),
                key=lambda row: number(row.get("quoteVolume")) or 0,
                reverse=True,
            )
            selected = [by_name["BTCUSDT"]] if "BTCUSDT" in by_name else []
            selected += alt[: self.settings.max_altcoins]
            if not selected or selected[0]["symbol"] != "BTCUSDT":
                raise ProviderError("BTCUSDT 未在可交易现货列表中")
            if refresh:
                self.exchange_cache = exchange_result
                self.universe_symbols = [row["symbol"] for row in selected]
                self.universe_raw_id = ticker_raw
            common_raw = list(dict.fromkeys([ticker_raw, exchange_raw, self.universe_raw_id]))
            results = await asyncio.gather(
                *(self._binance_asset(row, common_raw) for row in selected),
                return_exceptions=True,
            )
            snapshots = [row for row in results if isinstance(row, Snapshot)]
            errors = [row for row in results if isinstance(row, Exception)]
            self.status(
                "Binance Spot",
                ProviderState.DEGRADED if errors else ProviderState.READY,
                f"已获取 {len(snapshots)}/{len(selected)} 个现货标的"
                + (f"；失败：{type(errors[0]).__name__}" if errors else ""),
                "BTC + USDT 现货成交量前列；名单每小时更新，原始筛选依据保留",
            )
            with_futures = [
                row
                for row in snapshots
                if row.features.funding_rate_pct is not None
                and row.features.oi_change_5m_pct is not None
            ]
            okx_used = "OKX" in self.futures_source_used
            label = self.futures_label()
            for stale in ("Binance Futures", "OKX Futures"):
                if stale != label and stale not in label:
                    self.store.delete_status(stale)
            self.status(
                label,
                ProviderState.READY
                if len(with_futures) == len(snapshots) and snapshots
                else ProviderState.DEGRADED
                if with_futures
                else ProviderState.UNAVAILABLE,
                f"完整衍生品特征 {len(with_futures)}/{len(snapshots)} 个标的"
                + ("；" + "; ".join(sorted(self.futures_errors)) if self.futures_errors else ""),
                "Funding、跨所 Basis、OI 5m（本地累积）；无合约主动成交与完整清算历史"
                if okx_used
                else "Funding、Basis、OI 5m、合约主动成交；未提供完整清算历史",
            )
            return snapshots
        except (ProviderError, ValueError, KeyError, TypeError) as exc:
            message = str(exc) if isinstance(exc, ProviderError) else "现货响应结构验证失败"
            self.status(
                "Binance Spot", ProviderState.UNAVAILABLE, message, "BTC 与 USDT 山寨币现货"
            )
            self.status(
                self.futures_label(),
                ProviderState.UNAVAILABLE,
                "现货采集未完成，本轮未形成联动特征",
                "USD-M 合约" if "OKX" not in self.futures_source_used else "USDT 永续合约",
            )
            return []

    async def _binance_asset(self, ticker: dict, common_raw: list[str]) -> Snapshot:
        symbol = ticker["symbol"]
        base = self.settings.binance_spot_url
        spot_rows, book_result = await asyncio.gather(
            self.get(
                "Binance Spot",
                base,
                "/api/v3/klines",
                {"symbol": symbol, "interval": "1m", "limit": 180},
            ),
            self.get("Binance Spot", base, "/api/v3/depth", {"symbol": symbol, "limit": 100}),
        )
        rows, candle_raw, spot_received_at = spot_rows
        book, book_raw, book_received_at = book_result
        at = max(spot_received_at, book_received_at)
        raw_ids = common_raw + [candle_raw, book_raw]
        candles = candles_from_binance(rows)
        features, issues = candle_features(candles, spot_received_at)
        depth_features(book, features)
        at = await self._collect_derivatives(
            symbol, float(ticker["lastPrice"]), features, issues, raw_ids, at
        )
        closed = [c for c in candles if c.close_time <= spot_received_at]
        if not closed:
            raise ProviderError("没有可用的已收盘 K 线")
        market_time = milliseconds(int(ticker["closeTime"]))
        return Snapshot(
            asset_id=f"binance:{symbol}",
            symbol=symbol,
            module=Module.BTC if symbol == "BTCUSDT" else Module.ALT,
            source="Binance",
            market_time=market_time,
            available_at=at,
            price=float(ticker["lastPrice"]),
            quote_volume_24h=float(ticker["quoteVolume"]),
            features=features,
            raw_ids=raw_ids,
            quality_issues=issues,
            candles=closed,
        )

    def _futures_sources(self) -> list[str]:
        mode = self.settings.futures_source
        if mode == "binance":
            return ["binance"]
        if mode == "okx":
            return ["okx"]
        return ["binance", "okx"]

    async def _collect_derivatives(
        self,
        symbol: str,
        spot_price: float,
        features: Features,
        issues: list[str],
        raw_ids: list[str],
        at: datetime,
    ) -> datetime:
        """按配置选择衍生品数据源；auto 模式下 Binance 失败自动回退 OKX。"""
        for name in self._futures_sources():
            if name == "binance":
                at, ok = await self._binance_derivatives(symbol, features, issues, raw_ids, at)
            else:
                at, ok = await self._okx_derivatives(
                    symbol, spot_price, features, issues, raw_ids, at
                )
            if ok:
                self.futures_source_used.add("Binance" if name == "binance" else "OKX")
                return at
        issues.append("DERIVATIVES_UNAVAILABLE")
        return at

    async def _binance_derivatives(
        self,
        symbol: str,
        features: Features,
        issues: list[str],
        raw_ids: list[str],
        at: datetime,
    ) -> tuple[datetime, bool]:
        try:
            futures_base = self.settings.binance_futures_url
            results = await asyncio.gather(
                self.get(
                    "Binance Futures", futures_base, "/fapi/v1/premiumIndex", {"symbol": symbol}
                ),
                self.get(
                    "Binance Futures",
                    futures_base,
                    "/futures/data/openInterestHist",
                    {"symbol": symbol, "period": "5m", "limit": 30},
                ),
                self.get(
                    "Binance Futures",
                    futures_base,
                    "/fapi/v1/klines",
                    {"symbol": symbol, "interval": "1m", "limit": 30},
                ),
                return_exceptions=True,
            )
            for value in results:
                if isinstance(value, tuple):
                    raw_ids.append(value[1])
                    at = max(at, value[2])
            if all(isinstance(value, tuple) for value in results):
                mark, oi, perp = (value[0] for value in results)
                mark_at = milliseconds(int(mark["time"]))
                if (
                    not 0
                    <= (results[0][2] - mark_at).total_seconds()
                    <= self.settings.stale_seconds
                ):
                    issues.append("DERIVATIVES_TIMESTAMP_STALE_OR_FUTURE")
                else:
                    oi = [
                        point
                        for point in oi
                        if int(point["timestamp"]) <= results[1][2].timestamp() * 1000
                    ]
                    perp_closed = [
                        c for c in candles_from_binance(perp) if c.close_time <= results[2][2]
                    ]
                    derivatives_features(mark, oi, perp_closed, features, at)
            else:
                self.futures_errors.update(str(v) for v in results if isinstance(v, ProviderError))
        except (ProviderError, KeyError, TypeError, ValueError):
            pass
        ok = features.funding_rate_pct is not None and features.oi_change_5m_pct is not None
        return at, ok

    async def _okx_derivatives(
        self,
        symbol: str,
        spot_price: float,
        features: Features,
        issues: list[str],
        raw_ids: list[str],
        at: datetime,
    ) -> tuple[datetime, bool]:
        """OKX 公开接口：funding-rate、open-interest（当前值）、ticker。

        OKX 没有公开的 OI 历史接口，持仓量变化靠本地累积读数计算；
        公开 K 线不含主动成交量，合约 taker 特征留空。
        """
        try:
            inst = okx_inst_id(symbol)
            if not inst:
                return at, False
            base = self.settings.okx_url
            results = await asyncio.gather(
                self.get("OKX Futures", base, "/api/v5/public/funding-rate", {"instId": inst}),
                self.get("OKX Futures", base, "/api/v5/public/open-interest", {"instId": inst}),
                self.get("OKX Futures", base, "/api/v5/market/ticker", {"instId": inst}),
                return_exceptions=True,
            )
            for value in results:
                if isinstance(value, tuple):
                    raw_ids.append(value[1])
                    at = max(at, value[2])
            if not all(isinstance(value, tuple) for value in results):
                self.futures_errors.update(str(v) for v in results if isinstance(v, ProviderError))
                return at, False
            funding, oi_resp, ticker = (value[0] for value in results)
            for payload in (funding, oi_resp, ticker):
                if (
                    not isinstance(payload, dict)
                    or payload.get("code") != "0"
                    or not payload.get("data")
                ):
                    raise ProviderError("OKX 返回数据结构不符合预期")
            rate = number(funding["data"][0].get("fundingRate"))
            if rate is not None:
                features.funding_rate_pct = rate * 100
            swap_last = number(ticker["data"][0].get("last"))
            if swap_last and spot_price > 0:
                # 跨所基差：OKX 永续最新价相对 Binance 现货价，仅作方向参考。
                features.basis_pct = (swap_last / spot_price - 1) * 100
                issues.append("BASIS_CROSS_EXCHANGE")
            oi_usd = number(oi_resp["data"][0].get("oiUsd"))
            if oi_usd is not None and oi_usd > 0:
                key = f"okx:{inst}"
                self.store.save_oi(key, at, oi_usd)
                history = self.store.oi_history(key, at - timedelta(minutes=65))
                target = at - timedelta(minutes=5)
                past = [p for p in history if p[0] <= at - timedelta(minutes=2, seconds=30)]
                if past:
                    ref_ts, ref_oi = min(past, key=lambda p: abs((p[0] - target).total_seconds()))
                    if abs((ref_ts - target).total_seconds()) <= 150 and ref_oi > 0:
                        features.oi_change_5m_pct = (oi_usd / ref_oi - 1) * 100
                if len(history) >= 20:
                    values = [p[1] for p in history]
                    deviation = statistics.pstdev(values[:-1])
                    if deviation > 0:
                        features.oi_zscore = (values[-1] - statistics.mean(values[:-1])) / deviation
        except (ProviderError, KeyError, TypeError, ValueError, IndexError):
            pass
        ok = features.funding_rate_pct is not None and features.oi_change_5m_pct is not None
        return at, ok

    async def memes(self) -> list[Snapshot]:
        try:
            profiles, profile_raw, _ = await self.get(
                "DEX Screener", self.settings.dexscreener_url, "/token-profiles/latest/v1"
            )
            if not isinstance(profiles, list):
                raise ProviderError("候选发现接口返回的数据结构不符合预期")
            universe = [(token.chain, token.address) for token in self.settings.meme_watchlist]
            universe += [
                (row["chainId"], row["tokenAddress"])
                for row in profiles
                if row.get("chainId") in self.settings.meme_chains
                and isinstance(row.get("tokenAddress"), str)
            ]
            universe = list(dict.fromkeys(universe))[: self.settings.max_memes]
            results = await asyncio.gather(
                *(self._meme_asset(chain, address, profile_raw) for chain, address in universe),
                return_exceptions=True,
            )
            snapshots = [row for row in results if isinstance(row, Snapshot)]
            errors = [row for row in results if isinstance(row, Exception)]
            self.status(
                "DEX Screener",
                ProviderState.DEGRADED if errors else ProviderState.READY,
                f"候选 {len(universe)} 个，可用池子 {len(snapshots)} 个",
                "最新 token profiles + 自选地址；非完整新币扫描，存在宣传选择偏差",
            )
            return snapshots
        except (ProviderError, ValueError, KeyError, TypeError) as exc:
            message = str(exc) if isinstance(exc, ProviderError) else "DEX 响应结构验证失败"
            self.status(
                "DEX Screener", ProviderState.UNAVAILABLE, message, "DEX 代币发现与交易池快照"
            )
            return []

    async def _meme_asset(self, chain: str, address: str, profile_raw: str) -> Snapshot | None:
        if not address.isalnum() or not chain.isalnum():
            raise ProviderError("代币地址或链标识格式无效")
        pairs, pair_raw, at = await self.get(
            "DEX Screener", self.settings.dexscreener_url, f"/token-pairs/v1/{chain}/{address}"
        )
        quote_received_at = at
        if not isinstance(pairs, list):
            raise ProviderError("交易池数据结构不符合预期")

        def same_address(other: str) -> bool:
            return other == address if chain == "solana" else other.lower() == address.lower()

        eligible = [
            p
            for p in pairs
            if same_address(p.get("baseToken", {}).get("address", ""))
            and (number(p.get("priceUsd")) or 0) > 0
            and p.get("chainId") == chain
        ]
        if not eligible:
            return None
        pair = max(eligible, key=lambda p: number(p.get("liquidity", {}).get("usd")) or 0)
        raw_ids = [profile_raw, pair_raw]
        chain_ids = {"ethereum": "1", "base": "8453", "bsc": "56", "arbitrum": "42161"}
        risk_data = None
        if self.settings.enable_goplus and chain in chain_ids:
            try:
                response, risk_raw, risk_at = await self.get(
                    "GoPlus",
                    self.settings.goplus_url,
                    f"/api/v1/token_security/{chain_ids[chain]}",
                    {"contract_addresses": address},
                )
                raw_ids.append(risk_raw)
                at = max(at, risk_at)
                risk_data = response.get("result", {}).get(address.lower())
                if isinstance(risk_data, dict) and risk_data:
                    self.status(
                        "GoPlus",
                        ProviderState.READY,
                        "公开 EVM 合约检查可用",
                        "部分 EVM 链；返回缺项仍需人工核查，Solana 暂未接入",
                    )
                else:
                    self.status(
                        "GoPlus",
                        ProviderState.NEEDS_VERIFICATION,
                        "接口未提供该地址的有效检查结果",
                        "EVM 公开检查",
                    )
            except (ProviderError, TypeError, KeyError) as exc:
                message = str(exc) if isinstance(exc, ProviderError) else "风险检查响应无效"
                self.status("GoPlus", ProviderState.UNAVAILABLE, message, "EVM 公开检查")
        risk = token_risk(risk_data, chain)
        volumes = pair.get("volume") or {}
        volume_5m, volume_1h = number(volumes.get("m5")), number(volumes.get("h1"))
        # h1 includes m5: subtract it to avoid comparing overlapping windows.
        baseline = (
            (volume_1h - volume_5m) / 11
            if volume_1h is not None and volume_5m is not None and volume_1h > volume_5m
            else None
        )
        transactions = (pair.get("txns") or {}).get("m5") or {}
        created_at = milliseconds(pair["pairCreatedAt"]) if pair.get("pairCreatedAt") else None
        features = Features(
            return_5m_pct=number((pair.get("priceChange") or {}).get("m5")),
            return_1h_pct=number((pair.get("priceChange") or {}).get("h1")),
            relative_volume=volume_5m / baseline if baseline and volume_5m is not None else None,
            liquidity_usd=number((pair.get("liquidity") or {}).get("usd")),
            volume_1h_usd=volume_1h,
            buys_5m=transactions.get("buys"),
            sells_5m=transactions.get("sells"),
            pool_age_hours=max(0, (at - created_at).total_seconds() / 3600)
            if created_at and created_at <= at
            else None,
        )
        canonical_address = address if chain == "solana" else address.lower()
        return Snapshot(
            asset_id=f"dex:{chain}:{canonical_address}:{pair['pairAddress']}",
            symbol=pair["baseToken"].get("symbol") or address[:8],
            module=Module.MEME,
            source="DEX Screener",
            chain=chain,
            address=canonical_address,
            pair_address=pair["pairAddress"],
            market_time=quote_received_at,
            available_at=at,
            price=float(pair["priceUsd"]),
            quote_volume_24h=number(volumes.get("h24")),
            features=features,
            risk=risk,
            raw_ids=raw_ids,
            quality_issues=["QUOTE_TIME_UNVERIFIED", "PROFILE_DISCOVERY_BIAS"],
        )
