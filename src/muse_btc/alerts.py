import re
from copy import deepcopy
from datetime import datetime, timedelta

from .decision_explanation import semantic_signature
from .evaluation_policy import evaluation_id
from .models import SignalKind, new_id
from .rules import component_usable, usable

LEVELS = {"INFO": 0, "WATCH": 1, "SETUP": 2, "STRONG": 3, "CRITICAL_RISK": 4}
DELIVERY_POLICY_VERSION = "meaningful-change-v2"


def notification_projections(store, alerts, now):
    """Apply the current policy to both new and immutable historical payloads."""
    signals = store.signals_by_ids(list({a["signal_id"] for a in alerts}))
    parent_ids = {
        a.get("parent_signal_id")
        or (signals[a["signal_id"]].parent_signal_id if a["signal_id"] in signals else None)
        for a in alerts
    } - {None}
    parents = store.signals_by_ids(list(parent_ids))
    origins = store.notification_origins(list(parent_ids), now)
    results = []
    for alert in alerts:
        result = dict(alert)
        signal = signals.get(alert["signal_id"])
        if signal:
            for key in ("invalidation_price", "invalidation_method", "entry_zone"):
                result.setdefault(key, getattr(signal, key))
        kind = signal.kind if signal else alert.get("signal_kind")
        invalidated = (
            kind == SignalKind.INVALIDATED
            or alert.get("risk_type") == "SIGNAL_INVALIDATED"
            or alert.get("title") == "原信号失效"
        )
        result["notification_class"] = (
            "CANCELLATION"
            if invalidated
            else "MARKET_RISK"
            if kind == SignalKind.RISK or alert["level"] == "CRITICAL_RISK"
            else "OPPORTUNITY"
            if alert["level"] == "STRONG"
            else "ARCHIVE"
        )
        if invalidated:
            parent_id = signal.parent_signal_id if signal else alert.get("parent_signal_id")
            parent = parents.get(parent_id)
            notices = origins.get(parent_id, [])
            sent = [n for n in notices if n["receipt"].get("status") == "SENT"]
            origin = (sent or notices or [None])[-1]
            result.update(
                parent_signal_id=parent_id,
                parent_had_strong_notice=bool(notices),
                parent_delivery_confirmed=bool(sent),
                parent_notification_ids=[n["notification_id"] for n in sent],
                original_candidate_price=origin.get("notification_price") if origin else None,
                original_candidate_at=origin["notification_at"] if origin else None,
                original_notice_sent_at=origin["receipt"].get("received_at") if origin else None,
                original_invalidation_price=parent.invalidation_price if parent else None,
                invalidation_method=parent.invalidation_method if parent else None,
                cancellation_reason="；".join(alert.get("evidence", [])),
                display_level="WATCH" if notices else "INFO",
                risk_level=None,
            )
            deadline = (
                min(
                    parent.expires_at,
                    datetime.fromisoformat(origin["notification_at"])
                    + timedelta(seconds=origin.get("horizon_seconds", parent.horizon_seconds)),
                )
                if parent and origin
                else None
            )
            result["parent_observation_expires_at"] = deadline.isoformat() if deadline else None
            result["cancellation_delivery_status"] = (
                "SKIP_UNDELIVERED_PARENT"
                if not parent or not notices
                else "SKIP_PARENT_EXPIRED"
                if deadline <= now
                else "READY"
                if sent
                else "WAIT_PARENT_RECEIPT"
                if any(n["receipt"].get("status") != "SKIPPED" for n in notices)
                else "SKIP_UNDELIVERED_PARENT"
            )
        results.append(result)
    return results


def market_risk_status(alert, current, now, settings):
    if not current or not usable(current, now, settings):
        return "SKIP_STALE_DATA"
    f = current.features
    if alert["rule_id"] == "spot-sell-pressure":
        if f.return_15m_pct is None or f.spot_taker_buy_ratio is None:
            return "SKIP_STALE_DATA"
        active = f.return_15m_pct <= -1.5 and f.spot_taker_buy_ratio < 0.45
    elif alert["rule_id"] == "leverage-overheat":
        if (
            not all(component_usable(current, k, now, settings) for k in ("funding", "oi_history"))
            or f.funding_rate_pct is None
            or f.oi_change_5m_pct is None
        ):
            return "SKIP_STALE_DATA"
        active = f.funding_rate_pct >= 0.05 and f.oi_change_5m_pct >= 2
    else:
        return "READY"
    return "READY" if active else "SKIP_RISK_CLEARED"


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


