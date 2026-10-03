"""Public macro/liquidity archives and release reactions, with explicit revision limits."""

import asyncio
import statistics
from datetime import UTC, datetime, timedelta

from .intelligence import EvidenceRecord, IntelligenceStore
from .models import ProviderState, utc_now
from .providers.common import ProviderError, number
from .providers.public_intelligence import Page
from .rules import quote_usable
from .storage import stamp

MACRO_MISSING = ["CME FedWatch", "FOMC expectations", "ISM", "MOVE", "Gold", "release consensus"]


def supply_changes(row):
    def supply(field):
        return number((row.get(field) or {}).get("peggedUSD"))

    current, week, month = (
        supply(k) for k in ("circulating", "circulatingPrevWeek", "circulatingPrevMonth")
    )
    return {
        "supply_usd": current,
        "change_7d_usd": current - week if current is not None and week is not None else None,
        "change_30d_usd": current - month if current is not None and month is not None else None,
        "change_7d_pct": (current / week - 1) * 100 if current is not None and week else None,
        "change_30d_pct": (current / month - 1) * 100 if current is not None and month else None,
        "price": number(row.get("price")),
        "missing": ["mint/burn transactions", "exchange supply"],
    }


def etf_rows(text):
    """Read total net flow column only; '-' remains missing, never zero."""
    rows = Page(text).rows
    header = next(
        (r for r in rows if r and r[0].lower() == "date" and any(v.lower() == "total" for v in r)),
        None,
    )
    if not header:
        raise ProviderError("ETF 表格缺少 Date / Total 栏，未推断资金流")
    total_index = next(i for i, v in enumerate(header) if v.lower() == "total")
    result = []
    for row in rows:
        if len(row) <= total_index:
            continue
        try:
            date = datetime.strptime(row[0], "%d %b %Y").replace(tzinfo=UTC)
        except ValueError:
            continue
        value = row[total_index].replace(",", "").replace("(", "-").replace(")", "")
        flow = number(value)
        if flow is not None:
            result.append((date, flow * 1000000))
    if not result:
        raise ProviderError("ETF 表格没有完整日期与资金流")
    return sorted(result)


