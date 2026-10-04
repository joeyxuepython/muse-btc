"""Independent DEX discovery and conservative holder/wallet research framework."""

import asyncio
import re
import statistics
from collections import Counter

from .async_io import run_sync
from .intelligence import EvidenceRecord, IntelligenceStore
from .models import Features, Module, ProviderState, Snapshot, TokenRisk
from .providers.common import ProviderError, milliseconds, number

RISK_CHECKS = [
    "mint_authority",
    "freeze_authority",
    "owner_privilege",
    "honeypot",
    "sell_restriction",
    "blacklist",
    "transfer_tax",
    "upgradeability",
    "lp_lock",
    "creator_holdings",
    "insider_holdings",
    "bundled_supply",
    "sniper_wallet",
    "same_block_buyer",
    "dev_selling",
    "liquidity_removal",
]
CHAINS = {"ethereum": "1", "base": "8453", "bsc": "56", "solana": "solana"}


def token_identity(chain, address):
    if chain not in CHAINS:
        raise ValueError("不支持的链")
    if chain == "solana":
        if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", address):
            raise ValueError("Solana 地址格式无效")
    elif not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
        raise ValueError("EVM 地址格式无效")
    else:
        address = address.lower()
    return chain + ":" + address


def risk_score(risk: TokenRisk):
    return {
        "rug_risk_score": min(100, len(risk.blockers) * 30) if risk.blockers else None,
        "risk_status": "BLOCKED"
        if risk.blockers
        else "UNKNOWN"
        if risk.missing_checks
        else "SCREENED",
        "missing_checks": risk.missing_checks,
        "blockers": risk.blockers,
        "limitations": "缺失检查不视为安全；Rug 风险独立于 Discovery 分数",
    }


def holder_metrics(record):
    data = record.data
    ordinary = sorted(
        (h for h in data["holders"] if h.get("role", "ordinary") in {"ordinary", "insider"}),
        key=lambda h: h["balance"],
        reverse=True,
    )
    balances = [h["balance"] for h in ordinary]
    return {
        "observed_unique_holders": len(ordinary),
        "complete": data["complete"],
        "top10_pct": sum(balances[:10]) / data["total_supply"] * 100 if data["complete"] else None,
        "top20_pct": sum(balances[:20]) / data["total_supply"] * 100 if data["complete"] else None,
        "observed_top10_pct": sum(balances[:10]) / data["total_supply"] * 100,
        "observed_top20_pct": sum(balances[:20]) / data["total_supply"] * 100,
        "insider_pct": sum(h["balance"] for h in ordinary if h.get("role") == "insider")
        / data["total_supply"]
        * 100,
        "median_observed_position": statistics.median(balances) if balances else None,
        "excluded_roles": ["pool", "burn", "router", "program", "exchange"],
        "record_id": record.id,
        "as_of": record.available_at.isoformat(),
    }


