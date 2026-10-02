from datetime import datetime, timedelta

from .config import Settings
from .models import Module, Regime, Signal, SignalKind, Snapshot

RULE_VERSION = "rules-v1"


def usable(snapshot: Snapshot, now: datetime, settings: Settings) -> bool:
    blocking = {
        "CANDLE_GAPS",
        "DUPLICATE_CANDLES",
        "INVALID_TAKER_VOLUME",
        "INSUFFICIENT_CANDLE_HISTORY",
        "FUTURE_MARKET_TIMESTAMP",
    }
    return (
        snapshot.available_at <= now
        and snapshot.market_time <= now
        and (now - snapshot.available_at).total_seconds() <= settings.stale_seconds
        and (now - snapshot.market_time).total_seconds() <= settings.stale_seconds
        and not blocking.intersection(snapshot.quality_issues)
    )


def market_regime(btc: Snapshot | None, now: datetime, settings: Settings) -> Regime:
    result = Regime(as_of=now)
    if not btc or not usable(btc, now, settings):
        result.evidence = ["BTC 数据缺失、过期或质量检查未通过"]
        return result
    result.btc_snapshot_id = btc.id
    f = btc.features
    if f.ema20 is not None and f.ema50 is not None:
        result.btc_structure = "UPTREND" if f.ema20 > f.ema50 else "DOWNTREND"
    if f.return_15m_pct is None or f.spot_taker_buy_ratio is None:
        result.evidence = ["缺少 BTC 价格或现货成交方向数据"]
        return result
    if f.return_15m_pct <= -1.5 and f.spot_taker_buy_ratio < 0.45:
        result.risk_mode = "RISK_OFF"
        result.evidence = [
            f"BTC 15 分钟变化 {f.return_15m_pct:.2f}%",
            f"现货主动买入占比 {f.spot_taker_buy_ratio:.0%}",
        ]
    elif (
        f.funding_rate_pct is not None
        and f.oi_change_5m_pct is not None
        and f.funding_rate_pct >= 0.05
        and f.oi_change_5m_pct >= 2
    ):
        result.risk_mode = "LEVERAGE_OVERHEAT"
        result.evidence = [
            f"Funding {f.funding_rate_pct:.3f}%",
            f"OI 5 分钟变化 {f.oi_change_5m_pct:.2f}%",
        ]
    elif f.funding_rate_pct is None or f.oi_change_5m_pct is None:
        result.risk_mode = "CAUTION"
        result.evidence = ["现货数据可用，但衍生品风险无法完整核实"]
    else:
        result.risk_mode = "NORMAL"
        result.evidence = ["当前规则未触发 BTC 急跌或杠杆过热条件"]
    result.contradictions = ["尚未接入宏观、稳定币资金流和全市场市值数据"]
    return result


def _signal(
    snapshot: Snapshot,
    regime: Regime,
    now: datetime,
    kind: SignalKind,
    rule_id: str,
    title: str,
    evidence: list[str],
    groups: list[str],
    score: float,
    contradictions: list[str] | None = None,
) -> Signal:
    horizon = 900 if snapshot.module == Module.MEME else 3600
    stop_distance = max(
        (snapshot.features.atr14 or snapshot.price * 0.01) * 2, snapshot.price * 0.005
    )
    # Guard absurd ATR values while retaining the recorded price and raw evidence.
    stop_price = max(snapshot.price * 0.01, snapshot.price - stop_distance)
    opportunity = kind in (SignalKind.WATCH, SignalKind.ENTRY_CANDIDATE)
    return Signal(
        asset_id=snapshot.asset_id,
        symbol=snapshot.symbol,
        module=snapshot.module,
        kind=kind,
        rule_id=rule_id,
        emitted_at=now,
        snapshot_id=snapshot.id,
        btc_snapshot_id=regime.btc_snapshot_id,
        title=title,
        evidence=evidence,
        contradictions=contradictions or [],
        evidence_groups=groups,
        evidence_score=score,
        reference_price=snapshot.price,
        entry_zone=[snapshot.price * 0.998, snapshot.price * 1.002]
        if kind == SignalKind.ENTRY_CANDIDATE
        else None,
        invalidation_price=stop_price if opportunity else None,
        invalidation_conditions=[
            "价格跌破失效参考位",
            "BTC 风险升至 RISK_OFF 或杠杆过热",
            "数据过期时暂停使用本信号",
        ]
        if opportunity
        else ["风险条件解除"],
        expires_at=now + timedelta(seconds=horizon * 4),
        horizon_seconds=horizon,
    )


