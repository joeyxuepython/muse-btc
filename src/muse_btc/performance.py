"""Descriptive forward-label evaluation, never a trading PnL backtest."""

import math
from collections import defaultdict
from datetime import timedelta
from statistics import mean, median

from .models import Module, SignalKind


def wilson(wins, count):
    if not count:
        return None
    z = 1.959963984540054
    p = wins / count
    denominator = 1 + z * z / count
    middle = (p + z * z / (2 * count)) / denominator
    margin = z * math.sqrt(p * (1 - p) / count + z * z / (4 * count * count)) / denominator
    return [max(0, middle - margin), min(1, middle + margin)]


def summary(samples, kind):
    net = [o.paper_net_return_pct for o in samples if o.paper_net_return_pct is not None]
    excess = [o.excess_return_pct for o in samples if o.excess_return_pct is not None]
    entry = kind == SignalKind.ENTRY_CANDIDATE
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
            o.return_pct - 2 * o.round_trip_cost_bps / 100 for o in samples
        )
        if entry and samples
        else None,
        "down_move_rate": mean(o.return_pct < 0 for o in samples)
        if kind == SignalKind.RISK and samples
        else None,
    }


def build_report(signals, outcomes, horizons, settings, now):
    signals = [s for s in signals if s.emitted_at <= now]
    measured = {
        (o.signal_id, o.horizon_seconds): o
        for o in outcomes
        if o.evaluated_at <= now and o.measured_at <= now
    }
    groups = defaultdict(list)
    for s in signals:
        if s.module != Module.MEME and s.kind != SignalKind.INVALIDATED:
            groups[(s.rule_id, s.rule_version, s.kind, s.module)].append(s)
    rows = []
    for (rule, version, kind, module), candidates in sorted(groups.items()):
        candidates.sort(key=lambda s: (s.emitted_at, s.id))
        for horizon in horizons:
            matured = [s for s in candidates if s.emitted_at + timedelta(seconds=horizon) <= now]
            samples = [measured[(s.id, horizon)] for s in matured if (s.id, horizon) in measured]
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
            rows.append(
                {
                    "rule_id": rule,
                    "rule_version": version,
                    "signal_kind": kind,
                    "module": module,
                    "horizon_seconds": horizon,
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
                    **summary(usable, kind),
                    "market_confirmation_cohorts": {
                        k: summary(v, kind) for k, v in sorted(cohorts.items())
                    },
                    "chronological_diagnostic": {
                        "cutoff": cutoff.isoformat() if cutoff else None,
                        "earlier": summary(earlier, kind),
                        "later": summary(later, kind),
                        "purged_count": len(usable) - len(earlier) - len(later) if cutoff else 0,
                        "is_untouched_out_of_sample": False,
                    },
                }
            )
    return {
        "version": "validation-v2",
        "as_of": now.isoformat(),
        "validation_status": "OBSERVATION_ONLY",
        "production_promotion": False,
        "signal_count": len(signals),
        "outcome_count": len(measured),
        "horizons_seconds": horizons,
        "rules": rows,
        "limitations": [
            "按规则版本、信号类型、模块分组；WATCH/RISK 不计作入场胜率或做空收益",
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
