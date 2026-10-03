import math
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_THRESHOLDS = {
    "flat_price_pct": 0.5,
    "rvol": 1.8,
    "oi_build_pct": 1.5,
    "funding_hot_pct": 0.05,
    "spot_buy_ratio": 0.58,
    "relative_strength_pct": 0.3,
    "max_spread_bps": 15,
    "setup_groups": 3,
    "strong_groups": 4,
}


class WatchedToken(BaseModel):
    chain: str
    address: str = Field(min_length=10, max_length=128, pattern=r"^[A-Za-z0-9]+$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MUSE_", env_file=".env", extra="ignore", validate_assignment=True
    )

    database_path: Path = Path("data/muse.db")
    poll_seconds: int = Field(default=120, ge=30, le=3600)
    max_altcoins: int = Field(default=100, ge=1, le=100)
    detail_batch_size: int = Field(default=100, ge=1, le=100)
    universe_refresh_seconds: int = Field(default=86400, ge=300, le=86400)
    universe_max_replacements: int = Field(default=10, ge=0, le=100)
    pinned_symbols: list[str] = []
    min_quote_volume_usdt: float = Field(default=100000, ge=0)
    universe_weights: dict[str, float] = {
        "liquidity": 30,
        "volume": 25,
        "derivatives": 20,
        "volatility": 10,
        "importance": 10,
        "quality": 5,
    }
    ranking_weights: dict[str, float] = {
        "relative_volume": 20,
        "relative_strength": 20,
        "spot_flow": 20,
        "oi": 15,
        "funding": 10,
        "taker": 10,
        "liquidity": 5,
    }
    time_alignment_seconds: int = Field(default=120, ge=1, le=300)
    statistics_stale_seconds: int = Field(default=600, ge=300, le=1800)
    alert_score_delta: float = Field(default=10, ge=1, le=100)
    max_memes: int = Field(default=8, ge=1, le=30)
    meme_chains: list[str] = ["ethereum", "base", "bsc", "solana"]
    meme_watchlist: list[WatchedToken] = []
    enable_collector: bool = True
    enable_goplus: bool = False
    stale_seconds: int = Field(default=300, ge=60, le=3600)
    alert_cooldown_seconds: int = Field(default=3600, ge=60)
    request_timeout_seconds: float = Field(default=12, ge=1, le=60)
    api_token: SecretStr | None = None
    binance_spot_url: str = "https://data-api.binance.vision"
    binance_futures_url: str = "https://fapi.binance.com"
    # 可选：仅 Binance 相关域名走的代理（如新加坡节点），形如 http://user:pass@host:port；
    # 不设置则全部请求走默认出口。DEX Screener / GoPlus 等始终走默认出口。
    binance_proxy: SecretStr | None = None
    okx_url: str = "https://www.okx.com"
    # Legacy values remain readable; V4 always collects derivatives from OKX.
    futures_source: str = Field(default="okx", pattern=r"^(okx|binance|auto)$")
    dexscreener_url: str = "https://api.dexscreener.com"
    goplus_url: str = "https://api.gopluslabs.io"
    fee_bps_each_way: float = Field(default=10, ge=0)
    slippage_bps_each_way: float = Field(default=5, ge=0)
    # Application collection is opt-in; these switches never create a Codex scheduled task.
    enable_intelligence: bool = True
    enable_meme_discovery: bool = False
    enable_social: bool = False
    macro_series: list[str] = [
        "DFF",
        "CPIAUCSL",
        "CPILFESL",
        "PCEPI",
        "PCEPILFE",
        "PAYEMS",
        "UNRATE",
        "ICSA",
        "GDPC1",
        "DTWEXBGS",
        "DGS2",
        "DGS10",
        "VIXCLS",
        "NASDAQCOM",
        "SP500",
        "WALCL",
        "WTREGEN",
        "RRPONTSYD",
        "WRESBAL",
    ]
    enable_background_intelligence: bool = False
    background_scopes: list[str] = ["macro", "events", "onchain", "options"]
    worker_tick_seconds: int = Field(default=30, ge=5, le=300)
    event_refresh_seconds: int = Field(default=300, ge=60)
    event_reaction_tolerance_seconds: int = Field(default=30, ge=1, le=60)
    macro_series_max_age_days: dict[str, int] = {"GDPC1": 150, "WALCL": 14, "WTREGEN": 14}
    macro_stale_days: int = Field(default=45, ge=1, le=180)
    intelligence_refresh_seconds: int = Field(default=3600, ge=300)
    research_max_documents: int = Field(default=100, ge=1, le=300)
    research_max_bytes: int = Field(default=2000000, ge=10000, le=5000000)
    research_feeds: dict[str, str] = {
        "Glassnode": "https://research.glassnode.com/rss/",
        "Coinbase": "https://www.coinbase.com/institutional/research-insights",
        "CoinShares": "https://coinshares.com/insights/research-data/",
        "Santiment": "https://insights.santiment.net/feed",
        "arXiv": "https://export.arxiv.org/api/query",
    }
    stablecoins_url: str = "https://stablecoins.llama.fi"
    fred_url: str = "https://fred.stlouisfed.org"
    etf_url: str = "https://farside.co.uk/btc/"
    # Free BTC collection can be invoked explicitly or enabled in the cloud worker.
    coinmetrics_url: str = "https://community-api.coinmetrics.io/v4"
    bgeometrics_url: str = "https://bitcoin-data.com/v1"
    deribit_url: str = "https://www.deribit.com/api/v2"
    btc_history_days: int = Field(default=365, ge=8, le=1460)
    btc_onchain_refresh_seconds: int = Field(default=86400, ge=86400)
    btc_options_refresh_seconds: int = Field(default=300, ge=60)
    deribit_greeks_limit: int = Field(default=12, ge=0, le=40)
    x_bearer_token: SecretStr | None = None
    social_max_pages: int = Field(default=3, ge=1, le=10)
    x_query: str = "(BTC OR ETH OR crypto) -is:retweet lang:en"
    meme_discovery_batch_size: int = Field(default=20, ge=1, le=100)
    breakout_threshold_pct: float = Field(default=3, gt=0)
    ranking_hit_threshold_pct: float = Field(default=2, gt=0)
    ml_min_samples: int = Field(default=200, ge=40)
    rule_thresholds: dict[str, float] = DEFAULT_THRESHOLDS
    threshold_version: str = "thresholds-v4-2"

    @field_validator("background_scopes")
    @classmethod
    def valid_scopes(cls, values):
        if set(values) - {"macro", "events", "onchain", "options", "research", "social"}:
            raise ValueError("Unsupported background scope")
        return list(dict.fromkeys(values))

    @field_validator("macro_series")
    @classmethod
    def valid_series(cls, values):
        if any(not v.isalnum() or len(v) > 32 for v in values):
            raise ValueError("FRED series identifiers must be alphanumeric")
        return list(dict.fromkeys(values))

    @field_validator("rule_thresholds")
    @classmethod
    def valid_thresholds(cls, values):
        if set(values) - set(DEFAULT_THRESHOLDS):
            raise ValueError("Unknown rule threshold")
        values = DEFAULT_THRESHOLDS | values
        if any(not math.isfinite(v) or v < 0 for v in values.values()):
            raise ValueError("Thresholds must be finite and nonnegative")
        if values["strong_groups"] < max(3, values["setup_groups"]):
            raise ValueError("STRONG cannot require fewer groups than SETUP")
        if values["spot_buy_ratio"] > 1 or values["setup_groups"] < 2:
            raise ValueError("Invalid flow ratio or confirmation count")
        return values

    @field_validator("universe_weights", "ranking_weights")
    @classmethod
    def valid_weights(cls, values):
        if (
            not values
            or any(not math.isfinite(v) or v < 0 for v in values.values())
            or sum(values.values()) <= 0
        ):
            raise ValueError("权重必须有限、非负且总和大于零")
        return values
