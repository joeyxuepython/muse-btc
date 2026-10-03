import asyncio
import statistics

from ..features import depth_features
from ..models import Features
from .common import ProviderError, milliseconds, number

STAT = "/api/v5/rubik/stat/"


class OKXDerivativesProvider:
    """Verified OKX public contracts, units and timestamps; no Binance futures fallback."""

    def __init__(self, settings, store, request):
        self.settings, self.store, self.request = settings, store, request
        self.instruments = []
        self.tickers = {}
        self.open_interest = {}
        self.marks = {}
        self.bulk_raw_ids = []
        self.bulk_received_at = None
        self.bulk_received_times = {}

    async def get(self, path, params):
        payload, raw_id, at = await self.request("OKX Futures", self.settings.okx_url, path, params)
        if (
            not isinstance(payload, dict)
            or payload.get("code") != "0"
            or not isinstance(payload.get("data"), list)
        ):
            raise ProviderError("OKX 返回错误代码或无效数据结构")
        return payload["data"], raw_id, at

    async def metadata(self):
        rows, raw, at = await self.get("/api/v5/public/instruments", {"instType": "SWAP"})
        self.instruments = rows
        return rows, raw, at

    async def bulk(self):
        self.bulk_raw_ids = []
        self.bulk_received_times = {}
        results = await asyncio.gather(
            self.get("/api/v5/market/tickers", {"instType": "SWAP"}),
            self.get("/api/v5/public/open-interest", {"instType": "SWAP"}),
            self.get("/api/v5/public/mark-price", {"instType": "SWAP"}),
            return_exceptions=True,
        )
        for name, result in zip(("tickers", "open_interest", "marks"), results, strict=True):
            if isinstance(result, tuple):
                rows, raw, at = result
                setattr(self, name, {r["instId"]: r for r in rows if "instId" in r})
                self.bulk_raw_ids.append(raw)
                self.bulk_received_at = at
                self.bulk_received_times[name] = at
            else:
                setattr(self, name, {})
        return self.bulk_raw_ids

    def current(self, entry, features, times, received, issues, at):
        features.oi_contracts = features.oi_usd = features.mark_price = None
        features.basis_pct = features.cross_venue_premium_pct = None
        for name in ("oi", "mark"):
            times.pop(name, None)
            received.pop(name, None)
        inst = entry.get("okx_inst_id")
        if not inst:
            issues.append("OKX_SWAP_UNAVAILABLE")
            return
        oi = self.open_interest.get(inst, {})
        mark = self.marks.get(inst, {})
        for name, row in (("oi", oi), ("mark", mark)):
            stamp = number(row.get("ts"))
            if stamp is None:
                issues.append(f"{name.upper()}_UNAVAILABLE")
                continue
            source = milliseconds(stamp)
            if not 0 <= (at - source).total_seconds() <= self.settings.stale_seconds:
                issues.append(f"{name.upper()}_STALE_OR_FUTURE")
                continue
            times[name] = source
            received[name] = self.bulk_received_times.get(
                "open_interest" if name == "oi" else "marks", at
            )
            if name == "oi":
                features.oi_contracts, features.oi_usd = (
                    number(row.get("oi")),
                    number(row.get("oiUsd")),
                )
            else:
                features.mark_price = number(row.get("markPx"))

    async def details(self, entry, spot_price, features, times, received, issues, raw_ids, at):
        inst = entry.get("okx_inst_id")
        if not inst:
            return at
        queries = {
            "funding": ("/api/v5/public/funding-rate", {"instId": inst}),
            "funding_history": (
                "/api/v5/public/funding-rate-history",
                {"instId": inst, "limit": 100},
            ),
            "oi_history": (
                STAT + "contracts/open-interest-history",
                {"instId": inst, "period": "5m", "limit": 100},
            ),
            "taker": (
                STAT + "taker-volume-contract",
                {"instId": inst, "period": "5m", "unit": "2", "limit": 20},
            ),
            "long_short": (
                STAT + "contracts/long-short-account-ratio-contract",
                {"instId": inst, "period": "5m", "limit": 2},
            ),
            "elite_accounts": (
                STAT + "contracts/long-short-account-ratio-contract-top-trader",
                {"instId": inst, "period": "5m", "limit": 2},
            ),
            "elite_positions": (
                STAT + "contracts/long-short-position-ratio-contract-top-trader",
                {"instId": inst, "period": "5m", "limit": 2},
            ),
            "index": ("/api/v5/market/index-tickers", {"instId": inst.removesuffix("-SWAP")}),
            "perp_book": ("/api/v5/market/books", {"instId": inst, "sz": 100}),
            "perp_trades": ("/api/v5/market/trades", {"instId": inst, "limit": 100}),
            "perp_candles": ("/api/v5/market/candles", {"instId": inst, "bar": "1m", "limit": 100}),
        }
        results = await asyncio.gather(
            *(self.get(p, q) for p, q in queries.values()), return_exceptions=True
        )
        for name, result in zip(queries, results, strict=True):
            if not isinstance(result, tuple):
                issues.append(name.upper() + "_UNAVAILABLE")
                continue
            rows, raw, received_at = result
            raw_ids.append(raw)
            at = max(at, received_at)
            if not rows:
                issues.append(name.upper() + "_UNAVAILABLE")
                continue
            try:
                candidate, candidate_times = features.model_copy(deep=True), dict(times)
                self._apply(
                    name, rows, entry, spot_price, candidate, candidate_times, issues, received_at
                )
                for field in type(features).model_fields:
                    setattr(features, field, getattr(candidate, field))
                times.update(candidate_times)
                if name in times:
                    received[name] = received_at
            except (ValueError, TypeError, KeyError, IndexError):
                issues.append(name.upper() + "_INVALID")
        return at

    def _apply(self, name, rows, entry, spot_price, f, times, issues, at):
        if name == "funding_history":
            rates = [
                number(r.get("realizedRate")) for r in rows if milliseconds(r["fundingTime"]) <= at
            ]
            rates = [r * 100 for r in rates if r is not None]
            if len(rates) >= 10:
                f.funding_mean_pct = statistics.mean(rates)
                deviation = statistics.pstdev(rates)
                if f.funding_rate_pct is not None:
                    f.funding_zscore = (
                        (f.funding_rate_pct - f.funding_mean_pct) / deviation if deviation else None
                    )
                    f.funding_percentile = (
                        sum(r <= f.funding_rate_pct for r in rates) / len(rates) * 100
                    )
                f.funding_trend_pct = statistics.mean(rates[:5]) - statistics.mean(rates[-5:])
            times[name] = max(
                milliseconds(r["fundingTime"]) for r in rows if milliseconds(r["fundingTime"]) <= at
            )
            return
        if name == "perp_candles":
            closed = [
                r for r in rows if len(r) >= 9 and str(r[8]) == "1" and milliseconds(r[0]) <= at
            ]
            if closed:
                times[name] = milliseconds(max(int(r[0]) for r in closed))
            return
        if name == "funding":
            # ts is current data generation time, fundingTime is future settlement.
            ts = milliseconds(rows[0]["ts"])
        elif name in ("index", "perp_book"):
            ts = milliseconds(rows[0]["ts"])
        elif name == "perp_trades":
            ts = max(milliseconds(r["ts"]) for r in rows)
        else:
            ts = max(milliseconds(r[0]) for r in rows)
        limit = (
            self.settings.statistics_stale_seconds
            if name in ("oi_history", "taker", "long_short", "elite_accounts", "elite_positions")
            else self.settings.stale_seconds
        )
        if not 0 <= (at - ts).total_seconds() <= limit:
            issues.append(name.upper() + "_STALE_OR_FUTURE")
            return
        times[name] = ts
        if name == "funding":
            f.funding_rate_pct = number(rows[0].get("fundingRate"))
            if f.funding_rate_pct is not None:
                f.funding_rate_pct *= 100
        elif name == "oi_history":
            points = sorted(
                (milliseconds(r[0]), float(r[1])) for r in rows if milliseconds(r[0]) <= at
            )
            if (
                len(points) >= 2
                and (points[-1][0] - points[-2][0]).total_seconds() == 300
                and points[-2][1] > 0
            ):
                f.oi_change_5m_pct = (points[-1][1] / points[-2][1] - 1) * 100
                f.oi_velocity_pct_per_minute = f.oi_change_5m_pct / 5
                if (
                    len(points) >= 3
                    and (points[-2][0] - points[-3][0]).total_seconds() == 300
                    and points[-3][1] > 0
                ):
                    f.oi_acceleration_pct = (
                        f.oi_change_5m_pct - (points[-2][1] / points[-3][1] - 1) * 100
                    )
            values = [p[1] for p in points]
            if len(values) >= 20:
                prior = values[:-1]
                dev = statistics.pstdev(prior)
                f.oi_zscore = (values[-1] - statistics.mean(prior)) / dev if dev else None
                f.oi_percentile = sum(v <= values[-1] for v in prior) / len(prior) * 100
        elif name == "taker":
            point = max(rows, key=lambda r: int(r[0]))
            sell, buy = float(point[1]), float(point[2])
            if sell < 0 or buy < 0:
                raise ValueError("negative flow")
            f.perp_taker_buy_ratio = buy / (buy + sell) if buy + sell else None
            f.perp_taker_delta_usd = buy - sell
        elif name in ("long_short", "elite_accounts", "elite_positions"):
            field = {
                "long_short": "long_short_account_ratio",
                "elite_accounts": "elite_account_ratio",
                "elite_positions": "elite_position_ratio",
            }[name]
            setattr(f, field, number(max(rows, key=lambda r: int(r[0]))[1]))
        elif name == "index":
            f.index_price = number(rows[0].get("idxPx"))
            mark_at = times.get("mark")
            if (
                f.mark_price
                and f.index_price
                and mark_at
                and abs((mark_at - ts).total_seconds()) <= self.settings.time_alignment_seconds
            ):
                f.basis_pct = (f.mark_price / f.index_price - 1) * 100
        elif name == "perp_book":
            temporary = Features()
            depth_features(rows[0], temporary)
            f.perp_spread_bps = temporary.spread_bps
            metadata = entry.get("contract_metadata") or {}
            multiplier = number(metadata.get("ctVal"))
            mult = number(metadata.get("ctMult")) or 1
            base = entry["canonical_asset_id"].removeprefix("asset:")
            if multiplier and metadata.get("ctValCcy") == base:
                for side in ("bid", "ask"):
                    depth = getattr(temporary, side + "_depth_1pct_usd")
                    if depth is not None:
                        setattr(f, "perp_" + side + "_depth_1pct_usdt", depth * multiplier * mult)
            else:
                issues.append("CONTRACT_MULTIPLIER_UNVERIFIED")
        elif name == "perp_trades":
            metadata = entry.get("contract_metadata") or {}
            multiplier = number(metadata.get("ctVal"))
            mult = number(metadata.get("ctMult")) or 1
            base = entry["canonical_asset_id"].removeprefix("asset:")
            if multiplier and metadata.get("ctValCcy") == base:
                by_id = {r["tradeId"]: r for r in rows if milliseconds(r["ts"]) <= at}
                f.perp_sample_cvd_usdt = sum(
                    float(r["sz"])
                    * multiplier
                    * mult
                    * float(r["px"])
                    * (1 if r["side"] == "buy" else -1)
                    for r in by_id.values()
                )
            else:
                issues.append("CONTRACT_MULTIPLIER_UNVERIFIED")
        mark_at = times.get("mark")
        spot_at = times.get("quote")
        if (
            f.mark_price
            and spot_price > 0
            and mark_at
            and spot_at
            and abs((mark_at - spot_at).total_seconds()) <= self.settings.time_alignment_seconds
        ):
            f.cross_venue_premium_pct = (f.mark_price / spot_price - 1) * 100
