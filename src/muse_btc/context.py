"""Validated imports for source-specific adapters that need credentials or curated data."""

import math
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, ConfigDict, Field

from .intelligence import ContextInput, EvidenceRecord, IntelligenceStore, canonical_url
from .models import Record


class ContextData(Record):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Catalyst(ContextData):
    asset_id: str
    title: str = Field(min_length=5, max_length=500)
    event_time: AwareDatetime
    category: Literal["listing", "upgrade", "governance", "partnership", "unlock", "other"]
    direction: Literal["POSITIVE", "NEGATIVE", "UNKNOWN"] = "UNKNOWN"
    verified: bool = False
    confidence: float = Field(default=0, ge=0, le=1)


class Tokenomics(ContextData):
    asset_id: str
    circulating_supply: float | None = Field(default=None, ge=0)
    total_supply: float | None = Field(default=None, gt=0)
    fdv_usd: float | None = Field(default=None, gt=0)
    market_cap_usd: float | None = Field(default=None, gt=0)
    unlock_time: AwareDatetime | None = None
    unlock_pct_circulating: float | None = Field(default=None, ge=0)
    insider_pct: float | None = Field(default=None, ge=0, le=100)


class Fundamental(ContextData):
    asset_id: str
    fees_30d_usd: float | None = Field(default=None, ge=0)
    revenue_30d_usd: float | None = Field(default=None, ge=0)
    tvl_usd: float | None = Field(default=None, ge=0)
    active_users: int | None = Field(default=None, ge=0)
    verified: bool = False


class MacroEvent(ContextData):
    name: str
    release_time: AwareDatetime
    actual: float
    consensus: float | None = None
    previous: float | None = None
    units: str
    vintage: str


class ETFFlow(ContextData):
    asset: Literal["BTC", "ETH"]
    net_flow_usd: float
    holdings: float | None = Field(default=None, ge=0)
    aum: float | None = Field(default=None, ge=0)


class Holder(ContextData):
    chain: str
    address: str
    total_supply: float = Field(gt=0)
    holders: list[dict] = Field(max_length=10000)
    complete: bool = False
    slot_or_block: int | None = Field(default=None, ge=0)


class WalletTrade(ContextData):
    chain: str
    address: str
    wallet: str
    tx_id: str
    side: Literal["BUY", "SELL"]
    quantity: float = Field(gt=0)
    price_usd: float = Field(gt=0)
    block: int = Field(ge=0)
    wallet_role: Literal["ordinary", "pool", "burn", "router", "program", "exchange"] = "ordinary"


class WalletLink(ContextData):
    chain: str
    wallet_a: str
    wallet_b: str
    relationship: Literal["common_funder", "transfer", "same_block", "coordinated"]
    observed_tx: str
    confidence: float = Field(ge=0, le=1)


class SocialPost(ContextData):
    platform: str
    post_id: str
    author_id: str
    text: str = Field(min_length=1, max_length=20000)
    assets: list[str] = []
    contract_mentions: list[str] = []
    followers: int | None = Field(default=None, ge=0)
    likes: int = Field(default=0, ge=0)
    replies: int = Field(default=0, ge=0)
    reposts: int = Field(default=0, ge=0)
    paid_promotion: bool | None = None


class SocialThesis(ContextData):
    author_id: str
    asset_id: str
    direction: Literal["BULLISH", "BEARISH"]
    horizon_seconds: int = Field(ge=300, le=2592000)
    reference_price: float = Field(gt=0)
    post_id: str


DATA_MODELS = {
    "catalyst": Catalyst,
    "tokenomics": Tokenomics,
    "fundamental": Fundamental,
    "macro_event": MacroEvent,
    "etf": ETFFlow,
    "holder": Holder,
    "wallet_trade": WalletTrade,
    "wallet_link": WalletLink,
    "social": SocialPost,
    "social_thesis": SocialThesis,
}


