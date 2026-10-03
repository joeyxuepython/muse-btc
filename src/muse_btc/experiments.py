"""Phase 6 offline validation framework. Training never promotes a production signal."""

import math
import random
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from .intelligence import EvidenceRecord, IntelligenceStore, digest
from .models import SignalKind
from .rules import quote_usable

FEATURES = (
    "relative_volume",
    "relative_strength_15m_pct",
    "spot_taker_buy_ratio",
    "oi_acceleration_pct",
    "funding_rate_pct",
    "perp_taker_buy_ratio",
    "spread_bps",
    "depth_imbalance",
    "realized_volatility_pct",
)
RANK_HORIZONS = (14400, 86400, 259200, 604800)


def archived_batches(store, now):
    import json

    with store.connect() as db:
        rows = db.execute(
            "SELECT batch_id,as_of,payload FROM ranking_history WHERE as_of<=? "
            "ORDER BY as_of,rowid",
            (now.isoformat(),),
        ).fetchall()
    batches = defaultdict(list)
    for row in rows:
        batches[(row["batch_id"], row["as_of"])].append(json.loads(row["payload"]))
    return sorted(
        (
            (datetime.fromisoformat(at), sorted(values, key=lambda r: r["rank"]))
            for (_, at), values in batches.items()
        ),
        key=lambda b: b[0],
    )


def forward_label(store, row, at, horizon, now, settings):
    target = at + timedelta(seconds=horizon)
    if target > now:
        return {"status": "PENDING"}
    start = store.snapshot(row["snapshot_id"])
    if not start or start.available_at > at or not quote_usable(start, at, settings):
        return {"status": "INVALID_BASELINE"}
    history = store.snapshot_range(
        at, min(now, target + timedelta(seconds=settings.poll_seconds * 2)), row["asset_id"]
    )
    history = [
        s for s in history if s.market_time > at and quote_usable(s, s.available_at, settings)
    ]
    end = next((s for s in history if s.market_time >= target), None)
    if not end:
        return {"status": "MISSING_FUTURE", "survivorship_policy": "RETAINED_MISSING_NOT_DROPPED"}
    path = [s for s in history if s.available_at <= end.available_at]
    stamps = [at] + [s.market_time for s in path]
    gap = max(
        ((b - a).total_seconds() for a, b in zip(stamps, stamps[1:], strict=False)), default=horizon
    )
    if gap > settings.stale_seconds:
        return {"status": "GAPPED", "max_gap_seconds": gap}
    returns = [0] + [(s.price / start.price - 1) * 100 for s in path]
    threshold_hit = next(
        (s for s, v in zip(stamps, returns, strict=True) if v >= settings.breakout_threshold_pct),
        None,
    )
    return {
        "status": "MEASURED",
        "return_pct": (end.price / start.price - 1) * 100,
        "mfe_pct": max(returns),
        "mae_pct": min(returns),
        "time_to_mfe_seconds": (stamps[returns.index(max(returns))] - at).total_seconds(),
        "time_to_mae_seconds": (stamps[returns.index(min(returns))] - at).total_seconds(),
        "breakout_lead_seconds": (threshold_hit - at).total_seconds() if threshold_hit else None,
        "label_available_at": end.available_at.isoformat(),
        "end_snapshot_id": end.id,
    }


def ranking_report(store, now, settings):
    daily = {}
    for at, rows in archived_batches(store, now):
        # One predeclared first batch per UTC day, not whichever batch performed best.
        daily.setdefault(at.date().isoformat(), (at, rows))
    result = []
    for horizon in RANK_HORIZONS:
        for topk in (5, 10, 20):
            hits, measured, pending, missing, days = 0, [], 0, 0, 0
            for at, rows in daily.values():
                selected = rows[:topk]
                labels = [forward_label(store, row, at, horizon, now, settings) for row in selected]
                pending += sum(label["status"] == "PENDING" for label in labels)
                missing += sum(label["status"] not in {"MEASURED", "PENDING"} for label in labels)
                complete = len(selected) == topk and all(
                    label["status"] == "MEASURED" for label in labels
                )
                if not complete:
                    continue
                days += 1
                measured += [label["return_pct"] for label in labels]
                hits += sum(
                    label["return_pct"] >= settings.ranking_hit_threshold_pct for label in labels
                )
            result.append(
                {
                    "horizon_seconds": horizon,
                    "k": topk,
                    "complete_days": days,
                    "measured_assets": len(measured),
                    "pending_assets": pending,
                    "missing_or_gapped_assets": missing,
                    "precision_at_k": hits / len(measured) if measured else None,
                    "mean_return_pct": statistics.mean(measured) if measured else None,
                    "hit_threshold_pct": settings.ranking_hit_threshold_pct,
                }
            )
    signals = store.signals(limit=100000, as_of=now)
    weekly = defaultdict(list)
    for signal in signals:
        if signal.kind not in {SignalKind.WATCH, SignalKind.ENTRY_CANDIDATE}:
            continue
        label = forward_label(
            store,
            {"snapshot_id": signal.snapshot_id, "asset_id": signal.asset_id},
            signal.emitted_at,
            signal.horizon_seconds,
            now,
            settings,
        )
        week = signal.emitted_at.strftime("%G-W%V")
        weekly[(week, signal.rule_id)].append(label)
    false_positives = []
    for (week, rule), labels in weekly.items():
        covered = [label for label in labels if label["status"] == "MEASURED"]
        false_positives.append(
            {
                "week": week,
                "rule": rule,
                "eligible": len(labels),
                "measured": len(covered),
                "missing": len(labels) - len(covered),
                "false_positive_rate": statistics.mean(
                    label["mfe_pct"] < settings.breakout_threshold_pct for label in covered
                )
                if covered
                else None,
                "median_lead_seconds": statistics.median(
                    label["breakout_lead_seconds"]
                    for label in covered
                    if label["breakout_lead_seconds"] is not None
                )
                if any(label["breakout_lead_seconds"] is not None for label in covered)
                else None,
            }
        )
    return {
        "mode": "ARCHIVED_POINT_IN_TIME",
        "ranking": result,
        "weekly_false_positives": false_positives,
        "daily_sampling": "FIRST_BATCH_UTC",
        "cost_scenarios_bps": [
            2 * (settings.fee_bps_each_way + settings.slippage_bps_each_way),
            4 * (settings.fee_bps_each_way + settings.slippage_bps_each_way),
        ],
        "limitations": [
            "采样 MFE 不代表可成交收益",
            "缺失未来数据保留，不冒充零收益",
            "完整 TopK 组才进入 Precision 分母",
            "重复提醒相关；报告不证明可盈利",
        ],
    }


