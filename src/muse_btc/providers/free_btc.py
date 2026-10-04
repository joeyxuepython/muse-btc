"""Free read-only BTC sources. Keep vendor definitions, missing values and latency explicit."""

import json
from datetime import UTC, datetime, timedelta

from ..models import utc_now
from ..storage import stamp
from .common import ProviderError, milliseconds, number

CM_METRICS = {
    "CapMVRVCur": ("mvrv", "ratio"),
    "CapMrktCurUSD": ("market_cap", "USD"),
    "SplyCur": ("supply", "BTC"),
    "AdrActCnt": ("active_addresses", "addresses"),
    "TxCnt": ("tx_count", "tx"),
    "FlowInExNtv": ("exchange_inflow", "BTC"),
    "FlowOutExNtv": ("exchange_outflow", "BTC"),
    "SplyExNtv": ("exchange_supply", "BTC"),
}
BG_METRICS = {
    "sopr": (("sopr",), "ratio"),
    "sth-sopr": (("sthSopr",), "ratio"),
    "lth-sopr": (("lthSopr",), "ratio"),
    "realized-price": (("realizedPrice",), "USD"),
    "sth-realized-price": (("sthRealizedPrice", "realizedPriceSth"), "USD"),
    "lth-realized-price": (("lthRealizedPrice", "realizedPriceLth"), "USD"),
}