def import_context(store, item: ContextInput, now: datetime):
    url = canonical_url(item.source_url)
    data = DATA_MODELS[item.kind].model_validate(item.data).model_dump(mode="json")
    if item.market_time > now:
        raise ValueError("证据发布时间不能在未来；未来事件写入 event_time 字段")
    if item.kind == "macro_event":
        if datetime.fromisoformat(data["release_time"]) > now:
            raise ValueError("实际宏观数据不能在发布前录入")
        data["surprise"] = (
            data["actual"] - data["consensus"] if data["consensus"] is not None else None
        )
    if item.kind == "holder":
        for h in data["holders"]:
            if (
                not isinstance(h.get("wallet"), str)
                or not isinstance(h.get("balance"), (int, float))
                or h["balance"] < 0
                or not math.isfinite(h["balance"])
                or h.get("role", "ordinary")
                not in {"ordinary", "pool", "burn", "router", "program", "exchange", "insider"}
            ):
                raise ValueError("持仓字段或地址角色不符")
        if len({h["wallet"] for h in data["holders"]}) != len(data["holders"]):
            raise ValueError("持有人地址重复")
        if sum(h["balance"] for h in data["holders"]) > data["total_supply"] * 1.000001:
            raise ValueError("持仓总量超过供应量")
    # Identity is derived from validated domain fields, not user-provided tickers/keys.
    key = item.key
    if item.kind in {"holder", "wallet_trade"}:
        address = data["address"].lower() if data["chain"] != "solana" else data["address"]
        key = data["chain"] + ":" + address
        if item.kind == "wallet_trade":
            key += ":" + data["tx_id"] + ":" + data["wallet"] + ":" + data["side"]
    elif item.kind == "social":
        key = data["platform"] + ":" + data["post_id"]
    data["source_url"] = url
    raw = store.save_raw("LOCAL_VERIFIED_IMPORT", url, data, now)
    return IntelligenceStore(store).save(
        EvidenceRecord(
            kind=item.kind,
            key=key,
            source="LOCAL_IMPORT",
            market_time=item.market_time,
            available_at=now,
            raw_ids=[raw],
            data=data,
        )
    )


def asset_context(archive, asset_id, now):
    records = [
        r
        for kind in ("catalyst", "tokenomics", "fundamental")
        for r in archive.records(kind, now)
        if r.data.get("asset_id") == asset_id
    ]
    supporting, risks, groups, missing = [], [], [], []
    for record in records:
        d = record.data
        if record.kind == "catalyst":
            t = datetime.fromisoformat(d["event_time"])
            if not d["verified"] or (now - t).total_seconds() > 86400 * 7:
                continue
            if d["direction"] == "POSITIVE" and d["confidence"] >= 0.7:
                supporting.append("已核实催化剂：" + d["title"])
                groups.append("catalyst")
            if d["direction"] == "NEGATIVE":
                risks.append("负面催化剂：" + d["title"])
        elif record.kind == "tokenomics":
            unlock = datetime.fromisoformat(d["unlock_time"]) if d["unlock_time"] else None
            if (
                unlock
                and 0 <= (unlock - now).total_seconds() <= 86400 * 7
                and (d["unlock_pct_circulating"] or 0) >= 5
            ):
                risks.append(f"7 日内解锁占流通量 {d['unlock_pct_circulating']:.1f}%")
            if d["fdv_usd"] and d["market_cap_usd"] and d["fdv_usd"] / d["market_cap_usd"] > 10:
                risks.append("FDV / 流通市值超过 10 倍")
        elif record.kind == "fundamental" and d["verified"]:
            supporting.append("基本面有可审计记录；不直接解释短期买盘")
    for kind in ("catalyst", "tokenomics", "fundamental"):
        if not any(r.kind == kind for r in records):
            missing.append(kind)
    return {
        "records": [r.id for r in records],
        "supporting": supporting,
        "risks": risks,
        "evidence_groups": list(set(groups)),
        "missing": missing,
    }
