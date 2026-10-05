import asyncio
import json
from datetime import datetime

from ..async_io import run_sync
from ..features import candle_features, cross_venue_features, depth_features
from ..models import Candle, Features, Module, Snapshot, utc_now
from ..universe import CORE, build_universe
from .common import ProviderError, milliseconds


class BinanceSpotProvider:
    def __init__(self, settings, store, request, derivatives):
        self.settings, self.store, self.request, self.derivatives = (
            settings,
            store,
            request,
            derivatives,
        )
        self.selection = store.universe()
        self.coverage = {}
        self.round = store.state("detail_round") or 0

    async def get(self, path, params=None):
        return await self.request("Binance Spot", self.settings.binance_spot_url, path, params)

    async def collect(self):
        await self.derivatives.bulk()
        now = utc_now()
        refresh = (
            not self.selection
            or (now - datetime.fromisoformat(self.selection["selected_at"])).total_seconds()
            >= self.settings.universe_refresh_seconds
        )
        if refresh:
            values = await asyncio.gather(
                self.get("/api/v3/exchangeInfo"),
                self.get("/api/v3/ticker/24hr"),
                self.derivatives.metadata(),
                return_exceptions=True,
            )
            if any(isinstance(v, Exception) for v in values[:2]):
                raise ProviderError("现货元数据或报价不可用")
            (info, info_raw, _), (tickers, ticker_raw, at) = values[:2]
            swaps, swap_raw = values[2][:2] if isinstance(values[2], tuple) else ([], None)
            selection_at = max(v[2] for v in values if isinstance(v, tuple))
            if not isinstance(tickers, list) or not isinstance(info, dict):
                raise ProviderError("交易所元数据或报价结构无效")
            self.selection = build_universe(
                info,
                tickers,
                swaps,
                list(self.derivatives.tickers.values()),
                self.selection,
                self.settings,
                selection_at,
                [r for r in (info_raw, ticker_raw, swap_raw) if r],
            )
            await run_sync(self.store.save_universe, self.selection)
        else:
            symbols = [r["binance_symbol"] for r in self.selection["entries"]]
            tickers, ticker_raw, at = await self.get(
                "/api/v3/ticker/24hr",
                {"symbols": json.dumps(symbols, separators=(",", ":"))},
            )
        if not isinstance(tickers, list):
            raise ProviderError("行情接口返回非列表")
        by_symbol = {r["symbol"]: r for r in tickers if isinstance(r, dict) and "symbol" in r}
        selected = self.selection["entries"]
        previous = {s.symbol: s for s in await run_sync(self.store.latest_snapshots, now)}
        details = set(CORE)
        # Allocate slots by tier without starving lower tiers; checkpoints survive restart.
        remaining = self.settings.detail_batch_size
        if remaining >= len(selected) - len(CORE):
            details.update(r["binance_symbol"] for r in selected)
        else:
            # Optional reduced budget: oldest evidence first; tier only breaks ties.

            def age_key(entry):
                old = previous.get(entry["binance_symbol"])
                return (
                    old.detail_updated_at.timestamp() if old and old.detail_updated_at else 0,
                    entry["tier"],
                    entry["binance_symbol"],
                )

            candidates = [r for r in selected if r["binance_symbol"] not in CORE]
            details.update(r["binance_symbol"] for r in sorted(candidates, key=age_key)[:remaining])
        self.round += 1
        await run_sync(self.store.set_state, "detail_round", self.round)
        common = [ticker_raw] + self.selection["raw_ids"] + self.derivatives.bulk_raw_ids
        results = await asyncio.gather(
            *(
                self._asset(
                    entry,
                    by_symbol[entry["binance_symbol"]],
                    at,
                    common,
                    entry["binance_symbol"] in details,
                    previous.get(entry["binance_symbol"]),
                )
                for entry in selected
                if entry["binance_symbol"] in by_symbol
            ),
            return_exceptions=True,
        )
        snapshots = [s for s in results if isinstance(s, Snapshot)]
        actual = {s.symbol for s in snapshots}
        self.coverage = {
            "target": self.settings.max_altcoins + 2,
            "target_altcoins": self.settings.max_altcoins,
            "selected": len(selected),
            "quotes": len(snapshots),
            "core": [s for s in CORE if s in actual],
            "details": sum(
                s.detail_updated_at is not None
                and (at - s.detail_updated_at).total_seconds() <= self.settings.stale_seconds
                for s in snapshots
            ),
            "scheduled_details": len(details),
            "updated_at": at.isoformat(),
            "symbols": [r["binance_symbol"] for r in selected],
            "missing_quotes": [
                r["binance_symbol"] for r in selected if r["binance_symbol"] not in actual
            ],
            "missing_details": [s.symbol for s in snapshots if not s.detail_updated_at],
            "missing_pins": self.selection["missing_pins"],
            "universe_updated_at": self.selection["selected_at"],
        }
        return snapshots

    async def _asset(self, entry, ticker, quote_received, common, scheduled, old):
        symbol = ticker["symbol"]
        now = utc_now()
        times = {"quote": milliseconds(ticker["closeTime"])}
        received = {"quote": quote_received}
        raw_ids = list(dict.fromkeys(common))
        features = Features()
        candles = []
        issues = []
        updated = None
        if (
            old
            and old.detail_updated_at
            and 0 <= (now - old.detail_updated_at).total_seconds() <= self.settings.stale_seconds
        ):
            features = old.features.model_copy(deep=True)
            candles = old.candles
            updated = old.detail_updated_at
            times.update(old.component_times)
            times["quote"] = milliseconds(ticker["closeTime"])
            received.update(old.component_received_at)
            received["quote"] = quote_received
            raw_ids.extend(old.raw_ids)
            issues = list(old.quality_issues)
        if scheduled:
            # A full refresh replaces old detail evidence; cached derivative queries
            # append their actual raw IDs again below. Do not carry a growing lineage.
            raw_ids = list(dict.fromkeys(common))
            # A failed new check cannot retain old endpoint values as current evidence.
            features, candles, issues = Features(), [], []
            times, received = (
                {"quote": milliseconds(ticker["closeTime"])},
                {"quote": quote_received},
            )
            values = await asyncio.gather(
                self.get("/api/v3/klines", {"symbol": symbol, "interval": "1m", "limit": 180}),
                self.get(
                    "/api/v3/depth", {"symbol": symbol, "limit": self.settings.spot_depth_limit}
                ),
                self.get("/api/v3/aggTrades", {"symbol": symbol, "limit": 100}),
                return_exceptions=True,
            )
            for name, result in zip(("candles", "book", "spot_trades"), values, strict=True):
                if not isinstance(result, tuple):
                    issues.append(name.upper() + "_UNAVAILABLE")
                    continue
                payload, raw, collected = result
                raw_ids.append(raw)
                now = max(now, collected)
                received[name] = collected
                try:
                    if name == "candles":
                        candles = [
                            Candle(
                                open_time=milliseconds(r[0]),
                                close_time=milliseconds(r[6]),
                                open=float(r[1]),
                                high=float(r[2]),
                                low=float(r[3]),
                                close=float(r[4]),
                                volume=float(r[5]),
                                quote_volume=float(r[7]),
                                taker_buy_quote_volume=float(r[10]),
                            )
                            for r in payload
                            if milliseconds(r[6]) <= collected
                        ]
                        features, candle_issues = candle_features(candles, collected)
                        issues.extend(candle_issues)
                        if candles:
                            times[name] = candles[-1].close_time
                    elif name == "book":
                        depth_features(payload, features)
                        # Binance depth snapshot has no exchange time; explicitly use receive time.
                        times[name] = collected
                    else:
                        trades = {r["a"]: r for r in payload if milliseconds(r["T"]) <= collected}
                        if trades:
                            times[name] = max(milliseconds(r["T"]) for r in trades.values())
                            features.spot_sample_cvd_usdt = sum(
                                float(r["p"]) * float(r["q"]) * (-1 if r["m"] else 1)
                                for r in trades.values()
                            )
                except (ValueError, TypeError, KeyError, IndexError):
                    issues.append(name.upper() + "_INVALID")
            updated = now if candles else None
        if not candles:
            issues.append("INSUFFICIENT_CANDLE_HISTORY")
        self.derivatives.current(entry, features, times, received, issues, now)
        if scheduled:
            now = await self.derivatives.details(
                entry, float(ticker["lastPrice"]), features, times, received, issues, raw_ids, now
            )
        components = {
            "funding": ("funding_rate_pct",),
            "oi_history": (
                "oi_change_5m_pct",
                "oi_zscore",
                "oi_velocity_pct_per_minute",
                "oi_acceleration_pct",
                "oi_percentile",
            ),
            "taker": ("perp_taker_buy_ratio", "perp_taker_delta_usd"),
            "long_short": ("long_short_account_ratio",),
            "elite_accounts": ("elite_account_ratio",),
            "elite_positions": ("elite_position_ratio",),
            "index": ("index_price",),
        }
        for name, fields in components.items():
            limit = (
                self.settings.stale_seconds
                if name in ("funding", "index")
                else self.settings.statistics_stale_seconds
            )
            source = times.get(name)
            if source is None or not 0 <= (now - source).total_seconds() <= limit:
                for field in fields:
                    setattr(features, field, None)
        if features.funding_rate_pct is None or features.oi_change_5m_pct is None:
            issues.append("DERIVATIVES_UNAVAILABLE")
        cross_venue_features(features, float(ticker["lastPrice"]), times, now, self.settings)
        features.relative_strength_15m_pct = features.relative_strength_eth_15m_pct = None
        fields = (
            "ema20",
            "spread_bps",
            "oi_change_5m_pct",
            "funding_rate_pct",
            "perp_taker_buy_ratio",
            "long_short_account_ratio",
        )
        missing = [name for name in fields if getattr(features, name) is None]
        return Snapshot(
            asset_id="binance:" + symbol,
            canonical_asset_id=entry["canonical_asset_id"],
            symbol=symbol,
            module=Module.BTC
            if symbol == CORE[0]
            else Module.ETH
            if symbol == CORE[1]
            else Module.ALT,
            source="Binance",
            market_time=times["quote"],
            available_at=now,
            price=float(ticker["lastPrice"]),
            quote_volume_24h=float(ticker["quoteVolume"]),
            features=features,
            candles=candles,
            raw_ids=list(dict.fromkeys(raw_ids)),
            quality_issues=list(dict.fromkeys(issues)),
            detail_updated_at=updated,
            component_times=times,
            component_received_at=received,
            missing_metrics=missing,
            tier=entry["tier"],
            feature_version="features-v4-1",
        )