def json_object(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ProviderError("数据源未返回有效 JSON") from exc


def daily_points(rows, fields, at, *, coinmetrics=False):
    """Skip invalid/missing rows; reject conflicting dates and identity/time mismatches."""
    points, rejected = {}, 0
    for row in rows:
        if not isinstance(row, dict):
            rejected += 1
            continue
        if coinmetrics and row.get("asset") != "btc":
            raise ProviderError("Coin Metrics 返回非 BTC 数据")
        try:
            day = datetime.fromisoformat(
                str(row["time" if coinmetrics else "d"]).replace("Z", "+00:00")
            )
            if coinmetrics and day.tzinfo is None:
                raise ValueError
            day = day.replace(tzinfo=UTC) if day.tzinfo is None else day.astimezone(UTC)
            if day > at or day.time().isoformat() != "00:00:00":
                raise ValueError
            if not coinmetrics and row.get("unixTs") is not None:
                if datetime.fromtimestamp(float(row["unixTs"]), UTC).date() != day.date():
                    raise ProviderError("链上日期与 unixTs 不一致")
            value = next((number(row[f]) for f in fields if row.get(f) is not None), None)
            if value is None or value < 0:
                raise ValueError
        except (ValueError, TypeError, KeyError, OverflowError):
            rejected += 1
            continue
        if day in points and points[day] != value:
            raise ProviderError("同一天的链上指标存在冲突值")
        points[day] = value
    if not points:
        raise ProviderError("指标没有有效、非未来的日线数据")
    return sorted(points.items()), rejected


def bgeometrics_rows(payload):
    if isinstance(payload, list):
        return payload, True
    if isinstance(payload, dict):
        rows = payload.get("content", payload.get("data"))
        if isinstance(rows, list):
            total = number(payload.get("totalElements", payload.get("total")))
            pages = number(payload.get("totalPages"))
            return rows, not ((total is not None and total > len(rows)) or (pages and pages > 1))
    raise ProviderError("BGeometrics 响应结构不符")


class FreeBTCProvider:
    def __init__(self, public):
        self.public = public
        self.settings, self.store = public.settings, public.store

    async def coinmetrics(self):
        at = utc_now()
        text, raw, received = await self.public.fetch(
            "Coin Metrics",
            self.settings.coinmetrics_url + "/timeseries/asset-metrics",
            {"community-api.coinmetrics.io"},
            {
                "assets": "btc",
                "metrics": ",".join(CM_METRICS),
                "frequency": "1d",
                "start_time": (at - timedelta(days=self.settings.btc_history_days))
                .date()
                .isoformat(),
                "end_time": at.date().isoformat(),
                "page_size": 10000,
            },
        )
        payload = json_object(text)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ProviderError("Coin Metrics 响应结构不符")
        if payload.get("next_page_url") or payload.get("next_page_token"):
            raise ProviderError("Coin Metrics 返回额外分页，未将部分历史当作完整历史")
        return payload["data"], raw, received

    def reserve_bgeometrics_request(self, now):
        # This budget is persisted and reserved atomically, including failed attempts.
        # Other applications using the same public IP are outside this local budget.
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM runtime_state WHERE key='bgeometrics_budget'"
            ).fetchone()
            attempts = [datetime.fromisoformat(v) for v in json.loads(row[0])] if row else []
            attempts = [t for t in attempts if t > now - timedelta(days=1)]
            if len(attempts) >= 15 or sum(t > now - timedelta(hours=1) for t in attempts) >= 8:
                raise ProviderError("BGeometrics 本地免费额度已用尽，等待小时或日窗口释放")
            attempts.append(now)
            db.execute(
                "INSERT OR REPLACE INTO runtime_state VALUES (?,?)",
                ("bgeometrics_budget", json.dumps([stamp(t) for t in attempts])),
            )

    async def bgeometrics(self, metric):
        at = utc_now()
        self.reserve_bgeometrics_request(at)
        text, raw, received = await self.public.fetch(
            "BGeometrics",
            self.settings.bgeometrics_url + "/" + metric,
            {"bitcoin-data.com", "api.bitcoin-data.com"},
            {
                "startday": (at - timedelta(days=self.settings.btc_history_days))
                .date()
                .isoformat(),
                # Free SOPR / cost-basis endpoints restrict the most recent seven days.
                "endday": (at - timedelta(days=7)).date().isoformat(),
                "size": self.settings.btc_history_days + 1,
                "page": 0,
            },
        )
        rows, complete = bgeometrics_rows(json_object(text))
        return rows, raw, received, complete

    async def deribit(self, method, params):
        text, raw, at = await self.public.fetch(
            "Deribit",
            self.settings.deribit_url + "/public/" + method,
            {"www.deribit.com", "deribit.com"},
            params,
        )
        payload = json_object(text)
        if not isinstance(payload, dict) or payload.get("error") or "result" not in payload:
            raise ProviderError("Deribit JSON-RPC 请求失败或缺少 result")
        return payload["result"], raw, at

    async def options(self):
        instruments, meta_raw, _ = await self.deribit(
            "get_instruments", {"currency": "BTC", "kind": "option", "expired": "false"}
        )
        summaries, raw, at = await self.deribit(
            "get_book_summary_by_currency", {"currency": "BTC", "kind": "option"}
        )
        if not isinstance(instruments, list) or not isinstance(summaries, list):
            raise ProviderError("Deribit 期权列表结构不符")
        metadata = {
            r["instrument_name"]: r
            for r in instruments
            if isinstance(r, dict) and r.get("instrument_name")
        }
        chain, rejected = [], 0
        for row in summaries:
            if not isinstance(row, dict):
                rejected += 1
                continue
            instrument = metadata.get(row.get("instrument_name"), {})
            try:
                expiry = milliseconds(instrument["expiration_timestamp"])
                strike = number(instrument["strike"])
                if (
                    instrument.get("kind") != "option"
                    or instrument.get("base_currency") != "BTC"
                    or instrument.get("option_type") not in {"call", "put"}
                    or expiry <= at
                    or strike is None
                    or strike <= 0
                ):
                    raise ValueError
            except (ValueError, TypeError, KeyError, OverflowError):
                rejected += 1
                continue
            chain.append(
                {
                    "instrument": row["instrument_name"],
                    "expiry": expiry.isoformat(),
                    "strike_usd": strike,
                    "option_type": instrument["option_type"],
                    "price_unit": instrument.get("quote_currency"),
                    "mark_price": number(row.get("mark_price")),
                    "bid_price": number(row.get("bid_price")),
                    "ask_price": number(row.get("ask_price")),
                    "mark_iv_pct": number(row.get("mark_iv")),
                    "open_interest_btc": number(row.get("open_interest")),
                    "volume_24h_btc": number(row.get("volume")),
                    "underlying_price_usd": number(row.get("underlying_price")),
                    "greeks": None,
                    "ticker_time": None,
                }
            )
        if not chain:
            raise ProviderError("Deribit 没有有效的 BTC 活跃期权")
        # Sample both calls and puts across expiries, rather than implying full Greek coverage.
        groups = {}
        for row in chain:
            if row["underlying_price_usd"] and row["underlying_price_usd"] > 0:
                groups.setdefault((row["expiry"], row["option_type"]), []).append(row)
        for rows in groups.values():
            rows.sort(key=lambda r: abs(r["strike_usd"] / r["underlying_price_usd"] - 1))
        candidates = []
        for depth in range(max((len(rows) for rows in groups.values()), default=0)):
            candidates.extend(
                rows[depth] for _, rows in sorted(groups.items()) if depth < len(rows)
            )
        selected = candidates[: self.settings.deribit_greeks_limit]
        errors, raw_ids, latest = [], [meta_raw, raw], at
        for row in selected:
            try:
                ticker, ticker_raw, received = await self.deribit(
                    "ticker", {"instrument_name": row["instrument"]}
                )
                raw_ids.append(ticker_raw)
                latest = max(latest, received)
                quote_time = milliseconds(ticker["timestamp"])
                if (
                    ticker.get("instrument_name") != row["instrument"]
                    or not 0
                    <= (received - quote_time).total_seconds()
                    <= self.settings.stale_seconds
                ):
                    raise ProviderError("期权 ticker 身份不符或报价时间过期")
                greeks = ticker.get("greeks")
                if not isinstance(greeks, dict):
                    raise ProviderError("期权 ticker 缺少 Greeks")
                values = {
                    k: number(greeks.get(k)) for k in ("delta", "gamma", "vega", "theta", "rho")
                }
                if any(v is None for v in values.values()):
                    raise ProviderError("期权 Greeks 不完整")
                row["greeks"], row["ticker_time"] = values, quote_time.isoformat()
            except (ProviderError, ValueError, KeyError, TypeError, OverflowError) as exc:
                errors.append(
                    {
                        "instrument": row["instrument"],
                        "reason": str(exc)
                        if isinstance(exc, ProviderError)
                        else "期权 ticker 结构不符",
                    }
                )
        latest = max(latest, utc_now())  # Failure details are known only after the last attempt.
        return (
            {
                "asset": "BTC",
                "chain": sorted(
                    chain, key=lambda r: (r["expiry"], r["strike_usd"], r["option_type"])
                ),
                "summary_received_at": at.isoformat(),
                "summary_time_basis": "RECEIVED_AT_NOT_EXCHANGE_QUOTE_TIME",
                "rejected_rows": rejected,
                "greeks_requested": len(selected),
                "greeks_observed": sum(r["greeks"] is not None for r in chain),
                "greeks_errors": errors,
                "full_greeks_coverage": all(r["greeks"] is not None for r in chain),
                "source_url": self.settings.deribit_url
                + "/public/get_book_summary_by_currency?currency=BTC&kind=option",
                "historical_surface_complete": False,
                "limitations": [
                    "Deribit 单一交易所",
                    "Greeks 为限额采样，其余保持缺失",
                    "当前快照不能回填为历史 IV 曲面",
                    "OI 不表示做市商净头寸",
                ],
            },
            raw_ids,
            latest,
        )
