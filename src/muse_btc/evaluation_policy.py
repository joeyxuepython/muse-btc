"""Rule-specific, prospective measurement plans; no selection by observed returns.

These windows express the rule's intended observation timescale, not optimized
holding periods or proven exits. Five-minute checkpoints never invalidate a signal.
"""

import hashlib
import json

from .models import Module, SignalEvaluation, SignalKind

RULE_EVALUATIONS = {
    "pre-pump-fusion": SignalEvaluation(
        metric="LONG_RETURN",
        horizons_seconds=(300, 900),
        primary_horizon_seconds=900,
        rationale="5/15 分钟启动结构：15 分钟为主检验，5 分钟只作早期辅助观察",
    ),
    "spot-led-momentum": SignalEvaluation(
        metric="LONG_RETURN",
        horizons_seconds=(300, 900, 3600),
        primary_horizon_seconds=3600,
        rationale="现货成交与 EMA 趋势：1 小时为主检验，5/15 分钟观察短期路径",
    ),
    "squeeze-candidate": SignalEvaluation(
        metric="LONG_RETURN",
        horizons_seconds=(300, 900),
        primary_horizon_seconds=900,
        rationale="5 分钟价格/OI 变化的短时挤压假设：15 分钟主检验，5 分钟辅助",
    ),
    "spot-sell-pressure": SignalEvaluation(
        metric="RISK_DIRECTION",
        horizons_seconds=(900,),
        primary_horizon_seconds=900,
        rationale="15 分钟卖压告警：检查随后 15 分钟的下跌方向和采样路径，不模拟持仓",
    ),
    "leverage-overheat": SignalEvaluation(
        metric="RISK_DIRECTION",
        horizons_seconds=(900, 3600),
        primary_horizon_seconds=3600,
        rationale="Funding/OI 过热告警：1 小时主方向检查，15 分钟观察早期风险路径",
    ),
    "btc-macro-stress": SignalEvaluation(
        metric="RISK_DIRECTION",
        horizons_seconds=(86400,),
        primary_horizon_seconds=86400,
        rationale="宏观/ETF 日频风险背景：一天价格方向观察，不是买卖策略",
    ),
    "btc-mvrv-elevated": SignalEvaluation(
        metric="RISK_DIRECTION",
        horizons_seconds=(86400,),
        primary_horizon_seconds=86400,
        rationale="链上日频估值背景：保留原一天观察窗，仅描述后续风险方向",
    ),
    "btc-holder-loss": SignalEvaluation(
        metric="RISK_DIRECTION",
        horizons_seconds=(86400,),
        primary_horizon_seconds=86400,
        rationale="持有人成本/亏损日频背景：保留原一天风险观察，不推断可成交收益",
    ),
    "btc-options-volatility": SignalEvaluation(
        metric="VOLATILITY",
        horizons_seconds=(14400,),
        primary_horizon_seconds=14400,
        rationale="期权 IV 只提出波动假设：保留原 4 小时范围/绝对变化观察，不判断涨跌",
    ),
}


def declared_evaluation(rule_id):
    # Missing declarations are errors for newly defined rules, not a generic fallback.
    return RULE_EVALUATIONS[rule_id].model_copy(deep=True)


def signal_evaluation(signal):
    if signal.module == Module.MEME or signal.kind == SignalKind.INVALIDATED:
        return None
    if signal.evaluation is not None:
        if signal.kind == SignalKind.RISK and signal.evaluation.metric != "RISK_DIRECTION":
            raise ValueError("Risk alerts require risk-direction evaluation")
        if signal.kind == SignalKind.ENTRY_CANDIDATE and signal.evaluation.metric != "LONG_RETURN":
            raise ValueError("Entry candidates require long-return evaluation")
        return signal.evaluation
    # Historical signals did declare an observation window, but not a holding plan.
    # Honor only that archived window; never retrofit today's rule periods into them.
    return SignalEvaluation(
        version="legacy-observation-v0",
        metric="RISK_DIRECTION" if signal.kind == SignalKind.RISK else "LONG_RETURN",
        horizons_seconds=(signal.horizon_seconds,),
        primary_horizon_seconds=signal.horizon_seconds,
        rationale="旧记录仅有观察窗，非提前声明持有期；只继续原观察窗，旧九期限结果另存历史",
    )


def evaluation_id(plan):
    return hashlib.sha256(
        json.dumps(plan.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()[:12]