def sigmoid(value):
    return 1 / (1 + math.exp(-max(-40, min(40, value))))


def fit_logistic(x, y, epochs=300, learning_rate=0.05, l2=0.01):
    width = len(x[0])
    weights = [0.0] * (width + 1)
    for _ in range(epochs):
        grad = [0.0] * (width + 1)
        for row, label in zip(x, y, strict=True):
            error = (
                sigmoid(weights[0] + sum(w * v for w, v in zip(weights[1:], row, strict=True)))
                - label
            )
            grad[0] += error
            for j, v in enumerate(row, 1):
                grad[j] += error * v
        weights = [
            w - learning_rate * (g / len(x) + (l2 * w if j else 0))
            for j, (w, g) in enumerate(zip(weights, grad, strict=True))
        ]
    return weights


def predict(weights, x):
    return [
        sigmoid(weights[0] + sum(w * v for w, v in zip(weights[1:], row, strict=True))) for row in x
    ]


def brier(y, probabilities):
    return statistics.mean((label - p) ** 2 for label, p in zip(y, probabilities, strict=True))


def calibration(y, predictions):
    rows = []
    for low in (0, 0.2, 0.4, 0.6, 0.8):
        items = [
            (v, p)
            for v, p in zip(y, predictions, strict=True)
            if low <= p < low + 0.2 or low == 0.8 and p == 1
        ]
        rows.append(
            {
                "probability_bin": [low, low + 0.2],
                "samples": len(items),
                "mean_prediction": statistics.mean(p for _, p in items) if items else None,
                "actual_hit_rate": statistics.mean(v for v, _ in items) if items else None,
            }
        )
    return rows


