import math
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    detail_batch_size: int = Field(default=20, ge=1, le=100)
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
    meme_chains: list[str] = ["ethereum", "base", "solana"]
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
