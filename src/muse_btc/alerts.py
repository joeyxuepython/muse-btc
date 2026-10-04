import re
from datetime import datetime, timedelta

from .models import SignalKind, new_id

LEVELS = {"INFO": 0, "WATCH": 1, "SETUP": 2, "STRONG": 3, "CRITICAL_RISK": 4}


def evidence_signature(values):
    # Numeric updates stay visible without making every tick a new notification.
    return [re.sub(r"[-+]?\d+(?:\.\d+)?", "#", v) for v in values]


def pre_pump_confirmation(candidate, signal, snapshot, now, settings):
    """Delivery policy only; archived strategy signals/replays remain untouched."""
    first_seen = datetime.fromisoformat(candidate["first_seen"]) if candidate else now
    first_price = candidate.get("first_price") if candidate else signal.reference_price
    deadline = first_seen + timedelta(seconds=settings.pre_pump_confirmation_seconds)
    gain = round((signal.reference_price / first_price - 1) * 100, 6) if first_price else None
    status = "CONFIRMED"
    reason = None
    if now < first_seen or now >= deadline:
        status, reason = "TOO_LATE", "pre-pump 确认超出首次发现后的时间窗口"
    elif gain is None or snapshot is None:
        status, reason = "UNKNOWN_ORIGIN", "缺少首次价格或确认快照，无法验证追涨幅度"
    elif gain > settings.pre_pump_max_chase_pct or any(
        value is not None and value > settings.pre_pump_max_chase_pct
        for value in (snapshot.features.return_5m_pct, snapshot.features.return_15m_pct)
    ):
        status, reason = "PRICE_EXTENDED", "pre-pump 确认时涨幅已超过通知追涨上限"
    return {
        "confirmation_status": status,
        "confirmation_deadline": deadline.isoformat(),
        "gain_since_first_seen_pct": gain,
        "confirmation_reason": reason,
        "max_chase_pct": settings.pre_pump_max_chase_pct,
    }


