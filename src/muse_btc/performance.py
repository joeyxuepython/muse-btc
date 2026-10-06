"""Descriptive forward-label evaluation, never a trading PnL backtest."""

import math
from collections import defaultdict
from datetime import timedelta
from statistics import mean, median

from .evaluation_policy import evaluation_id, signal_evaluation
from .models import SignalKind


def wilson(wins, count):
    if not count:
        return None
    z = 1.959963984540054
    p = wins / count
    denominator = 1 + z * z / count
    middle = (p + z * z / (2 * count)) / denominator
    margin = z * math.sqrt(p * (1 - p) / count + z * z / (4 * count * count)) / denominator
    return [max(0, middle - margin), min(1, middle + margin)]


def summary(samples, kind, metric="LONG_RETURN"):
    net = [o.paper_net_return_pct for o in samples if o.paper_net_return_pct is not None]
    excess = [o.excess_return_pct for o in samples if o.excess_return_pct is not None]
    entry = kind == SignalKind.ENTRY_CANDIDATE and metric == "LONG_RETURN"
    risk = metric == "RISK_DIRECTION"
    volatility = metric == "VOLATILITY"
    terminal_down = sum(o.return_pct < 0 for o in samples)
    priced = [o for o in samples if o.round_trip_cost_bps is not None]
    return {
        "sample_count": len(samples),
        "mean_return_pct": mean(o.return_pct for o in samples) if samples else None,
        "mean_max_adverse_pct": mean(o.max_adverse_pct for o in samples) if samples else None,
        "mean_excess_return_pct": mean(excess) if excess else None,
        "benchmark_covered_count": len(excess),
        "mean_paper_net_return_pct": mean(net) if entry and net else None,
        "paper_net_count": len(net) if entry else 0,
        "median_paper_net_return_pct": median(net) if entry and net else None,
        "paper_positive_rate": mean(v > 0 for v in net) if entry and net else None,
        "paper_positive_rate_wilson_95": wilson(sum(v > 0 for v in net), len(net))
        if entry
        else None,
        "mean_double_cost_return_pct": mean(
            o.return_pct - 2 * o.round_trip_cost_bps / 100 for o in priced
        )
        if entry and priced
        else None,
        "down_move_rate": mean(o.return_pct < 0 for o in samples) if risk and samples else None,
        "risk_terminal_decline_rate": terminal_down / len(samples) if risk and samples else None,
        "risk_terminal_decline_rate_wilson_95": wilson(terminal_down, len(samples))
        if risk
        else None,
        "risk_window_decline_rate": mean(o.max_adverse_pct < 0 for o in samples)
        if risk and samples
        else None,
        "risk_mean_max_decline_pct": mean(max(0, -o.max_adverse_pct) for o in samples)
        if risk and samples
        else None,
        "risk_mean_max_rebound_pct": mean(o.max_favorable_pct for o in samples)
        if risk and samples
        else None,
        "mean_observed_range_pct": mean(o.max_favorable_pct - o.max_adverse_pct for o in samples)
        if volatility and samples
        else None,
        "mean_absolute_end_change_pct": mean(abs(o.return_pct) for o in samples)
        if volatility and samples
        else None,
    }


