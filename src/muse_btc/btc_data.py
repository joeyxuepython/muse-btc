"""Manual free-data collection and source-separated BTC research views."""

from datetime import datetime, timedelta

from .intelligence import EvidenceRecord, IntelligenceStore
from .liquidations import collect_liquidations
from .macro import MacroEngine
from .models import ProviderState, utc_now
from .providers.common import ProviderError
from .providers.free_btc import BG_METRICS, CM_METRICS, FreeBTCProvider, daily_points


class BTCDataEngine:
    def __init__(self, store, settings, public):
        self.store, self.settings, self.public = store, settings, public
        self.archive = IntelligenceStore(store)
        self.provider = FreeBTCProvider(public) if public else None

    def cached(self, key, seconds):
        cached = self.store.state("btc_cache:" + key)
        if cached and cached["history_days"] == self.settings.btc_history_days:
            if 0 <= (utc_now() - datetime.fromisoformat(cached["at"])).total_seconds() < seconds:
                return [
                    r | {"status": "CACHED", "data_status": r["status"]} for r in cached["results"]
                ]
        return None

    def cache(self, key, results):
        self.store.set_state(
            "btc_cache:" + key,
            {
                "at": utc_now().isoformat(),
                "history_days": self.settings.btc_history_days,
                "results": results,
            },
        )

    def archive_points(self, source, metric, unit, points, raw, at, delay_days=0):
        for day, value in points:
            self.archive.save(
                EvidenceRecord(
                    kind="onchain",
                    key=f"{source}:BTC:{metric}:{day.date().isoformat()}",
                    source=source,
                    market_time=day,
                    available_at=at,
                    raw_ids=[raw],
                    data={
                        "asset": "BTC",
                        "metric": metric,
                        "value": value,
                        "unit": unit,
                        "frequency": "1d",
                        "free_recency_limit_days": delay_days,
                        "source_url": self.settings.coinmetrics_url
                        + "/timeseries/asset-metrics?assets=btc&frequency=1d&metrics="
                        + next(field for field, (name, _) in CM_METRICS.items() if name == metric)
                        if source == "Coin Metrics"
                        else self.settings.bgeometrics_url + "/" + metric,
                        "revision_policy": "VISIBLE_FROM_ACTUAL_FETCH_NOT_OBSERVATION_DATE",
                    },
                )
            )

    async def onchain(self):
        results = self.cached("Coin Metrics", self.settings.btc_onchain_refresh_seconds)
        if results is None:
            results = []
            try:
                rows, raw, at = await self.provider.coinmetrics()
                for field, (metric, unit) in CM_METRICS.items():
                    try:
                        points, rejected = daily_points(rows, (field,), at, coinmetrics=True)
                        self.archive_points("Coin Metrics", metric, unit, points, raw, at)
                        results.append(
                            {
                                "source": "Coin Metrics",
                                "metric": metric,
                                "status": "DEGRADED" if rejected else "ARCHIVED",
                                "points": len(points),
                                "rejected_rows": rejected,
                                "latest_observation": points[-1][0].isoformat(),
                                "checked_at": at.isoformat(),
                            }
                        )
                    except ProviderError as exc:
                        results.append(self.failure("Coin Metrics", metric, exc))
                if all(r["status"] != "UNAVAILABLE" for r in results):
                    self.cache("Coin Metrics", results)
            except ProviderError as exc:
                results = [
                    self.failure("Coin Metrics", metric, exc) for metric, _ in CM_METRICS.values()
                ]
        for metric, (fields, unit) in BG_METRICS.items():
            cached = self.cached("BGeometrics:" + metric, self.settings.btc_onchain_refresh_seconds)
            if cached:
                results.extend(cached)
                continue
            try:
                rows, raw, at, complete = await self.provider.bgeometrics(metric)
                points, rejected = daily_points(rows, fields, at)
                self.archive_points("BGeometrics", metric, unit, points, raw, at, 7)
                result = {
                    "source": "BGeometrics",
                    "metric": metric,
                    "status": "DEGRADED" if rejected or not complete else "DELAYED",
                    "points": len(points),
                    "rejected_rows": rejected,
                    "history_page_complete": complete,
                    "free_recency_limit_days": 7,
                    "latest_observation": points[-1][0].isoformat(),
                    "checked_at": at.isoformat(),
                }
                self.cache("BGeometrics:" + metric, [result])
                results.append(result)
            except ProviderError as exc:
                results.append(self.failure("BGeometrics", metric, exc))
        return results

    @staticmethod
    def failure(source, metric, exc):
        return {
            "source": source,
            "metric": metric,
            "status": "UNAVAILABLE",
            "checked_at": utc_now().isoformat(),
            "reason": str(exc),
        }

    async def options(self):
        cached = self.cached("Deribit", self.settings.btc_options_refresh_seconds)
        if cached:
            return cached
        try:
            data, raw_ids, at = await self.provider.options()
            self.archive.save(
                EvidenceRecord(
                    kind="options",
                    key="Deribit:BTC:" + at.isoformat(),
                    source="Deribit",
                    market_time=datetime.fromisoformat(data["summary_received_at"]),
                    available_at=at,
                    raw_ids=raw_ids,
                    data=data,
                )
            )
            result = {
                "source": "Deribit",
                "metric": "options",
                "status": "DEGRADED"
                if data["greeks_errors"] or data["rejected_rows"]
                else "ARCHIVED",
                "instruments": len(data["chain"]),
                "greeks_observed": data["greeks_observed"],
                "full_greeks_coverage": data["full_greeks_coverage"],
                "errors": data["greeks_errors"],
                "checked_at": at.isoformat(),
            }
            self.cache("Deribit", [result])
            return [result]
        except ProviderError as exc:
            return [self.failure("Deribit", "options", exc)]

    async def collect(self, scope="all", liquidation_seconds=10):
        if scope not in {"all", "onchain", "options", "macro", "liquidations"}:
            raise ValueError("未知免费数据采集范围")
        if not self.public:
            return {"status": "UNAVAILABLE", "reason": "公开数据 provider 未配置"}
        lease_seconds = max(
            600,
            liquidation_seconds + 60,
            self.settings.request_timeout_seconds * (self.settings.deribit_greeks_limit + 12) * 4
            + 60,
        )
        token = self.archive.acquire("btc-free-data", utc_now(), lease_seconds)
        if not token:
            return {"status": "BUSY"}
        results = []
        try:
            if scope in {"all", "onchain"}:
                results.extend(await self.onchain())
            if scope in {"all", "options"}:
                results.extend(await self.options())
            if scope in {"all", "macro"}:
                macro = await MacroEngine(self.store, self.settings, self.public).collect()
                results.append(
                    {
                        "source": "FRED/DeFiLlama/Farside",
                        "metric": "macro",
                        "status": macro["status"],
                        "sources": macro.get("sources", []),
                        "checked_at": utc_now().isoformat(),
                    }
                )
            if scope in {"all", "liquidations"}:
                results.append(
                    await collect_liquidations(self.store, self.settings, liquidation_seconds)
                )
            checks = self.store.state("btc_free_checks") or {}
            for r in results:
                checks[r["source"] + ":" + r.get("metric", "liquidations")] = r
            self.store.set_state("btc_free_checks", checks)
            degraded = any(
                r.get("data_status", r["status"]) in {"UNAVAILABLE", "DEGRADED", "BUSY"}
                for r in results
            )
            if self.public:
                self.public.providers.status(
                    "BTC Free Data",
                    ProviderState.DEGRADED if degraded else ProviderState.READY,
                    "免费来源已检查；链上延迟和清算采样见分项",
                    "BTC 免费数据；手动采集",
                )
            return {
                "status": "DEGRADED" if degraded else "COMPLETE",
                "sources": results,
                "research_checks_performed": False,
                "scheduled": False,
            }
        finally:
            self.archive.release("btc-free-data", token)

    def series(self, now, source=None, metric=None, limit=1461):
        records = self.archive.records("onchain", now, limit=100000)
        return sorted(
            (
                r
                for r in records
                if (source is None or r.source == source)
                and (metric is None or r.data["metric"] == metric)
            ),
            key=lambda r: r.market_time,
        )[-limit:]

    def summary(self, now):
        records = self.archive.records("onchain", now, limit=100000)
        grouped = {}
        for row in records:
            grouped.setdefault((row.source, row.data["metric"]), []).append(row)
        expected = [("Coin Metrics", m, u, 0) for m, u in CM_METRICS.values()] + [
            ("BGeometrics", m, unit, 7) for m, (_, unit) in BG_METRICS.items()
        ]
        series = []
        for source, metric, unit, delay in expected:
            history = sorted(grouped.get((source, metric), []), key=lambda r: r.market_time)
            last = history[-1] if history else None
            status = (
                "NOT_COLLECTED"
                if last is None
                else "STALE"
                if now - last.market_time > timedelta(days=delay + 3)
                else "DELAYED"
                if delay
                else "AVAILABLE"
            )
            series.append(
                {
                    "source": source,
                    "metric": metric,
                    "unit": unit,
                    "value": last.data["value"] if last else None,
                    "status": status,
                    "observation_date": last.market_time.isoformat() if last else None,
                    "available_at": last.available_at.isoformat() if last else None,
                    "source_url": last.data["source_url"] if last else None,
                    "free_recency_limit_days": delay,
                    "history": [
                        [r.market_time.isoformat(), r.data["value"]] for r in history[-30:]
                    ],
                    "observations": len(history),
                }
            )
        options = self.archive.records("options", now, limit=1)
        option_view = options[0].model_dump(mode="json") if options else None
        if option_view:
            age = (now - options[0].market_time).total_seconds()
            option_view["status"] = (
                "STALE" if age > self.settings.btc_options_refresh_seconds * 2 else "AVAILABLE"
            )
            option_view["age_seconds"] = age
        return {
            "as_of": now.isoformat(),
            "onchain": series,
            "options": option_view,
            "checks": list((self.store.state("btc_free_checks") or {}).values()),
            "liquidations": {
                "events": [
                    r.model_dump(mode="json")
                    for r in self.archive.records("liquidation", now, limit=100)
                ],
                "windows": [
                    r.model_dump(mode="json")
                    for r in self.archive.records("liquidation_window", now, limit=10)
                ],
                "full_market_total_usd": None,
                "full_market": False,
            },
            "limitations": [
                "各供应商口径独立，未替代 Glassnode 指标",
                "历史修订仅从实际获取日起可见",
                "链上成本指标免费版本延迟七天",
                "期权与清算需持续归档才能积累历史",
                "宏观与 ETF 联合压力已参与风险限制；链上与期权提供分项背景，完整市场状态模型待验证",
            ],
        }
