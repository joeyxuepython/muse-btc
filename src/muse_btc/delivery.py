"""Consolidate correlated opportunities after draining a frozen notification batch."""

import hashlib
from collections import defaultdict


def build_deliveries(items):
    groups = defaultdict(dict)
    for item in items:
        if item.get("delivery_status") != "READY":
            continue
        if item.get("receipt", {}).get("status") in ("SENT", "SKIPPED"):
            continue
        # Risks remain separate and cannot be suppressed by a bullish score.
        key = (item["asset_id"], "LONG", item.get("horizon_seconds", 3600))
        if item["level"] == "CRITICAL_RISK":
            key = (item["asset_id"], "RISK", item["notification_id"])
        groups[key][item["notification_id"]] = item
    deliveries = []
    for key, members in groups.items():
        ids = sorted(members)
        rows = [members[i] for i in ids]
        ranked = [a for a in rows if a.get("opportunity_score") is not None]
        latest = max(ranked, key=lambda a: a["notification_at"]) if ranked else None
        deliveries.append(
            {
                "delivery_id": hashlib.sha256("|".join(ids).encode()).hexdigest(),
                "asset_id": key[0],
                "symbol": rows[0]["symbol"],
                "category": key[1],
                "member_notification_ids": ids,
                "rule_ids": sorted({a["rule_id"] for a in rows}),
                "patterns": sorted({p for a in rows for p in a.get("patterns", [])}),
                "opportunity_score": latest["opportunity_score"] if latest else None,
                "rule_evidence_scores": {a["rule_id"]: a.get("rule_evidence_score") for a in rows},
                "risk_level": "CRITICAL_RISK" if key[1] == "RISK" else None,
                "independent_strategy_count": None,
                "interpretation": "同币相关条件合并；分数不相加，不代表多套独立策略确认"
                if key[1] == "LONG"
                else "风险等级独立于机会排行；失效通知保留原信号关联",
                "members": rows,
            }
        )
    return deliveries


async def fetch_notification_batch(client, *, after=0, generation=None, page_size=50):
    """Read-only helper; the sender commits next_cursor only after all groups are handled."""
    cursor, boundary, items = after, None, []
    for _ in range(200):
        params = {"after": cursor, "limit": page_size}
        if generation is not None:
            params["generation"] = generation
        if boundary is not None:
            params["through"] = boundary
        response = await client.get("/api/alerts/notifications", params=params)
        response.raise_for_status()
        page = response.json()
        if boundary is None:
            boundary, generation = page["upper_cursor"], page["generation"]
        if page["generation"] != generation or page["upper_cursor"] != boundary:
            raise ValueError("Notification batch changed; cursor was not committed")
        items += page["items"]
        next_cursor = page["next_cursor"]
        if page["has_more"] and next_cursor <= cursor:
            raise ValueError("Notification page did not advance")
        cursor = next_cursor
        if not page["has_more"]:
            return {
                "generation": generation,
                "upper_cursor": boundary,
                "next_cursor": cursor,
                "items": items,
                "delivery_groups": build_deliveries(items),
                "as_of": page["as_of"],
            }
    raise ValueError("Notification batch exceeds 200 pages; cursor was not committed")