def wallet_clusters(links, as_of):
    """Connected components are association hypotheses, not verified common ownership."""
    parent = {}

    def root(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            x = parent[x]
        return x

    for record in links:
        if record.available_at > as_of or record.data["confidence"] < 0.8:
            continue
        d = record.data
        if d["relationship"] == "same_block":
            continue
        a, b = d["chain"] + ":" + d["wallet_a"], d["chain"] + ":" + d["wallet_b"]
        parent[root(a)] = root(b)
    groups = {}
    for wallet in parent:
        groups.setdefault(root(wallet), []).append(wallet)
    return [sorted(wallets) for wallets in groups.values()]


class MemeEngine:
    def __init__(self, providers):
        self.providers, self.store, self.settings = providers, providers.store, providers.settings
        self.archive = IntelligenceStore(self.store)

    async def collect(self):
        if not self.settings.enable_meme_discovery:
            return []
        try:
            profiles, raw, at = await self.providers.get(
                "DEX Screener", self.settings.dexscreener_url, "/token-profiles/latest/v1"
            )
            if not isinstance(profiles, list):
                raise ProviderError("DEX profiles 响应结构不符")
            queued = await run_sync(self.store.state, "meme_discovery_queue") or []
            known = {r["key"] for r in queued}
            for p in profiles + [
                {"chainId": t.chain, "tokenAddress": t.address}
                for t in self.settings.meme_watchlist
            ]:
                chain, address = p.get("chainId"), p.get("tokenAddress")
                if chain not in self.settings.meme_chains or not address:
                    continue
                try:
                    key = token_identity(chain, address)
                except ValueError:
                    continue
                if key not in known:
                    queued.append({"key": key, "raw_id": raw, "first_seen": at.isoformat()})
                    known.add(key)
            await run_sync(self.store.set_state, "meme_discovery_queue", queued)
            cursor = int(await run_sync(self.store.state, "meme_discovery_cursor") or 0)
            batch = (
                [
                    queued[(cursor + i) % len(queued)]
                    for i in range(min(len(queued), self.settings.meme_discovery_batch_size))
                ]
                if queued
                else []
            )
            results = await asyncio.gather(*(self.asset(r) for r in batch), return_exceptions=True)
            await run_sync(
                self.store.set_state,
                "meme_discovery_cursor",
                (cursor + len(batch)) % max(1, len(queued)),
            )
            snapshots = [s for s in results if isinstance(s, Snapshot)]
            await run_sync(
                self.providers.status,
                "DEX Screener",
                ProviderState.DEGRADED,
                f"独立发现池 {len(queued)}；本轮有效 {len(snapshots)}/{len(batch)}；非全链覆盖",
                "公开 profiles 与已知 token pools；总库不受 100 币名额限制",
            )
            return snapshots
        except (ProviderError, ValueError, TypeError, KeyError):
            await run_sync(
                self.providers.status,
                "DEX Screener",
                ProviderState.UNAVAILABLE,
                "发现接口受限或响应无效",
                "Meme 独立发现",
            )
            return []

    async def asset(self, queued):
        chain, address = queued["key"].split(":", 1)
        pairs, raw, at = await self.providers.get(
            "DEX Screener", self.settings.dexscreener_url, f"/token-pairs/v1/{chain}/{address}"
        )
        if not isinstance(pairs, list):
            raise ProviderError("Token pools 响应结构不符")
        matches = [
            p
            for p in pairs
            if p.get("chainId") == chain
            and (
                str(p.get("baseToken", {}).get("address", "")) == address
                if chain == "solana"
                else str(p.get("baseToken", {}).get("address", "")).lower() == address.lower()
            )
            and (number(p.get("priceUsd")) or 0) > 0
        ]
        if not matches:
            return None
        pair = max(matches, key=lambda p: number((p.get("liquidity") or {}).get("usd")) or 0)
        created = milliseconds(pair["pairCreatedAt"]) if pair.get("pairCreatedAt") else None
        age = (at - created).total_seconds() / 3600 if created and created <= at else None
        risk = TokenRisk(
            missing_checks=list(RISK_CHECKS), warnings=["公开 profile 可能是付费推广，不证明新发行"]
        )
        raw_ids = [queued["raw_id"], raw]
        if self.settings.enable_goplus and chain != "solana":
            try:
                from .providers import token_risk

                payload, risk_raw, risk_at = await self.providers.get(
                    "GoPlus",
                    self.settings.goplus_url,
                    "/api/v1/token_security/" + CHAINS[chain],
                    {"contract_addresses": address},
                )
                data = (payload.get("result") or {}).get(address.lower())
                risk = token_risk(data, chain)
                risk.missing_checks += [
                    "insider_holdings",
                    "bundled_supply",
                    "dev_selling",
                    "sniper_wallet",
                ]
                if not risk.blockers:
                    risk.status = "NEEDS_VERIFICATION"
                raw_ids.append(risk_raw)
                at = max(at, risk_at)
            except (ProviderError, ValueError, KeyError, TypeError):
                risk.warnings.append("GoPlus 不可用")
        f = Features(
            liquidity_usd=number((pair.get("liquidity") or {}).get("usd")),
            volume_1h_usd=number((pair.get("volume") or {}).get("h1")),
            buys_5m=int((pair.get("txns") or {}).get("m5", {}).get("buys", 0)),
            sells_5m=int((pair.get("txns") or {}).get("m5", {}).get("sells", 0)),
            pool_age_hours=age,
            return_5m_pct=number((pair.get("priceChange") or {}).get("m5")),
        )
        old = await run_sync(self.archive.records, "meme", at, key=queued["key"])
        initial = old[-1].data["first_price"] if old else float(pair["priceUsd"])
        score_components = {
            "liquidity": min((f.liquidity_usd or 0) / 100000, 1) * 30,
            "volume": min((f.volume_1h_usd or 0) / 50000, 1) * 20,
            "buy_activity": min((f.buys_5m or 0) / 100, 1) * 10,
        }
        data = {
            "chain": chain,
            "address": address,
            "pair_address": pair["pairAddress"],
            "symbol": pair["baseToken"]["symbol"],
            "pool_age_hours": age,
            "market_cap_usd": number(pair.get("marketCap")),
            "fdv_usd": number(pair.get("fdv")),
            "liquidity_usd": f.liquidity_usd,
            "volume_1h_usd": f.volume_1h_usd,
            "first_detected": queued["first_seen"],
            "first_price": initial,
            "return_since_detection_pct": (float(pair["priceUsd"]) / initial - 1) * 100,
            "price": float(pair["priceUsd"]),
            "discovery_score": sum(score_components.values()),
            "score_components": score_components,
            "coverage_pct": 60,
            "missing": [
                "unique_buyers",
                "holders",
                "smart_wallet",
                "social",
                "launchpad_graduation",
            ],
            "discovery_scope": "PUBLIC_PROFILE_AND_KNOWN_POOLS_NOT_ALL_NEW_TOKENS",
            "promoted": bool((pair.get("boosts") or {}).get("active")),
            **risk_score(risk),
            "source_url": pair.get("url"),
            "risk": risk.model_dump(mode="json"),
        }
        await run_sync(
            self.archive.save,
            EvidenceRecord(
                kind="meme",
                key=queued["key"],
                source="DEX Screener",
                market_time=at,
                available_at=at,
                raw_ids=raw_ids,
                data=data,
            ),
        )
        return Snapshot(
            asset_id="dex:" + queued["key"],
            canonical_asset_id=queued["key"],
            symbol=pair["baseToken"]["symbol"],
            module=Module.MEME,
            source="DEX Screener",
            chain=chain,
            address=address,
            pair_address=pair["pairAddress"],
            market_time=at,
            available_at=at,
            price=float(pair["priceUsd"]),
            features=f,
            risk=risk,
            raw_ids=raw_ids,
            missing_metrics=data["missing"],
            quality_issues=["DEX_RECEIVE_TIME_ONLY", "NO_CONTINUOUS_TRADE_COVERAGE"],
            detail_updated_at=at,
            feature_version="meme-v4-4",
        )

    def dashboard(self, now):
        memes = self.archive.records("meme", now)
        holders = self.archive.records("holder", now)
        history = self.archive.records("holder", now, latest=False)
        holder_rows = []
        for r in holders:
            row = holder_metrics(r)
            previous = next(
                (p for p in history if p.key == r.key and p.available_at < r.available_at), None
            )
            old = holder_metrics(previous) if previous else None
            hours = (
                (r.available_at - previous.available_at).total_seconds() / 3600
                if previous
                else None
            )
            row["holder_growth_per_hour"] = (
                (row["observed_unique_holders"] - old["observed_unique_holders"]) / hours
                if hours and r.data["complete"] and previous.data["complete"]
                else None
            )
            row["token"] = r.key
            holder_rows.append(row)
        trades = self.archive.records("wallet_trade", now, limit=100000)
        links = self.archive.records("wallet_link", now, limit=100000)
        clusters = wallet_clusters(links, now)
        grouped = {}
        for t in sorted(trades, key=lambda r: (r.market_time, r.key)):
            if t.data["wallet_role"] == "ordinary":
                key = token_identity(t.data["chain"], t.data["address"])
                grouped.setdefault(key, []).append(t)
        early = []
        for key, token_trades in grouped.items():
            buys = [t for t in token_trades if t.data["side"] == "BUY"]
            unique = list(dict.fromkeys(t.data["wallet"] for t in buys))
            sell_wallets = {t.data["wallet"] for t in token_trades if t.data["side"] == "SELL"}
            counts = Counter(t.data["wallet"] for t in buys)
            early.append(
                {
                    "token": key,
                    "first_buyers": {str(k): unique[:k] for k in (10, 20, 50, 100)},
                    "observed_unique_buyers": len(unique),
                    "observed_unique_sellers": len(sell_wallets),
                    "repeat_buyers": sum(v > 1 for v in counts.values()),
                    "same_block_buys": dict(Counter(t.data["block"] for t in buys)),
                    "coverage": "IMPORTED_TRANSACTIONS_ONLY",
                }
            )
        return {
            "tokens": [r.model_dump(mode="json") for r in memes],
            "holders": holder_rows,
            "early_buyers": early,
            "wallet_clusters": clusters,
            "wallets": self.wallet_history(trades, now),
            "limitations": [
                "公开 profile 不是全链新币索引",
                "地址关联仅为假设",
                "未知权限阻止安全评级",
                "钱包样本不足时不授予 Smart Money 身份",
            ],
        }

    def wallet_history(self, trades, now):
        # FIFO realised lots; open holdings are not treated as wins.
        wallets, lots = {}, {}
        for r in sorted(trades, key=lambda r: (r.market_time, r.key)):
            d = r.data
            if d["wallet_role"] != "ordinary":
                continue
            wallet = d["chain"] + ":" + d["wallet"]
            key = wallet + ":" + d["address"]
            entry = wallets.setdefault(
                wallet,
                {
                    "wallet": wallet,
                    "returns": [],
                    "hold_seconds": [],
                    "first_seen": r.available_at.isoformat(),
                    "trades": 0,
                },
            )
            entry["trades"] += 1
            if d["side"] == "BUY":
                lots.setdefault(key, []).append([d["quantity"], d["price_usd"], r.market_time])
            else:
                quantity = d["quantity"]
                for lot in lots.get(key, []):
                    matched = min(quantity, lot[0])
                    if matched <= 0:
                        continue
                    entry["returns"].append((d["price_usd"] / lot[1] - 1) * 100)
                    entry["hold_seconds"].append((r.market_time - lot[2]).total_seconds())
                    lot[0] -= matched
                    quantity -= matched
        rows = []
        for entry in wallets.values():
            returns = entry.pop("returns")
            holds = entry.pop("hold_seconds")
            rows.append(
                {
                    **entry,
                    "closed_lots": len(returns),
                    "median_return_pct": statistics.median(returns) if returns else None,
                    "win_rate": statistics.mean(v > 0 for v in returns) if returns else None,
                    "median_hold_seconds": statistics.median(holds) if holds else None,
                    "smart_wallet_score": None,
                    "identity_status": "INSUFFICIENT_INDEPENDENT_HISTORY",
                    "missing": ["fees", "MFE", "MAE", "rug_exposure", "out_of_sample_validation"],
                }
            )
        return rows