def evaluate(snapshot: Snapshot, regime: Regime, now: datetime, settings: Settings) -> list[Signal]:
    if not usable(snapshot, now, settings):
        return []
    if snapshot.module == Module.MEME:
        return _meme(snapshot, regime, now)
    f = snapshot.features
    result: list[Signal] = []
    if (
        f.funding_rate_pct is not None
        and f.funding_rate_pct >= 0.05
        and f.oi_change_5m_pct is not None
        and f.oi_change_5m_pct >= 2
    ):
        result.append(
            _signal(
                snapshot,
                regime,
                now,
                SignalKind.RISK,
                "leverage-overheat",
                "杠杆过热：关注减仓与退出风险",
                [f"Funding {f.funding_rate_pct:.3f}%", f"OI 5 分钟增加 {f.oi_change_5m_pct:.2f}%"],
                ["derivatives"],
                75,
                ["OI 同时包含多空双方，不能单独证明多头加仓"],
            )
        )
    if f.return_15m_pct is not None and f.return_15m_pct <= -1.5:
        if f.spot_taker_buy_ratio is not None and f.spot_taker_buy_ratio < 0.45:
            result.append(
                _signal(
                    snapshot,
                    regime,
                    now,
                    SignalKind.RISK,
                    "spot-sell-pressure",
                    "价格走弱与现货卖压共振",
                    [
                        f"15 分钟变化 {f.return_15m_pct:.2f}%",
                        f"现货主动买入占比 {f.spot_taker_buy_ratio:.0%}",
                    ],
                    ["price", "spot_flow"],
                    70,
                )
            )
    if (
        f.return_5m_pct is not None
        and f.return_5m_pct >= 0.5
        and f.oi_change_5m_pct is not None
        and f.oi_change_5m_pct <= -1
        and f.funding_rate_pct is not None
        and f.funding_rate_pct < 0
    ):
        result.append(
            _signal(
                snapshot,
                regime,
                now,
                SignalKind.WATCH,
                "squeeze-candidate",
                "潜在逼空结构：进入观察",
                ["价格上涨、OI 下降、Funding 为负", f"5 分钟涨幅 {f.return_5m_pct:.2f}%"],
                ["price", "derivatives"],
                60,
                ["缺少完整清算数据，尚不能确认逼空"],
            )
        )
    momentum = (
        f.return_5m_pct is not None
        and f.return_5m_pct >= 0.3
        and f.relative_volume is not None
        and f.relative_volume >= 1.8
    )
    demand = f.spot_taker_buy_ratio is not None and f.spot_taker_buy_ratio >= 0.58
    trend = f.ema20 is not None and f.ema50 is not None and f.ema20 > f.ema50
    relative = snapshot.module == Module.BTC or (
        f.relative_strength_15m_pct is not None and f.relative_strength_15m_pct >= 0.3
    )
    cool_derivatives = (
        f.funding_rate_pct is not None
        and f.funding_rate_pct < 0.03
        and f.oi_change_5m_pct is not None
        and f.oi_change_5m_pct < 2
    )
    liquidity = f.spread_bps is not None and f.spread_bps < 15
    if momentum and demand and trend and relative:
        safe_regime = regime.risk_mode == "NORMAL"
        can_enter = safe_regime and cool_derivatives and liquidity and not result
        kind = SignalKind.ENTRY_CANDIDATE if can_enter else SignalKind.WATCH
        score = 78 if can_enter else 48
        contradictions = []
        if not safe_regime:
            contradictions.append(f"BTC 风险背景为 {regime.risk_mode}，暂停入场级提醒")
        if not cool_derivatives:
            contradictions.append("衍生品风险未满足低过热条件或数据不可用")
        if not liquidity:
            contradictions.append("价差偏大或盘口数据不可用")
        result.append(
            _signal(
                snapshot,
                regime,
                now,
                kind,
                "spot-led-momentum",
                "现货推动的入场候选" if can_enter else "现货动量：继续观察",
                [
                    f"5 分钟变化 {f.return_5m_pct:.2f}%",
                    f"相对成交量 {f.relative_volume:.2f} 倍",
                    f"现货主动买入占比 {f.spot_taker_buy_ratio:.0%}",
                    "EMA20 高于 EMA50",
                ],
                ["price", "spot_flow", "derivatives"] if can_enter else ["price", "spot_flow"],
                score,
                contradictions,
            )
        )
    return result