class MacroEngine:
    def __init__(self, store, settings, public):
        self.store, self.settings, self.public = store, settings, public
        self.archive = IntelligenceStore(store)

    async def collect(self):
        now = utc_now()
        token = self.archive.acquire("macro", now, 600)
        if not token:
            return {"status": "BUSY"}
        results = []
        try:
            values = await asyncio.gather(
                *(self.public.fred(s) for s in self.settings.macro_series), return_exceptions=True
            )
            for series, result in zip(self.settings.macro_series, values, strict=True):
                if isinstance(result, Exception):
                    results.append({"series": series, "status": "UNAVAILABLE"})
                    continue
                points, raw, at = result
                last_time, last = points[-1]
                data = {
                    "series": series,
                    "value": last,
                    "observation_date": last_time.isoformat(),
                    "previous": points[-2][1] if len(points) > 1 else None,
                    "history": [[t.isoformat(), v] for t, v in points[-90:]],
                    "revision_policy": "LATEST_RELEASE_RECEIVED_NOW_NOT_HISTORICAL_VINTAGE",
                    "source_url": self.settings.fred_url + "/series/" + series,
                }
                self.archive.save(
                    EvidenceRecord(
                        kind="macro",
                        key=series,
                        source="FRED",
                        market_time=last_time,
                        available_at=at,
                        raw_ids=[raw],
                        data=data,
                    )
                )
                results.append({"series": series, "status": "ARCHIVED"})
            try:
                rows, raw, at = await self.public.stablecoins()
                for row in rows:
                    if row.get("symbol") not in {"USDT", "USDC", "FDUSD", "DAI"}:
                        continue
                    data = supply_changes(row)
                    if data["supply_usd"] is None:
                        continue
                    data.update(
                        {
                            "symbol": row["symbol"],
                            "source_id": str(row["id"]),
                            "source_url": "https://defillama.com/stablecoins",
                        }
                    )
                    self.archive.save(
                        EvidenceRecord(
                            kind="stablecoin",
                            key=str(row["id"]),
                            source="DeFiLlama",
                            market_time=at,
                            available_at=at,
                            raw_ids=[raw],
                            data=data,
                        )
                    )
                results.append({"series": "Stablecoins", "status": "ARCHIVED"})
            except (ProviderError, ValueError, KeyError, TypeError):
                results.append({"series": "Stablecoins", "status": "UNAVAILABLE"})
            try:
                text, raw, at = await self.public.fetch(
                    "Farside", self.settings.etf_url, {"farside.co.uk"}
                )
                flows = etf_rows(text)
                for t, v in flows[-90:]:
                    if t > at:
                        continue
                    self.archive.save(
                        EvidenceRecord(
                            kind="etf",
                            key="BTC:" + t.date().isoformat(),
                            source="Farside",
                            market_time=t,
                            available_at=at,
                            raw_ids=[raw],
                            data={
                                "asset": "BTC",
                                "net_flow_usd": v,
                                "source_url": self.settings.etf_url,
                                "holdings": None,
                                "aum": None,
                            },
                        )
                    )
                results.append({"series": "ETF", "status": "ARCHIVED"})
            except (ProviderError, ValueError, KeyError, TypeError):
                results.append({"series": "ETF", "status": "UNAVAILABLE"})
            ready = sum(r["status"] == "ARCHIVED" for r in results)
            self.public.providers.status(
                "Macro",
                ProviderState.READY
                if ready == len(results)
                else ProviderState.DEGRADED
                if ready
                else ProviderState.UNAVAILABLE,
                f"公开序列 {ready}/{len(results)}；部分事件共识需导入",
                "FRED/稳定币/ETF",
            )
            self.store.set_state("macro_last_check", now.isoformat())
        finally:
            self.archive.release("macro", token)
        return {"status": "COMPLETE" if ready == len(results) else "DEGRADED", "sources": results}

    def summary(self, now):
        macro = self.archive.records("macro", now)
        by_series = {r.key: r for r in macro}
        fresh = {
            r.key: r
            for r in macro
            if (now - self.archive.last_checked(r, now)).total_seconds()
            <= self.settings.intelligence_refresh_seconds * 2
            and (now - r.market_time).days
            <= self.settings.macro_series_max_age_days.get(r.key, self.settings.macro_stale_days)
        }
        spreads = None
        if all(k in fresh for k in ("DGS2", "DGS10")):
            if fresh["DGS2"].market_time == fresh["DGS10"].market_time:
                spreads = fresh["DGS10"].data["value"] - fresh["DGS2"].data["value"]
        liquidity = None
        if all(k in fresh for k in ("WALCL", "WTREGEN", "RRPONTSYD")):
            # WALCL/WTREGEN: millions USD. RRPONTSYD: billions USD.
            times = [fresh[k].market_time for k in ("WALCL", "WTREGEN", "RRPONTSYD")]
            if (max(times) - min(times)).days <= 7:
                liquidity = (
                    fresh["WALCL"].data["value"]
                    - fresh["WTREGEN"].data["value"]
                    - fresh["RRPONTSYD"].data["value"] * 1000
                ) * 1000000
        stables = self.archive.records("stablecoin", now)
        stable_fresh = [
            r for r in stables if (now - self.archive.last_checked(r, now)).total_seconds() <= 86400
        ]
        change = [
            r.data["change_7d_pct"] for r in stable_fresh if r.data["change_7d_pct"] is not None
        ]
        flows = sorted(self.archive.records("etf", now), key=lambda r: r.market_time)
        flow_values = [
            r.data["net_flow_usd"] for r in flows[-5:] if r.data["net_flow_usd"] is not None
        ]
        return {
            "as_of": now.isoformat(),
            "series": [
                r.model_dump(mode="json")
                | {"last_checked_at": self.archive.last_checked(r, now).isoformat()}
                for r in macro
            ],
            "missing": [s for s in self.settings.macro_series if s not in by_series]
            + MACRO_MISSING,
            "stale": [s for s in by_series if s not in fresh],
            "yield_spread_10y_2y_pct": spreads,
            "net_liquidity_proxy_usd": liquidity,
            "net_liquidity_formula": "WALCL - TGA - RRP, not total investable liquidity",
            "stablecoins": [r.model_dump(mode="json") for r in stables],
            "crypto_liquidity_index": statistics.mean(change) if len(change) == 4 else None,
            "etf": [r.model_dump(mode="json") for r in flows[-30:]],
            "etf_5_observation_net_flow_usd": sum(flow_values) if len(flow_values) == 5 else None,
            "etf_flow_acceleration_usd": flows[-1].data["net_flow_usd"]
            - flows[-2].data["net_flow_usd"]
            if len(flows) >= 2
            else None,
            "limitations": [
                "历史修订数据仅从实际抓取日起可见",
                "指数为四币供应变化均值，不是买盘",
                "ETF 公布滞后；未推断持仓或 AUM；抓取失败显示缺失",
            ],
        }

    def event_reactions(self, now):
        result = []
        for event in self.archive.records("macro_event", now):
            release = datetime.fromisoformat(event.data["release_time"])
            observations = {}
            for offset in (-3600, 0, 300, 900, 3600, 14400, 86400):
                target = release + timedelta(seconds=offset)
                if target > now:
                    observations[str(offset)] = None
                    continue
                snapshots = self.store.snapshot_range(
                    target - timedelta(seconds=self.settings.event_reaction_tolerance_seconds),
                    min(
                        now,
                        target + timedelta(seconds=self.settings.event_reaction_tolerance_seconds),
                    ),
                    "binance:BTCUSDT",
                )
                matches = [
                    s
                    for s in snapshots
                    if quote_usable(s, s.available_at, self.settings)
                    and abs((s.market_time - target).total_seconds())
                    <= self.settings.event_reaction_tolerance_seconds
                ]
                candidates = [
                    {
                        "price": s.price,
                        "snapshot_id": s.id,
                        "market_time": s.market_time.isoformat(),
                        "quote_source": "MARKET_SNAPSHOT",
                    }
                    for s in matches
                ]
                with self.store.connect() as db:
                    raw_quotes = db.execute(
                        "SELECT payload FROM intelligence_records WHERE kind='btc_event_quote' "
                        "AND market_time>=? AND market_time<=? AND available_at<=?",
                        (
                            stamp(
                                target
                                - timedelta(seconds=self.settings.event_reaction_tolerance_seconds)
                            ),
                            stamp(
                                min(
                                    now,
                                    target
                                    + timedelta(
                                        seconds=self.settings.event_reaction_tolerance_seconds
                                    ),
                                )
                            ),
                            stamp(now),
                        ),
                    ).fetchall()
                for raw_quote in raw_quotes:
                    quote = EvidenceRecord.model_validate_json(raw_quote[0])
                    if (
                        0
                        <= (quote.available_at - quote.market_time).total_seconds()
                        <= self.settings.event_reaction_tolerance_seconds
                    ):
                        candidates.append(
                            {
                                "price": quote.data["price"],
                                "snapshot_id": quote.id,
                                "market_time": quote.market_time.isoformat(),
                                "quote_source": "EVENT_QUOTE",
                            }
                        )
                nearest = (
                    min(
                        candidates,
                        key=lambda s: abs(
                            (datetime.fromisoformat(s["market_time"]) - target).total_seconds()
                        ),
                    )
                    if candidates
                    else None
                )
                observations[str(offset)] = (
                    nearest
                    | {
                        "target_time": target.isoformat(),
                        "offset_seconds": (
                            datetime.fromisoformat(nearest["market_time"]) - target
                        ).total_seconds(),
                    }
                    if nearest
                    else None
                )
            baseline = observations.get("0")
            for obs in observations.values():
                if obs:
                    obs["return_from_release_pct"] = (
                        (obs["price"] / baseline["price"] - 1) * 100 if baseline else None
                    )
            result.append({"event": event.model_dump(mode="json"), "btc_reactions": observations})
        return result