def train_model(store, now, settings, horizon=14400):
    if horizon not in RANK_HORIZONS:
        raise ValueError("Unsupported horizon")
    # Labels must mature before a later partition starts; this purges overlapping windows.
    samples = []
    for at, rows in archived_batches(store, now):
        labels = [(row, forward_label(store, row, at, horizon, now, settings)) for row in rows]
        measured = [(r, label) for r, label in labels if label["status"] == "MEASURED"]
        if len(measured) != len(rows) or not rows:
            continue
        threshold = sorted(label["return_pct"] for _, label in measured)[
            max(0, math.ceil(len(rows) * 0.9) - 1)
        ]
        for row, label in measured:
            snapshot = store.snapshot(row["snapshot_id"])
            values = [getattr(snapshot.features, f) for f in FEATURES]
            if any(v is None for v in values) or not row["data_ready"]:
                continue
            samples.append(
                {
                    "at": at,
                    "label_at": datetime.fromisoformat(label["label_available_at"]),
                    "snapshot_id": snapshot.id,
                    "x": values,
                    "y": int(label["return_pct"] >= threshold),
                    "return_pct": label["return_pct"],
                }
            )
    timestamps = sorted({s["at"] for s in samples})
    if len(samples) < settings.ml_min_samples or len(timestamps) < 10:
        return {
            "status": "INSUFFICIENT_HISTORY",
            "samples": len(samples),
            "required_samples": settings.ml_min_samples,
            "decision_batches": len(timestamps),
            "required_batches": 10,
            "production_promotion": False,
        }
    validation_start = timestamps[int(len(timestamps) * 0.6)]
    test_start = timestamps[int(len(timestamps) * 0.8)]
    train = [s for s in samples if s["label_at"] < validation_start]
    valid = [s for s in samples if validation_start <= s["at"] and s["label_at"] < test_start]
    test = [s for s in samples if s["at"] >= test_start]
    if min(len(train), len(valid), len(test)) < 10 or len({s["y"] for s in train}) < 2:
        return {"status": "INSUFFICIENT_PURGED_PARTITIONS", "production_promotion": False}
    means = [statistics.mean(s["x"][j] for s in train) for j in range(len(FEATURES))]
    scales = [statistics.pstdev(s["x"][j] for s in train) or 1 for j in range(len(FEATURES))]

    def scaled(partition):
        return [
            [(v - m) / sd for v, m, sd in zip(s["x"], means, scales, strict=True)]
            for s in partition
        ]

    xtrain, xvalid, xtest = scaled(train), scaled(valid), scaled(test)
    ytrain, yvalid, ytest = [[s["y"] for s in partition] for partition in (train, valid, test)]
    candidates = []
    for l2 in (0.001, 0.01, 0.1):
        weights = fit_logistic(xtrain, ytrain, l2=l2)
        candidates.append((brier(yvalid, predict(weights, xvalid)), l2, weights))
    _, l2, weights = min(candidates, key=lambda c: c[0])
    predictions = predict(weights, xtest)
    base_brier = brier(ytest, predictions)
    importance = {}
    rng = random.Random(42)
    for j, name in enumerate(FEATURES):
        xcopy = [row[:] for row in xtest]
        column = [row[j] for row in xcopy]
        rng.shuffle(column)
        for row, value in zip(xcopy, column, strict=True):
            row[j] = value
        importance[name] = brier(ytest, predict(weights, xcopy)) - base_brier
    positive_weights = [abs(w) for w in weights[1:]]
    denom = sum(positive_weights)
    proposed = {
        name: w / denom * 100 if denom else 0
        for name, w in zip(FEATURES, positive_weights, strict=True)
    }
    payload = {
        "status": "EXPERIMENTAL",
        "model_type": "L2_LOGISTIC_TOP_DECILE_RANKER",
        "target": "FUTURE_TOP_10_PERCENT_CROSS_SECTION_RETURN",
        "horizon_seconds": horizon,
        "features": FEATURES,
        "weights": weights,
        "means": means,
        "scales": scales,
        "samples": {"train": len(train), "validation": len(valid), "test": len(test)},
        "partition_boundaries": {
            "validation": validation_start.isoformat(),
            "test": test_start.isoformat(),
        },
        "test_brier_score": base_brier,
        "constant_baseline_brier": brier(ytest, [statistics.mean(ytrain)] * len(ytest)),
        "permutation_importance_brier_increase": importance,
        "calibration": calibration(ytest, predictions),
        "proposed_weights": proposed,
        "selected_l2": l2,
        "training_snapshot_ids": [s["snapshot_id"] for s in train],
        "test_snapshot_ids": [s["snapshot_id"] for s in test],
        "production_promotion": False,
        "limitations": [
            "单次按时间分割，不代表跨周期稳健性",
            "未接入 LightGBM/XGBoost/CatBoost",
            "权重建议不会自动写入规则",
            "样本有相关性；历史缺失影响覆盖",
        ],
    }
    saved = IntelligenceStore(store).save(
        EvidenceRecord(
            kind="model",
            key=digest(payload),
            source="ARCHIVED_EXPERIMENT",
            market_time=now,
            available_at=now,
            data=payload,
        )
    )
    return {"model_id": saved.id, **payload}


def paid_evaluation(store, item, now):
    required = {
        "provider",
        "baseline_dataset_hash",
        "candidate_dataset_hash",
        "start",
        "end",
        "out_of_sample",
        "samples",
        "cost_usd_month",
        "baseline",
        "candidate",
    }
    if set(item) != required or not item["out_of_sample"] or item["samples"] < 100:
        raise ValueError("付费数据评估需要完整匹配的样本外数据与至少 100 样本")
    metrics = {"precision", "recall", "lead_time_seconds", "risk_recall", "false_positive_rate"}
    for side in ("baseline", "candidate"):
        if set(item[side]) != metrics or any(
            not isinstance(v, (int, float)) or not math.isfinite(v) for v in item[side].values()
        ):
            raise ValueError("评估指标不完整或含非有限值")
    start, end = datetime.fromisoformat(item["start"]), datetime.fromisoformat(item["end"])
    if not start.tzinfo or not end.tzinfo or not start < end <= now:
        raise ValueError("评估窗口必须是已结束的时区明确区间")
    deltas = {k: item["candidate"][k] - item["baseline"][k] for k in metrics}
    payload = {
        **item,
        "metric_deltas": deltas,
        "status": "REVIEW_REQUIRED",
        "purchase_authorized": False,
        "limitations": ["导入的评估结果仍需对照原始数据复核；未购买或启用任何付费 API"],
    }
    return IntelligenceStore(store).save(
        EvidenceRecord(
            kind="paid_evaluation",
            key=digest(payload),
            source="LOCAL_EVALUATION",
            market_time=end,
            available_at=now,
            data=payload,
        )
    )
