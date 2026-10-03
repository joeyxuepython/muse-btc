from datetime import datetime

from .models import Module
from .rules import quote_usable, usable


def rank_assets(snapshots, previous, now: datetime, settings):
    prior = {r["canonical_asset_id"]: r for r in previous}
    rows = []
    for snapshot in snapshots:
        if snapshot.module != Module.ALT:
            continue
        f = snapshot.features
        values = {
            "relative_volume": min(max((f.relative_volume or 0) - 1, 0) / 2, 1)
            if f.relative_volume is not None
            else None,
            "relative_strength": min(max(f.relative_strength_15m_pct or 0, 0) / 1, 1)
            if f.relative_strength_15m_pct is not None
            else None,
            "spot_flow": min(max((f.spot_taker_buy_ratio or 0) - 0.5, 0) * 5, 1)
            if f.spot_taker_buy_ratio is not None
            else None,
            "oi": min(max(f.oi_change_5m_pct or 0, 0) / 2, 1)
            if f.oi_change_5m_pct is not None
            else None,
            "funding": max(0, 1 - max(f.funding_rate_pct or 0, 0) / 0.05)
            if f.funding_rate_pct is not None
            else None,
            "taker": min(max((f.perp_taker_buy_ratio or 0) - 0.5, 0) * 5, 1)
            if f.perp_taker_buy_ratio is not None
            else None,
            "liquidity": max(0, 1 - (f.spread_bps or 0) / 20) if f.spread_bps is not None else None,
        }
        # Cached detailed statistics keep their original age, not the new quote's age.
        for component, fields in {
            "oi_history": ("oi",),
            "funding": ("funding",),
            "taker": ("taker",),
        }.items():
            timestamp = snapshot.component_times.get(component)
            maximum = (
                settings.stale_seconds
                if component == "funding"
                else settings.statistics_stale_seconds
            )
            if snapshot.component_times and (
                timestamp is None or not 0 <= (now - timestamp).total_seconds() <= maximum
            ):
                for field in fields:
                    values[field] = None
        current = usable(snapshot, now, settings)
        if not current:
            values = {k: None for k in values}
        contributions = {
            k: round(settings.ranking_weights.get(k, 0) * v, 4)
            for k, v in values.items()
            if v is not None
        }
        total_weight = sum(settings.ranking_weights.values())
        score = round(sum(contributions.values()) / total_weight * 100, 2) if total_weight else 0
        coverage = (
            round(
                sum(settings.ranking_weights.get(k, 0) for k, v in values.items() if v is not None)
                / total_weight
                * 100,
                1,
            )
            if total_weight
            else 0
        )
        canonical = snapshot.canonical_asset_id or snapshot.asset_id
        old = prior.get(canonical)
        rows.append(
            {
                "canonical_asset_id": canonical,
                "asset_id": snapshot.asset_id,
                "symbol": snapshot.symbol,
                "snapshot_id": snapshot.id,
                "as_of": now.isoformat(),
                "score": score,
                "previous_score": old["score"] if old else None,
                "score_delta": round(score - old["score"], 2) if old else None,
                "previous_rank": old["rank"] if old else None,
                "score_components": contributions,
                "score_changes": {
                    k: round(contributions.get(k, 0) - old.get("score_components", {}).get(k, 0), 4)
                    for k in set(contributions) | set(old.get("score_components", {}))
                }
                if old
                else {},
                "coverage_pct": coverage,
                "missing": [k for k, v in values.items() if v is None],
                "price": snapshot.price,
                "quote_fresh": quote_usable(snapshot, now, settings),
                "data_ready": current,
                "tier": snapshot.tier,
                "validation_status": "OBSERVATION_ONLY",
                "version": "rank-v4-1",
                "weights": dict(settings.ranking_weights),
            }
        )
    rows.sort(key=lambda r: (not r["data_ready"], -r["score"], r["symbol"]))
    for i, row in enumerate(rows, 1):
        row["rank"] = i
        row["rank_change"] = row["previous_rank"] - i if row["previous_rank"] is not None else None
    return rows