def build_report(signals, outcomes, settings, now):
    signals = [s for s in signals if s.emitted_at <= now]
    measured = {
        (o.signal_id, o.horizon_seconds): o
        for o in outcomes
        if o.evaluated_at <= now and o.measured_at <= now
    }
    groups = defaultdict(list)
    plans = {}
    for s in signals:
        plan = signal_evaluation(s)
        if plan:
            key = evaluation_id(plan)
            plans[key] = plan
            groups[(s.rule_id, s.rule_version, s.kind, s.module, key, s.evaluation is None)].append(
                s
            )
    rows, legacy_rows = [], []
    ignored = 0
    for (rule, version, kind, module, policy_id, legacy), candidates in sorted(groups.items()):
        plan = plans[policy_id]
        ids = {s.id for s in candidates}
        horizons = (
            sorted(set(plan.horizons_seconds) | {h for s, h in measured if s in ids})
            if legacy
            else plan.horizons_seconds
        )
        ignored += (
            sum(
                s in ids
                and (
                    h not in horizons
                    or o.evaluation_policy_id != policy_id
                    or o.evaluation_metric != plan.metric
                )
                for (s, h), o in measured.items()
            )
            if not legacy
            else 0
        )
        candidates.sort(key=lambda s: (s.emitted_at, s.id))
        for horizon in horizons:
            matured = [s for s in candidates if s.emitted_at + timedelta(seconds=horizon) <= now]
            samples = [
                measured[(s.id, horizon)]
                for s in matured
                if (s.id, horizon) in measured
                and (
                    legacy
                    or (
                        measured[(s.id, horizon)].evaluation_policy_id == policy_id
                        and measured[(s.id, horizon)].evaluation_metric == plan.metric
                    )
                )
            ]
            covered = {
                o.signal_id: o
                for o in samples
                if o.max_observation_gap_seconds
                <= (
                    o.max_allowed_gap_seconds
                    if o.max_allowed_gap_seconds is not None
                    else settings.stale_seconds
                )
            }
            # Select by signal time before consulting returns. Missing labels occupy
            # their window too, so a later winning duplicate cannot replace a failure.
            until, selected = {}, []
            for s in matured:
                if s.asset_id in until and s.emitted_at < until[s.asset_id]:
                    continue
                sample = covered.get(s.id)
                until[s.asset_id] = max(
                    s.emitted_at + timedelta(seconds=horizon),
                    sample.measured_at if sample else s.emitted_at,
                )
                selected.append(s)
            usable = [covered[s.id] for s in selected if s.id in covered]
            cohorts = defaultdict(list)
            for s in selected:
                if s.id not in covered:
                    continue
                context = s.decision.get("market_confirmation", {})
                cohort = (
                    "NOT_RECORDED"
                    if not context
                    else "UNKNOWN"
                    if context.get("status") == "UNKNOWN"
                    else "RESONANT"
                    if context.get("resonance")
                    else "NON_RESONANT"
                )
                cohorts[cohort].append(covered[s.id])
            # A chronological diagnostic, not a claim that thresholds were selected
            # without seeing this data. Purge labels overlapping the split boundary.
            cutoff = matured[int(len(matured) * 0.8)].emitted_at if len(matured) >= 5 else None
            earlier, later = [], []
            if cutoff:
                earlier = [
                    covered[s.id]
                    for s in selected
                    if s.id in covered
                    and (covered[s.id].label_available_at or covered[s.id].evaluated_at) < cutoff
                ]
                later = [
                    covered[s.id] for s in selected if s.id in covered and s.emitted_at >= cutoff
                ]
            (legacy_rows if legacy else rows).append(
                {
                    "rule_id": rule,
                    "rule_version": version,
                    "signal_kind": kind,
                    "module": module,
                    "horizon_seconds": horizon,
                    "evaluation_policy": plan.model_dump(mode="json"),
                    "evaluation_policy_id": policy_id,
                    "evaluation_metric": plan.metric,
                    "horizon_role": "LEGACY_OBSERVATION"
                    if legacy
                    else "PRIMARY"
                    if horizon == plan.primary_horizon_seconds
                    else "AUXILIARY",
                    "candidate_count": len(candidates),
                    "matured_count": len(matured),
                    "pending_count": len(candidates) - len(matured),
                    "measured_count": len(samples),
                    "missing_outcome_count": len(matured) - len(samples),
                    "gapped_count": len(samples) - len(covered),
                    "covered_count": len(covered),
                    "overlapping_count": len(matured) - len(selected),
                    "nonoverlapping_count": len(usable),
                    "selected_missing_or_gapped_count": len(selected) - len(usable),
                    "evaluation_status": "INSUFFICIENT_SAMPLES"
                    if len(usable) < settings.validation_min_samples
                    else "DESCRIPTIVE_ONLY",
                    "required_samples": settings.validation_min_samples,
                    **summary(usable, kind, plan.metric),
                    "market_confirmation_cohorts": {
                        k: summary(v, kind, plan.metric) for k, v in sorted(cohorts.items())
                    },
                    "chronological_diagnostic": {
                        "cutoff": cutoff.isoformat() if cutoff else None,
                        "earlier": summary(earlier, kind, plan.metric),
                        "later": summary(later, kind, plan.metric),
                        "purged_count": len(usable) - len(earlier) - len(later) if cutoff else 0,
                        "is_untouched_out_of_sample": False,
                    },
                }
            )
    return {
        "version": "validation-v3",
        "as_of": now.isoformat(),
        "validation_status": "OBSERVATION_ONLY",
        "production_promotion": False,
        "signal_count": len(signals),
        "outcome_count": len(measured),
        "horizons_seconds": sorted({r["horizon_seconds"] for r in rows}),
        "rules": rows,
        "legacy_rules": legacy_rows,
        "legacy_signal_count": sum(s.evaluation is None for s in signals),
        "out_of_plan_outcome_count": ignored,
        "limitations": [
            "按规则版本、信号类型、模块分组；WATCH/RISK 不计作入场胜率或做空收益",
            "期限和主指标在信号生成时声明；辅助期限不能事后替代主期限或使信号失效",
            "风险指标为终点/采样窗口是否下跌及跌幅，不等于做空收益或告警增量价值",
            "期权波动背景仅记录采样范围/绝对变化，不推断方向或 IV 预测已经有效",
            "旧记录没有完整评估声明：legacy_rules 保留旧口径，不与新规则混算",
            "固定期限价格标签不是带入场成交、止损和退出规则的交易回测",
            "评估对象为归档规则信号，可能未送达；不是用户邮件或实际持仓的收益统计",
            "同币同规则重叠窗口按最早信号去重；跨币和跨规则仍可能相关",
            "MFE/MAE 仅为采样路径，可能遗漏盘中极值；没有复原历史盘口",
            "纸面成本为固定假设；同时给出双倍成本压力结果，不代表实际成交",
            "Wilson 区间仅作描述，未校正跨币相关性；满足样本数不等于有效策略",
            "共振分组与时间分割仅作诊断，不能证明因果改善或未使用过的样本外收益",
            "缺失未来数据单独报告，不以零收益或成功代替；不自动调整规则或晋级",
        ],
    }
