"""Consolidate correlated opportunities after draining a frozen notification batch."""

import hashlib
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .decision_explanation import VERSION, explanation_lines


def _time(value):
    return (
        datetime.fromisoformat(value)
        .astimezone(timezone(timedelta(hours=8)))
        .strftime("%m-%d %H:%M:%S")
        if value
        else "未记录"
    )


def _price(value):
    return f"{value:.8g}" if value is not None else "未记录"


def notification_message(rows, category):
    latest = max(rows, key=lambda a: a.get("current_price_market_time") or "")
    header = {"LONG": "入场候选", "RISK": "当前市场风险", "CANCELLATION": "撤销已发候选"}[category]
    lines = [
        f"{latest['symbol']}｜{header}",
        f"最新价 {_price(latest.get('current_price'))}"
        f"（{_time(latest.get('current_price_market_time'))}，UTC+8）",
    ]
    changes = list(
        dict.fromkeys(
            r.get("notification_reason", "未记录")
            + (
                f"（{r['notification_from_level']} → {r['level']}）"
                if r.get("notification_from_level")
                else ""
            )
            for r in rows
        )
    )
    lines.append("本次变化：" + "；".join(changes) + "。")
    for row in rows:
        if category == "CANCELLATION":
            threshold = row.get("original_invalidation_price")
            current = row.get("current_price")
            recovery = (
                "当前已回到旧阈值之上；原候选不会自动恢复"
                if threshold is not None and current is not None and current >= threshold
                else "当前仍低于旧阈值"
                if threshold is not None and current is not None
                else "原阈值未记录"
            )
            reason = row.get("cancellation_reason", "；".join(row.get("evidence", [])))
            lines += [
                f"{row['rule_id']}：原候选参考价 {_price(row.get('original_candidate_price'))}"
                f"（{_time(row.get('original_candidate_at'))}），"
                f"失效规则阈值 {_price(threshold)}。",
                f"失效于 {_time(row['notification_at'])}，"
                f"当时报价 {_price(row.get('notification_price'))}；"
                f"原因：{reason}。{recovery}。",
            ]
        elif category == "RISK":
            f = row.get("current_risk_inputs", {})
            facts = []
            if row["rule_id"] == "spot-sell-pressure":
                facts = (
                    [
                        f"15 分钟变化 {f['return_15m_pct']:.2f}%",
                        f"主动买入 {f['spot_taker_buy_ratio']:.0%}",
                    ]
                    if all(f.get(k) is not None for k in ("return_15m_pct", "spot_taker_buy_ratio"))
                    else []
                )
            elif row["rule_id"] == "leverage-overheat":
                facts = (
                    [
                        f"Funding {f['funding_rate_pct']:.3f}%",
                        f"OI 5 分钟变化 {f['oi_change_5m_pct']:.2f}%",
                    ]
                    if all(f.get(k) is not None for k in ("funding_rate_pct", "oi_change_5m_pct"))
                    else []
                )
            lines.append(f"{row['rule_id']}：{'；'.join(facts or row.get('evidence', []))}。")
        else:
            zone = row.get("entry_zone")
            threshold = row.get("invalidation_price")
            current = row.get("current_price")
            distance = (
                f"，距当前价 {(1 - threshold / current) * 100:.2f}%"
                if current and threshold
                else ""
            )
            lines += [
                f"{row['rule_id']}：{'；'.join(row.get('evidence', []))}。",
                f"候选参考区间 {'–'.join(_price(p) for p in zone) if zone else '未记录'}；"
                f"失效规则阈值 {_price(threshold)}{distance}。",
            ]
        if row.get("contradictions"):
            lines.append("限制：" + "；".join(row["contradictions"]) + "。")
    if category != "CANCELLATION":
        lines += explanation_lines(rows)
    if category in ("LONG", "CANCELLATION"):
        methods = {r.get("invalidation_method") for r in rows}
        lines.append(
            "规则阈值按 2×ATR、最少距参考价 0.5% 计算；ATR 缺失时回退 2%。"
            "阈值最低为参考价的 1%；未验证为支撑位。"
            if methods == {"ATR_2_FLOOR_0_5_FALLBACK_2"}
            else "规则阈值不是经过验证的支撑位；旧记录的计算方法可能缺失。"
        )
    lines.append(
        "仅撤销原候选，不代表新的看空判断。"
        if category == "CANCELLATION"
        else "规则尚未证明交易收益；请结合自己的交易计划判断。"
    )
    return "\n".join(lines)


def build_deliveries(items):
    groups = defaultdict(dict)
    for item in items:
        if item.get("delivery_status") != "READY":
            continue
        if item.get("receipt", {}).get("status") in ("SENT", "SKIPPED"):
            continue
        # Cancellation is a lifecycle update, independent of genuine market risk.
        key = (item["asset_id"], "LONG", item.get("horizon_seconds", 3600))
        classification = item.get("notification_class")
        if classification == "ARCHIVE":
            continue
        if classification == "CANCELLATION":
            key = (
                item["asset_id"],
                "CANCELLATION",
                item.get("cancellation_reason", "；".join(item.get("evidence", []))),
            )
        elif classification == "MARKET_RISK" or item["level"] == "CRITICAL_RISK":
            key = (item["asset_id"], "RISK", item["notification_id"])
        groups[key][item["notification_id"]] = item
    deliveries = []
    for key, members in groups.items():
        ids = sorted(members)
        rows = [members[i] for i in ids]
        ranked = [a for a in rows if key[1] == "LONG" and a.get("opportunity_score") is not None]
        latest = max(ranked, key=lambda a: a["notification_at"]) if ranked else None
        deliveries.append(
            {
                "delivery_id": hashlib.sha256("|".join(ids).encode()).hexdigest(),
                "asset_id": key[0],
                "symbol": rows[0]["symbol"],
                "category": key[1],
                "message_zh": notification_message(rows, key[1]),
                "decision_explanation_version": VERSION,
                "decision_explanations": [
                    {"notification_id": a["notification_id"], "decision": a.get("decision", {})}
                    for a in rows
                ],
                "member_notification_ids": ids,
                "rule_ids": sorted({a["rule_id"] for a in rows}),
                "patterns": sorted({p for a in rows for p in a.get("patterns", [])}),
                "opportunity_score": latest["opportunity_score"] if latest else None,
                "rule_evidence_scores": {a["rule_id"]: a.get("rule_evidence_score") for a in rows},
                "parent_signal_ids": sorted(
                    {a["parent_signal_id"] for a in rows if a.get("parent_signal_id")}
                ),
                "risk_level": "CRITICAL_RISK" if key[1] == "RISK" else None,
                "independent_strategy_count": None,
                "interpretation": "同币相关条件合并；分数不相加，不代表多套独立策略确认"
                if key[1] == "LONG"
                else "同币同原因的撤销合并；仅更新已发候选的状态"
                if key[1] == "CANCELLATION"
                else "风险等级独立于机会排行",
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
            deferred = [a for a in items if a.get("delivery_status") == "WAIT_PARENT_RECEIPT"]
            return {
                "generation": generation,
                "upper_cursor": boundary,
                "next_cursor": cursor,
                "commit_cursor": min(a["sequence"] for a in deferred) - 1 if deferred else cursor,
                "items": items,
                "delivery_groups": build_deliveries(items),
                "deferred_notification_ids": [a["notification_id"] for a in deferred],
                "delivery_policy_version": page.get("delivery_policy_version"),
                "decision_explanation_version": page.get("decision_explanation_version"),
                "as_of": page["as_of"],
            }
    raise ValueError("Notification batch exceeds 200 pages; cursor was not committed")