def _meme(snapshot: Snapshot, regime: Regime, now: datetime) -> list[Signal]:
    f, risk = snapshot.features, snapshot.risk
    if risk and risk.blockers:
        return [
            _signal(
                snapshot,
                regime,
                now,
                SignalKind.RISK,
                "meme-security-block",
                "代币风险阻断：暂停参与",
                risk.blockers,
                ["token_security"],
                90,
            )
        ]
    if f.liquidity_usd is not None and f.liquidity_usd < 25000:
        return [
            _signal(
                snapshot,
                regime,
                now,
                SignalKind.RISK,
                "meme-thin-liquidity",
                "池子流动性过低：退出能力风险",
                [f"公开池子流动性 ${f.liquidity_usd:,.0f}"],
                ["liquidity"],
                70,
            )
        ]
    if (
        f.liquidity_usd is None
        or f.liquidity_usd < 50000
        or f.buys_5m is None
        or f.sells_5m is None
        or f.relative_volume is None
    ):
        return []
    total = f.buys_5m + f.sells_5m
    buy_ratio = f.buys_5m / total if total else 0
    # Transaction counts do not establish independent buyers or exclude wash trading.
    if total < 30 or buy_ratio < 0.6 or f.relative_volume < 2:
        return []
    verified = bool(risk and risk.status == "SCREENED" and not risk.missing_checks)
    can_enter = (
        verified
        and regime.risk_mode == "NORMAL"
        and "QUOTE_TIME_UNVERIFIED" not in snapshot.quality_issues
    )
    notes = ["DEX 成交笔数不能证明独立买家，仍可能存在刷量"]
    if not verified:
        notes.append("关键合约、持仓或 LP 检查不完整，暂停入场级提醒")
    if regime.risk_mode != "NORMAL":
        notes.append(f"BTC 风险背景为 {regime.risk_mode}")
    if snapshot.quality_issues:
        notes.extend(snapshot.quality_issues)
    if risk:
        notes.extend(risk.warnings)
    return [
        _signal(
            snapshot,
            regime,
            now,
            SignalKind.ENTRY_CANDIDATE if can_enter else SignalKind.WATCH,
            "meme-activity-expansion",
            "Meme 活跃扩张：入场候选" if can_enter else "Meme 早期候选：等待风险核查",
            [
                f"池子流动性 ${f.liquidity_usd:,.0f}",
                f"5 分钟成交 {total} 笔，买入笔数占比 {buy_ratio:.0%}",
                f"短期相对成交量 {f.relative_volume:.2f} 倍",
            ],
            ["liquidity", "dex_activity", "token_security"]
            if verified
            else ["liquidity", "dex_activity"],
            72 if can_enter else 42,
            notes,
        )
    ]