def publish_alert(store, signal, ranking, now, settings):
    level = "WATCH"
    if signal.kind in (SignalKind.RISK, SignalKind.INVALIDATED):
        level = "CRITICAL_RISK"
    elif signal.kind == SignalKind.ENTRY_CANDIDATE and len(set(signal.evidence_groups)) >= 3:
        level = "STRONG"
    elif len(set(signal.evidence_groups)) >= settings.rule_thresholds.get("setup_groups", 3):
        level = "SETUP"
    candidate = store.active_alert(signal.asset_id, signal.rule_id, now)
    snapshot = store.snapshot(signal.snapshot_id)
    requested_level = level
    confirmation = {}
    contradictions = list(signal.contradictions)
    if signal.rule_id == "pre-pump-fusion" and level == "STRONG":
        confirmation = pre_pump_confirmation(candidate, signal, snapshot, now, settings)
        if confirmation["confirmation_status"] != "CONFIRMED":
            level = "SETUP"
            contradictions.append(confirmation["confirmation_reason"])
    score = ranking["score"] if ranking else signal.evidence_score
    at = now.isoformat()
    payload = signal.model_dump(mode="json")
    event = None
    if candidate:
        changed = (
            candidate["level"] != level
            or abs(candidate.get("notification_score", candidate["score"]) - score)
            >= settings.alert_score_delta
            or evidence_signature(candidate["evidence"]) != evidence_signature(signal.evidence)
            or evidence_signature(candidate["contradictions"]) != evidence_signature(contradictions)
            or set(candidate["evidence_groups"]) != set(signal.evidence_groups)
            or candidate["rule_version"] != signal.rule_version
        )
        if changed:
            event = {
                "event_at": at,
                "from_level": candidate["level"],
                "to_level": level,
                "score_delta": round(score - candidate["score"], 2),
                "reason": "证据或级别变化",
            }
            if LEVELS[level] > LEVELS[candidate["level"]]:
                candidate["escalated_at"] = at
                candidate["escalated_price"] = signal.reference_price
                candidate["escalated_level"] = level
            candidate["read_at"] = None
        alert = candidate
    else:
        alert = {
            "id": new_id(),
            "asset_id": signal.asset_id,
            "rule_id": signal.rule_id,
            "first_seen": at,
            "first_price": signal.reference_price,
            "read_at": None,
            "pinned": False,
            "resolved_at": None,
            "escalated_at": at if level in ("STRONG", "CRITICAL_RISK") else None,
            "escalated_price": signal.reference_price
            if level in ("STRONG", "CRITICAL_RISK")
            else None,
            "escalated_level": level if level in ("STRONG", "CRITICAL_RISK") else None,
            "max_score": score,
        }
        event = {
            "event_at": at,
            "from_level": None,
            "to_level": level,
            "score_delta": None,
            "reason": "首次发现",
        }
    if event:
        event["notification_id"] = new_id()
        event["price"] = signal.reference_price
        alert["notification_revision"] = at
        alert["notification_id"] = event["notification_id"]
        alert["notification_at"] = at
        alert["notification_price"] = signal.reference_price
        alert["notification_reason"] = event["reason"]
        alert["notification_from_level"] = event["from_level"]
        alert["notification_expires_at"] = (
            min(signal.expires_at, datetime.fromisoformat(confirmation["confirmation_deadline"]))
            if confirmation and level == "STRONG"
            else signal.expires_at
        ).isoformat()
        alert["notification_price_market_time"] = (
            snapshot.market_time.isoformat() if snapshot else None
        )
        alert["notification_price_available_at"] = (
            snapshot.available_at.isoformat() if snapshot else None
        )
        alert["price_provenance"] = "SIGNAL_REFERENCE"
        alert["notification_score"] = score
    previous_score = alert.get("score")
    alert.update(
        {
            "symbol": signal.symbol,
            "level": level,
            "score": score,
            "previous_score": previous_score,
            "score_delta": round(score - previous_score, 2) if previous_score is not None else None,
            "last_updated": at,
            "expires_at": signal.expires_at.isoformat(),
            "max_score": max(alert["max_score"], score),
            "signal_id": signal.id,
            "snapshot_id": signal.snapshot_id,
            "price": signal.reference_price,
            "title": signal.title,
            "evidence": signal.evidence,
            "contradictions": contradictions,
            "invalidation_conditions": signal.invalidation_conditions,
            "ranking": ranking,
            "validation_status": "OBSERVATION_ONLY",
            "signal_version": "web-alert-v4-2",
            "rule_version": signal.rule_version,
            "evidence_groups": payload["evidence_groups"],
            "requested_level": requested_level,
            "confirmation_status": confirmation.get("confirmation_status"),
            "confirmation_deadline": confirmation.get("confirmation_deadline"),
            "gain_since_first_seen_pct": confirmation.get("gain_since_first_seen_pct"),
            "confirmation_reason": confirmation.get("confirmation_reason"),
            "max_chase_pct": confirmation.get("max_chase_pct"),
        }
    )
    store.save_alert(alert, event)
    return alert


def alert_view(alert, now):
    result = dict(alert)
    result["state"] = (
        "RESOLVED"
        if alert.get("resolved_at")
        else "EXPIRED"
        if datetime.fromisoformat(alert["expires_at"]) <= now
        else "ACTIVE"
    )
    result["unread"] = result["state"] == "ACTIVE" and not alert.get("read_at")
    return result


def change_alert(store, alert_id, action, now):
    alert = store.alert(alert_id)
    if not alert:
        return None
    if action == "read":
        alert["read_at"] = now.isoformat()
    elif action == "unread":
        alert["read_at"] = None
    elif action == "pin":
        alert["pinned"] = True
    elif action == "unpin":
        alert["pinned"] = False
    elif action == "resolve":
        alert["resolved_at"] = now.isoformat()
    else:
        raise ValueError("unsupported action")
    store.save_alert(alert, {"event_at": now.isoformat(), "action": action})
    return alert_view(alert, now)
