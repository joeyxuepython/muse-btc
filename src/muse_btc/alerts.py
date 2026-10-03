import re
from datetime import datetime

from .models import SignalKind, new_id

LEVELS = {"INFO": 0, "WATCH": 1, "SETUP": 2, "STRONG": 3, "CRITICAL_RISK": 4}


def evidence_signature(values):
    # Numeric updates stay visible without making every tick a new notification.
    return [re.sub(r"[-+]?\d+(?:\.\d+)?", "#", v) for v in values]


def publish_alert(store, signal, ranking, now, settings):
    level = "WATCH"
    if signal.kind in (SignalKind.RISK, SignalKind.INVALIDATED):
        level = "CRITICAL_RISK"
    elif signal.kind == SignalKind.ENTRY_CANDIDATE and len(set(signal.evidence_groups)) >= 3:
        level = "STRONG"
    elif len(set(signal.evidence_groups)) >= settings.rule_thresholds.get("setup_groups", 3):
        level = "SETUP"
    candidate = next(
        (
            a
            for a in store.alerts()
            if a["asset_id"] == signal.asset_id
            and a["rule_id"] == signal.rule_id
            and not a.get("resolved_at")
            and datetime.fromisoformat(a["expires_at"]) > now
        ),
        None,
    )
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
            or evidence_signature(candidate["contradictions"])
            != evidence_signature(signal.contradictions)
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
            candidate["read_at"] = None
        alert = candidate
    else:
        alert = {
            "id": new_id(),
            "asset_id": signal.asset_id,
            "rule_id": signal.rule_id,
            "first_seen": at,
            "read_at": None,
            "pinned": False,
            "resolved_at": None,
            "escalated_at": None,
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
        alert["notification_revision"] = at
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
            "contradictions": signal.contradictions,
            "invalidation_conditions": signal.invalidation_conditions,
            "ranking": ranking,
            "validation_status": "OBSERVATION_ONLY",
            "signal_version": "web-alert-v4-1",
            "rule_version": signal.rule_version,
            "evidence_groups": payload["evidence_groups"],
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
    alert = next((a for a in store.alerts() if a["id"] == alert_id), None)
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
