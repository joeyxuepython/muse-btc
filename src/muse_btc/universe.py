from datetime import datetime

from .config import Settings
from .providers.common import number

CORE = ("BTCUSDT", "ETHUSDT")
STABLE = {
    "USDC",
    "FDUSD",
    "TUSD",
    "USDP",
    "DAI",
    "BUSD",
    "USD1",
    "USDE",
    "USDS",
    "USDD",
    "PYUSD",
    "WUSDT",
    "WUSDC",
    "EUR",
    "EURI",
    "AEUR",
}
LEVERAGED = {
    base + suffix
    for base in (
        "BTC",
        "ETH",
        "BNB",
        "ADA",
        "XRP",
        "LINK",
        "DOT",
        "EOS",
        "LTC",
        "TRX",
        "UNI",
        "YFI",
    )
    for suffix in ("UP", "DOWN", "BULL", "BEAR")
}


def build_universe(
    info: dict,
    tickers: list,
    swaps: list,
    swap_tickers: list,
    previous: dict | None,
    settings: Settings,
    at: datetime,
    raw_ids: list[str],
) -> dict:
    swap_by_base = {}
    for row in swaps:
        family = row.get("instFamily", "")
        if (
            row.get("state") == "live"
            and row.get("settleCcy") == "USDT"
            and row.get("ctType") == "linear"
            and family.endswith("-USDT")
            and row.get("instId") == family + "-SWAP"
        ):
            swap_by_base[family[:-5]] = row
    quotes = {r["symbol"]: r for r in tickers if isinstance(r, dict) and "symbol" in r}
    perp = {r["instId"]: r for r in swap_tickers if isinstance(r, dict) and "instId" in r}
    entries = []
    previous_entries = {r["canonical_asset_id"]: r for r in (previous or {}).get("entries", [])}
    rejected_pins = set(settings.pinned_symbols)
    for row in info.get("symbols", []):
        symbol, base = row.get("symbol"), row.get("baseAsset")
        ticker = quotes.get(symbol, {})
        price, volume = number(ticker.get("lastPrice")), number(ticker.get("quoteVolume"))
        if (
            row.get("status") != "TRADING"
            or row.get("quoteAsset") != "USDT"
            or not row.get("isSpotTradingAllowed", True)
            or base in STABLE | LEVERAGED
            or not base
            or not price
            or price <= 0
            or volume is None
            or volume < 0
        ):
            continue
        if (
            symbol not in CORE
            and symbol not in settings.pinned_symbols
            and volume < settings.min_quote_volume_usdt
        ):
            continue
        rejected_pins.discard(symbol)
        swap = swap_by_base.get(base)
        inst = swap["instId"] if swap else None
        derivative_ticker = perp.get(inst, {})
        coin_volume = number(derivative_ticker.get("volCcy24h"))
        last = number(derivative_ticker.get("last"))
        # OKX SWAP volCcy24h is base currency, so coin amounts cannot be ranked across assets.
        notional = (
            coin_volume * last
            if coin_volume is not None and coin_volume >= 0 and last and last > 0
            else None
        )
        entries.append(
            {
                "canonical_asset_id": f"asset:{base}",
                "first_seen": previous_entries.get(f"asset:{base}", {}).get(
                    "first_seen", at.isoformat()
                ),
                "binance_symbol": symbol,
                "okx_inst_id": inst,
                "quote_currency": "USDT",
                "contract_type": "SWAP" if swap else None,
                "contract_address": None,
                "chain": None,
                "token_identity_status": "NEEDS_VERIFICATION",
                "contract_metadata": swap,
                "pinned": symbol in settings.pinned_symbols,
                "volume_24h_usdt": volume,
                "okx_volume_24h_usdt_proxy": notional,
                "score": 0.0,
                "score_components": {},
                "missing_selection_data": [
                    "30d_activity",
                    "market_cap",
                    "full_depth",
                    "delisting_notice",
                ],
                "_spot": ticker,
                "_perp": perp.get(inst, {}),
            }
        )
    core = [next((r for r in entries if r["binance_symbol"] == s), None) for s in CORE]
    if any(r is None for r in core):
        raise ValueError("BTCUSDT 或 ETHUSDT 没有有效的可交易现货报价")
    candidates = [r for r in entries if r["binance_symbol"] not in CORE]
    total = len(candidates)
    for row in candidates:
        q = row["_spot"]
        vol_rank = sum(r["volume_24h_usdt"] <= row["volume_24h_usdt"] for r in candidates)
        turnover = vol_rank / total if total else 0
        bid, ask = number(q.get("bidPrice")), number(q.get("askPrice"))
        spread = (ask - bid) / ((ask + bid) / 2) * 10000 if bid and ask and ask >= bid else None
        spot_change = number(q.get("priceChangePercent"))
        derivative_volume = row["okx_volume_24h_usdt_proxy"]
        components = {"volume": turnover, "quality": 1.0 if row["okx_inst_id"] else 0.5}
        if spread is not None:
            components["liquidity"] = max(0.0, 1 - spread / 30)
        if derivative_volume is not None and derivative_volume >= 0:
            values = [r["okx_volume_24h_usdt_proxy"] for r in candidates]
            # Rank derivative activity within OKX; never compare raw cross-venue volumes.
            components["derivatives"] = (
                sum(v is not None and v <= derivative_volume for v in values) / total
            )
        if spot_change is not None:
            components["volatility"] = min(abs(spot_change) / 10, 1)
        row["score_components"] = components
        row["score"] = sum(settings.universe_weights.get(k, 0) * v for k, v in components.items())
        row["selection_coverage_pct"] = (
            sum(settings.universe_weights.get(k, 0) for k in components)
            / sum(settings.universe_weights.values())
            * 100
        )
    candidates.sort(
        key=lambda r: (
            r["pinned"],
            bool(r["okx_inst_id"]),
            r["score"],
            r["volume_24h_usdt"],
            r["binance_symbol"],
        ),
        reverse=True,
    )
    pins = [r for r in candidates if r["pinned"]]
    if len(pins) > settings.max_altcoins:
        raise ValueError("固定观察名单超过山寨币名额")
    old_symbols = {
        r["binance_symbol"] for r in (previous or {}).get("entries", []) if r["tier"] != "CORE"
    }
    current = {r["binance_symbol"]: r for r in candidates}
    retained = [r for r in candidates if r["binance_symbol"] in old_symbols and not r["pinned"]]
    selected = pins + retained[: settings.max_altcoins - len(pins)]
    new_candidates = [
        r for r in candidates if r["binance_symbol"] not in {x["binance_symbol"] for x in selected}
    ]
    replacements = 0
    for row in new_candidates:
        if len(selected) < settings.max_altcoins:
            selected.append(row)
        elif previous and replacements < settings.universe_max_replacements:
            replaceable = [
                r for r in selected if not r["pinned"] and r["binance_symbol"] in old_symbols
            ]
            if not replaceable:
                break
            lowest = min(replaceable, key=lambda r: (bool(r["okx_inst_id"]), r["score"]))
            if (bool(row["okx_inst_id"]), row["score"]) > (
                bool(lowest["okx_inst_id"]),
                lowest["score"],
            ):
                selected.remove(lowest)
                selected.append(row)
                replacements += 1
    selected.sort(key=lambda r: (r["pinned"], r["score"]), reverse=True)
    for i, row in enumerate(selected):
        row["tier"] = "TIER1" if i < 20 else "TIER2" if i < 50 else "TIER3"
    for row in core:
        row["tier"] = "CORE"
    result = core + selected
    for row in result:
        row.pop("_spot")
        row.pop("_perp")
    return {
        "selected_at": at.isoformat(),
        "entries": result,
        "target_altcoins": settings.max_altcoins,
        "actual_altcoins": len(selected),
        "replacements": replacements,
        "forced_removals": sorted(old_symbols - set(current)),
        "missing_pins": sorted(rejected_pins),
        "raw_ids": raw_ids,
        "version": "universe-v4",
        "weights": dict(settings.universe_weights),
    }
