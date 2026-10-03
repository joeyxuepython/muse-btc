"""Deduplicated trade archive. Sampled REST trades never become a full-market CVD claim."""

import math
from datetime import datetime, timedelta

from .providers.common import milliseconds, number
from .storage import stamp


def archive_snapshot_trades(store, snapshot, registry):
    entry = next(
        (r for r in (registry or {}).get("entries", []) if r["binance_symbol"] == snapshot.symbol),
        {},
    )
    metadata = entry.get("contract_metadata") or {}
    multiplier = number(metadata.get("ctVal"))
    mult = number(metadata.get("ctMult")) or 1
    base = (snapshot.canonical_asset_id or "").removeprefix("asset:")
    with store.connect() as db:
        for raw_id in snapshot.raw_ids:
            raw = store.raw(raw_id)
            if not raw or datetime.fromisoformat(raw["received_at"]) > snapshot.available_at:
                continue
            payload = raw["payload"]
            venue = ""
            if "/api/v3/aggTrades" in raw["endpoint"] and isinstance(payload, list):
                venue = "Binance Spot"
                rows = [
                    (
                        str(r["a"]),
                        milliseconds(r["T"]),
                        float(r["p"]) * float(r["q"]) * (-1 if r["m"] else 1),
                    )
                    for r in payload
                ]
            elif (
                "/api/v5/market/trades?" in raw["endpoint"]
                and multiplier
                and metadata.get("ctValCcy") == base
                and isinstance(payload, dict)
            ):
                venue = "OKX Perp"
                rows = [
                    (
                        str(r["tradeId"]),
                        milliseconds(r["ts"]),
                        float(r["sz"])
                        * float(r["px"])
                        * multiplier
                        * mult
                        * (1 if r["side"] == "buy" else -1),
                    )
                    for r in payload.get("data", [])
                ]
            else:
                continue
            for trade_id, market_time, notional in rows:
                if market_time > snapshot.available_at or not math.isfinite(notional):
                    continue
                db.execute(
                    "INSERT OR IGNORE INTO trade_observations VALUES (?,?,?,?,?,?,?)",
                    (
                        venue,
                        snapshot.asset_id,
                        trade_id,
                        stamp(market_time),
                        raw["received_at"],
                        notional,
                        raw_id,
                    ),
                )


def cvd_summary(store, asset_id, as_of, window_seconds=3600):
    with store.connect() as db:
        rows = db.execute(
            "SELECT * FROM trade_observations WHERE asset_id=? AND market_time>=? "
            "AND market_time<=? AND received_at<=? ORDER BY market_time,trade_id",
            (
                asset_id,
                stamp(as_of - timedelta(seconds=window_seconds)),
                stamp(as_of),
                stamp(as_of),
            ),
        ).fetchall()
    result = []
    for venue in ("Binance Spot", "OKX Perp"):
        points = [dict(r) for r in rows if r["venue"] == venue]
        cumulative, series = 0.0, []
        for r in points:
            cumulative += r["signed_notional"]
            series.append({"time": r["market_time"], "observed_cvd_usdt": cumulative})
        ids = sorted(int(r["trade_id"]) for r in points if r["trade_id"].isdigit())
        gaps = sum(b - a - 1 for a, b in zip(ids, ids[1:], strict=False) if b - a > 1)
        result.append(
            {
                "venue": venue,
                "asset_id": asset_id,
                "window_seconds": window_seconds,
                "observed_trades": len(points),
                "observed_cvd_usdt": cumulative if points else None,
                "numeric_id_gaps": gaps,
                "series": series[-1000:],
                "coverage": "REST_SAMPLE_UNVERIFIED_COMPLETENESS",
                "full_market_cvd": None,
                "limits": "轮询可能遗漏成交；不同市场不直接比较绝对 CVD",
            }
        )
    return result