def pre_pump_delivery_status(alert, current_price, now):
    deadline = alert.get("confirmation_deadline")
    if not deadline:
        return "SKIP_STALE_DATA"
    if datetime.fromisoformat(deadline) <= now:
        return "SKIP_EXPIRED"
    prices = [alert.get("first_price"), alert.get("notification_price")]
    if not current_price or not all(prices) or alert.get("max_chase_pct") is None:
        return "SKIP_STALE_DATA"
    if any(
        round((current_price / price - 1) * 100, 6) > alert["max_chase_pct"] for price in prices
    ):
        return "SKIP_PRICE_EXTENDED"
    return "READY"


def publish_alert(store, signal, ranking, now, settings):
    level = "WATCH"
    invalidated = signal.kind == SignalKind.INVALIDATED
    origins = (
        store.notification_origins([signal.parent_signal_id], now)
        if invalidated and signal.parent_signal_id
        else {}
    )
    parent_had_strong = bool(origins.get(signal.parent_signal_id))
    if invalidated:
        level = "WATCH" if parent_had_strong else "INFO"
    elif signal.kind == SignalKind.RISK:
        level = "CRITICAL_RISK"
    elif signal.kind == SignalKind.ENTRY_CANDIDATE and len(set(signal.evidence_groups)) >= 3:
        level = "STRONG"
    elif len(set(signal.evidence_groups)) >= settings.rule_thresholds.get("setup_groups", 3):
        level = "SETUP"
    storage_rule = (
        "invalidation:" + (signal.parent_signal_id or signal.id) if invalidated else signal.rule_id
    )
    candidate = store.active_alert(signal.asset_id, storage_rule, now)
    snapshot = store.snapshot(signal.snapshot_id)
    requested_level = level
    confirmation = {}
    contradictions = list(signal.contradictions)
    if signal.rule_id == "pre-pump-fusion" and level == "STRONG":
        confirmation = pre_pump_confirmation(candidate, signal, snapshot, now, settings)
        if confirmation["confirmation_status"] != "CONFIRMED":
            level = "SETUP"
            contradictions.append(confirmation["confirmation_reason"])
    tracking_score = ranking["score"] if ranking else signal.evidence_score
    risk = signal.kind in (SignalKind.RISK, SignalKind.INVALIDATED)
    observation = signal.model_version == "context-observation"
    evidence_score = None if risk or observation else signal.evidence_score
    score = None if risk else evidence_score
    opportunity_score = ranking["score"] if ranking else None
    at = now.isoformat()
    payload = signal.model_dump(mode="json")
    decision = deepcopy(signal.decision)
    if decision:
        decision["publication"] = {
            "requested_level": requested_level,
            "level": level,
            "confirmation_status": confirmation.get("confirmation_status"),
            "confirmation_deadline": confirmation.get("confirmation_deadline"),
            "gain_since_first_seen_pct": confirmation.get("gain_since_first_seen_pct"),
            "max_chase_pct": confirmation.get("max_chase_pct"),
            "reason": confirmation.get("confirmation_reason"),
            "ranking_role": "DISPLAY_ORDER_ONLY",
            "opportunity_score": opportunity_score,
        }
    event = None
    if candidate:
        changed = (
            candidate["level"] != level
            or candidate.get("score_schema") != "separated-v1"
            or evidence_signature(candidate["evidence"]) != evidence_signature(signal.evidence)
            or evidence_signature(candidate["contradictions"]) != evidence_signature(contradictions)
            or set(candidate["evidence_groups"]) != set(signal.evidence_groups)
            or candidate["rule_version"] != signal.rule_version
            or (
                candidate.get("decision")
                and decision
                and semantic_signature(candidate["decision"]) != semantic_signature(decision)
            )
        )
        if changed:
            event = {
                "event_at": at,
                "from_level": candidate["level"],
                "to_level": level,
                "score_delta": round(score - candidate["score"], 2)
                if score is not None
                and candidate.get("score") is not None
                and candidate.get("score_schema") == "separated-v1"
                else None,
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
            else min(
                signal.expires_at,
                now + timedelta(seconds=settings.risk_notification_max_age_seconds),
            )
            if risk
            else signal.expires_at
        ).isoformat()
        alert["notification_price_market_time"] = (
            snapshot.market_time.isoformat() if snapshot else None
        )
        alert["notification_price_available_at"] = (
            snapshot.available_at.isoformat() if snapshot else None
        )
        alert["price_provenance"] = "SIGNAL_REFERENCE"
        alert["notification_tracking_score"] = tracking_score
        alert["notification_score"] = score
    previous_score = alert.get("score") if alert.get("score_schema") == "separated-v1" else None
    if alert.get("score_schema") != "separated-v1":
        alert["max_score"] = score
    alert.update(
        {
            "symbol": signal.symbol,
            "storage_rule_id": storage_rule,
            "signal_kind": signal.kind,
            "parent_signal_id": signal.parent_signal_id,
            "parent_had_strong_notice": parent_had_strong,
            "notification_class": "CANCELLATION"
            if invalidated
            else "MARKET_RISK"
            if risk
            else "OPPORTUNITY"
            if level == "STRONG"
            else "ARCHIVE",
            "level": level,
            "score": score,
            "score_schema": "separated-v1",
            "score_kind": "RULE_EVIDENCE" if score is not None else None,
            "opportunity_score": opportunity_score,
            "rule_evidence_score": evidence_score,
            "risk_level": "CRITICAL_RISK" if signal.kind == SignalKind.RISK else None,
            "risk_type": "SIGNAL_INVALIDATED"
            if signal.kind == SignalKind.INVALIDATED
            else "MARKET_RISK"
            if risk
            else None,
            "original_signal_evidence_score": signal.evidence_score
            if signal.kind == SignalKind.INVALIDATED
            else None,
            "previous_score": previous_score,
            "score_delta": round(score - previous_score, 2)
            if previous_score is not None and score is not None
            else None,
            "last_updated": at,
            "expires_at": signal.expires_at.isoformat(),
            "max_score": max(alert.get("max_score") or score, score) if score is not None else None,
            "signal_id": signal.id,
            "snapshot_id": signal.snapshot_id,
            "price": signal.reference_price,
            "title": signal.title,
            "evidence": signal.evidence,
            "contradictions": contradictions,
            "invalidation_conditions": signal.invalidation_conditions,
            "invalidation_price": signal.invalidation_price,
            "invalidation_method": signal.invalidation_method,
            "entry_zone": signal.entry_zone,
            "ranking": ranking,
            "validation_status": "OBSERVATION_ONLY",
            "signal_version": "web-alert-v4-5",
            "rule_version": signal.rule_version,
            "evidence_groups": payload["evidence_groups"],
            "requested_level": requested_level,
            "confirmation_status": confirmation.get("confirmation_status"),
            "confirmation_deadline": confirmation.get("confirmation_deadline"),
            "gain_since_first_seen_pct": confirmation.get("gain_since_first_seen_pct"),
            "confirmation_reason": confirmation.get("confirmation_reason"),
            "max_chase_pct": confirmation.get("max_chase_pct"),
            "patterns": signal.patterns,
            "context": signal.context,
            "decision": decision,
            "horizon_seconds": signal.horizon_seconds,
            "evaluation": signal.evaluation.model_dump(mode="json") if signal.evaluation else None,
            "evaluation_policy_id": evaluation_id(signal.evaluation) if signal.evaluation else None,
            "score_limitations": "机会分用于横向排名；规则证据分未校准；风险等级不由分数换算",
        }
    )
    store.save_alert(alert, event)
    if invalidated and signal.parent_signal_id:
        previous = store.active_alert(signal.asset_id, signal.rule_id, now)
        if previous and previous["signal_id"] == signal.parent_signal_id:
            previous["resolved_at"] = at
            store.save_alert(
                previous, {"event_at": at, "reason": "原候选失效", "action": "resolve"}
            )
    return alert


def alert_view(alert, now):
    result = public_scores(alert)
    result["state"] = (
        "RESOLVED"
        if alert.get("resolved_at")
        else "EXPIRED"
        if datetime.fromisoformat(alert["expires_at"]) <= now
        else "ACTIVE"
    )
    result["unread"] = result["state"] == "ACTIVE" and not alert.get("read_at")
    return result


def public_scores(alert):
    result = dict(alert)
    if result.get("score_schema") != "separated-v1":
        result.update(
            legacy_score=result.get("score"),
            score=None,
            score_kind=None,
            opportunity_score=(result.get("ranking") or {}).get("score"),
            rule_evidence_score=None,
            risk_level="CRITICAL_RISK"
            if result["level"] == "CRITICAL_RISK"
            and result.get("notification_class") != "CANCELLATION"
            else None,
            score_schema="LEGACY_UNSEPARATED",
        )
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
