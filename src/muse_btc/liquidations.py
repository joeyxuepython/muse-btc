"""Explicit, bounded public liquidation observation; never infer full-market totals."""

import asyncio
import json

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from .async_io import run_sync
from .intelligence import EvidenceRecord, IntelligenceStore, digest
from .models import utc_now
from .providers.common import ProviderError, milliseconds, number

LIQUIDATION_URL = "wss://fstream.binance.com/market/ws/!forceOrder@arr"


def liquidation_event(payload, received):
    event = payload.get("data", payload) if isinstance(payload, dict) else None
    if not isinstance(event, dict) or event.get("e") != "forceOrder":
        return None
    order = event.get("o")
    if not isinstance(order, dict):
        raise ProviderError("清算事件缺少订单结构")
    # CM quantities are contract units, so exclude CM rather than multiply them as coins.
    if event.get("st", 1) != 1 or order.get("s") not in {"BTCUSDT", "ETHUSDT"}:
        return None
    try:
        trade_time, event_time = milliseconds(order["T"]), milliseconds(event["E"])
        if trade_time > event_time or event_time > received:
            raise ValueError
        if order.get("S") not in {"BUY", "SELL"}:
            raise ValueError
        qty, price = number(order.get("z")), number(order.get("ap"))
        if qty is None or price is None or qty <= 0 or price <= 0:
            raise ValueError
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        raise ProviderError("清算事件时间、方向或累计成交字段无效") from exc
    data = {
        "symbol": order["s"],
        "order_side": order["S"],
        "liquidated_position": "LONG" if order["S"] == "SELL" else "SHORT",
        "cumulative_filled_qty_base": qty,
        "average_fill_price_quote": price,
        "snapshot_filled_notional_quote": qty * price,
        "quote_unit": "USDT",
        "event_time": event_time.isoformat(),
        "order_status": order.get("X"),
        "coverage": "BINANCE_UM_LATEST_PER_SYMBOL_PER_SECOND",
        "full_market": False,
        "source_url": "https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market",
    }
    return trade_time, data


async def collect_liquidations(store, settings, seconds=10, connect_factory=connect):
    if not 1 <= seconds <= 3600:
        raise ValueError("清算监听时长必须为 1–3600 秒")
    archive = IntelligenceStore(store)
    started, connected_at, ended = utc_now(), None, None
    observed, rejected, error = 0, 0, None
    proxy = settings.binance_proxy.get_secret_value() if settings.binance_proxy else True
    try:
        async with connect_factory(
            LIQUIDATION_URL,
            proxy=proxy,
            open_timeout=settings.request_timeout_seconds,
            close_timeout=2,
            max_size=settings.research_max_bytes,
            ping_interval=20,
            ping_timeout=20,
        ) as socket:
            connected_at = utc_now()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + seconds
            while loop.time() < deadline:
                try:
                    message = await asyncio.wait_for(socket.recv(), deadline - loop.time())
                except TimeoutError:
                    break
                received = utc_now()
                try:
                    payload = json.loads(message)
                    parsed = liquidation_event(payload, received)
                    if parsed is None:
                        continue
                    trade_time, data = parsed
                    raw = await run_sync(
                        store.save_raw, "Binance Liquidations", LIQUIDATION_URL, payload, received
                    )
                    record = EvidenceRecord(
                        kind="liquidation",
                        key="Binance:" + digest(payload),
                        source="Binance Liquidations",
                        market_time=trade_time,
                        available_at=received,
                        raw_ids=[raw],
                        data=data,
                    )
                    saved = await run_sync(archive.save, record)
                    observed += int(saved.id == record.id)
                except (ValueError, ProviderError, TypeError):
                    rejected += 1
            ended = utc_now()
    except asyncio.CancelledError:
        error = "CancelledError"
        raise
    except (OSError, TimeoutError, WebSocketException, ValueError) as exc:
        # Exception text can include a proxy URL; retain the class only.
        error = type(exc).__name__
    finally:
        at = utc_now()
        ended = ended or at
        status = (
            "UNAVAILABLE"
            if connected_at is None
            else "DEGRADED"
            if error or rejected
            else "SAMPLED"
            if observed
            else "OBSERVED_NO_EVENTS"
        )
        result = {
            "status": status,
            "source": "Binance Liquidations",
            "checked_at": at.isoformat(),
            "connected_at": connected_at.isoformat() if connected_at else None,
            "ended_at": ended.isoformat(),
            "connected_seconds": (ended - connected_at).total_seconds() if connected_at else 0,
            "new_events": observed,
            "rejected_messages": rejected,
            "error": error,
            "full_market": False,
            "full_market_total_usd": None,
            "limitations": [
                "每交易对每秒最多一笔快照",
                "仅 BTC/ETH USDT 本位",
                "无历史回补",
                "无事件不表示全市场零清算",
                "累计成交快照不可相加为完整清算量",
            ],
        }
        await run_sync(
            archive.save,
            EvidenceRecord(
                kind="liquidation_window",
                key="Binance:" + started.isoformat(),
                source="Binance Liquidations",
                market_time=started,
                available_at=at,
                data=result,
            ),
        )
    return result
