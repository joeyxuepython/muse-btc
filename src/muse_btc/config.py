from pathlib import Path

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class WatchedToken(BaseModel):
    chain: str
    address: str = Field(min_length=10, max_length=128, pattern=r"^[A-Za-z0-9]+$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MUSE_", env_file=".env", extra="ignore")

    database_path: Path = Path("data/muse.db")
    poll_seconds: int = Field(default=120, ge=30, le=3600)
    max_altcoins: int = Field(default=99, ge=1, le=99)
    detail_batch_size: int = Field(default=20, ge=1, le=99)
    universe_refresh_seconds: int = Field(default=3600, ge=300, le=86400)
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
    # 衍生品数据源：okx（默认，公开接口无地区限制）、binance、auto（先 Binance，失败回退 OKX）
    futures_source: str = Field(default="okx", pattern=r"^(okx|binance|auto)$")
    dexscreener_url: str = "https://api.dexscreener.com"
    goplus_url: str = "https://api.gopluslabs.io"
    fee_bps_each_way: float = Field(default=10, ge=0)
    slippage_bps_each_way: float = Field(default=5, ge=0)
