from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


class Record(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, validate_assignment=True)


class Module(StrEnum):
    BTC = "BTC"
    ETH = "ETH"
    ALT = "ALT"
    MEME = "MEME"


class ProviderState(StrEnum):
    READY = "READY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    DISABLED = "DISABLED"
    NEEDS_VERIFICATION = "NEEDS_VERIFICATION"


class ProviderStatus(Record):
    name: str
    state: ProviderState
    checked_at: AwareDatetime = Field(default_factory=utc_now)
    message: str
    coverage: str


class Candle(Record):
    open_time: AwareDatetime
    close_time: AwareDatetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)
    quote_volume: float = Field(ge=0)
    taker_buy_quote_volume: float = Field(ge=0)


class Features(Record):
    return_5m_pct: float | None = None
    return_15m_pct: float | None = None
    return_1h_pct: float | None = None
    relative_volume: float | None = None
    volume_acceleration: float | None = None
    ema20: float | None = None
    ema50: float | None = None
    atr14: float | None = None
    rsi14: float | None = None
    realized_volatility_pct: float | None = None
    spot_taker_buy_ratio: float | None = Field(default=None, ge=0, le=1)
    perp_taker_buy_ratio: float | None = Field(default=None, ge=0, le=1)
    spot_cvd_window: float | None = None
    perp_cvd_window: float | None = None
    spread_bps: float | None = Field(default=None, ge=0)
    bid_depth_1pct_usd: float | None = None
    ask_depth_1pct_usd: float | None = None
    depth_imbalance: float | None = None
    funding_rate_pct: float | None = None
    basis_pct: float | None = None
    oi_change_5m_pct: float | None = None
    oi_zscore: float | None = None
    relative_strength_15m_pct: float | None = None
    liquidity_usd: float | None = None
    volume_1h_usd: float | None = None
    buys_5m: int | None = None
    sells_5m: int | None = None
    pool_age_hours: float | None = None
    relative_strength_eth_15m_pct: float | None = None
    oi_contracts: float | None = None
    oi_usd: float | None = None
    oi_velocity_pct_per_minute: float | None = None
    oi_acceleration_pct: float | None = None
    oi_percentile: float | None = None
    funding_mean_pct: float | None = None
    funding_zscore: float | None = None
    funding_percentile: float | None = None
    funding_trend_pct: float | None = None
    mark_price: float | None = None
    index_price: float | None = None
    cross_venue_premium_pct: float | None = None
    perp_taker_delta_usd: float | None = None
    long_short_account_ratio: float | None = None
    elite_account_ratio: float | None = None
    elite_position_ratio: float | None = None
    spot_sample_cvd_usdt: float | None = None
    perp_sample_cvd_usdt: float | None = None
    perp_spread_bps: float | None = None
    perp_bid_depth_1pct_usdt: float | None = None
    perp_ask_depth_1pct_usdt: float | None = None
    spot_perp_structure: str | None = None
    deleveraging_signal: bool | None = None


class TokenRisk(Record):
    status: str = "NEEDS_VERIFICATION"
    blockers: list[str] = []
    warnings: list[str] = []
    missing_checks: list[str] = []
    checks: dict[str, str] = {}
    top10_holder_pct: float | None = None
    # SCREENED means public checks passed, not a guarantee of token safety.


class Snapshot(Record):
    id: str = Field(default_factory=new_id)
    asset_id: str
    symbol: str
    module: Module
    source: str
    chain: str | None = None
    address: str | None = None
    pair_address: str | None = None
    market_time: AwareDatetime
    available_at: AwareDatetime
    decision_at: AwareDatetime | None = None
    price: float = Field(gt=0)
    quote_volume_24h: float | None = Field(default=None, ge=0)
    features: Features = Field(default_factory=Features)
    risk: TokenRisk | None = None
    raw_ids: list[str] = []
    quality_issues: list[str] = []
    candles: list[Candle] = []
    detail_updated_at: AwareDatetime | None = None
    canonical_asset_id: str | None = None
    component_times: dict[str, AwareDatetime] = {}
    component_received_at: dict[str, AwareDatetime] = {}
    missing_metrics: list[str] = []
    tier: str | None = None
    feature_version: str = "features-v1"


class Regime(Record):
    as_of: AwareDatetime
    risk_mode: str = "UNKNOWN"
    btc_structure: str = "UNKNOWN"
    rotation: str = "UNKNOWN"
    macro: str = "UNAVAILABLE"
    scope: str = "BINANCE_BTC"
    evidence: list[str] = []
    contradictions: list[str] = []
    btc_snapshot_id: str | None = None


class SignalKind(StrEnum):
    WATCH = "WATCH"
    ENTRY_CANDIDATE = "ENTRY_CANDIDATE"
    RISK = "RISK"
    INVALIDATED = "INVALIDATED"


class Signal(Record):
    id: str = Field(default_factory=new_id)
    asset_id: str
    symbol: str
    module: Module
    kind: SignalKind
    rule_id: str
    rule_version: str = "rules-v1"
    emitted_at: AwareDatetime
    snapshot_id: str
    btc_snapshot_id: str | None = None
    title: str
    evidence: list[str]
    contradictions: list[str] = []
    evidence_groups: list[str]
    evidence_score: float = Field(ge=0, le=100)
    validation_status: str = "OBSERVATION_ONLY"
    reference_price: float = Field(gt=0)
    entry_zone: list[float] | None = None
    invalidation_price: float | None = None
    invalidation_conditions: list[str] = []
    expires_at: AwareDatetime
    horizon_seconds: int
    parent_signal_id: str | None = None
    feature_version: str = "features-v1"
    signal_version: str = "signals-v1"
    model_version: str = "rules-only"


class SignalEvent(Record):
    id: str = Field(default_factory=new_id)
    signal_id: str
    state: str
    event_at: AwareDatetime
    reason: str
    snapshot_id: str | None = None


class Outcome(Record):
    signal_id: str
    horizon_seconds: int
    evaluated_at: AwareDatetime
    end_snapshot_id: str
    measured_at: AwareDatetime
    return_pct: float
    btc_return_pct: float | None = None
    excess_return_pct: float | None = None
    max_favorable_pct: float
    max_adverse_pct: float
    paper_net_return_pct: float | None = None
    round_trip_cost_bps: float
    sample_count: int
    max_observation_gap_seconds: float
    time_to_mfe_seconds: float | None = None
    time_to_mae_seconds: float | None = None
