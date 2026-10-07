"""Offline BTC SMA200 study. No production imports, keys, orders or network calls."""

import datetime as dt
import hashlib
import json
import math
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
DAY = 86_400_000
HOUR = 3_600_000


def stamp(value):
    return int(dt.datetime.fromisoformat(value).timestamp() * 1000)


def iso(value):
    return dt.datetime.fromtimestamp(int(value) / 1000, dt.UTC).isoformat()


def write(name, obj):
    (ROOT / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False))


def read_rows(prefix):
    rows = []
    for file in sorted((ROOT / "raw").glob(prefix + "-*.json")):
        if ".meta." not in file.name:
            rows.extend(json.loads(file.read_text()))
    return sorted(rows, key=lambda row: row[0])


def validate_rows(rows, step, start, end):
    times = [r[0] for r in rows]
    if len(times) != len(set(times)):
        raise ValueError("Duplicate Binance bar timestamps")
    expected = set(range(start, end + 1, step))
    malformed = []
    shortened = []
    for row in rows:
        o, h, low, c, v = map(float, row[1:6])
        values = [o, h, low, c, v, float(row[7]), float(row[9]), float(row[10])]
        if row[6] < row[0] + step - 1:
            shortened.append(
                {
                    "open": iso(row[0]),
                    "source_close": iso(row[6]),
                    "nominal_close": iso(row[0] + step - 1),
                }
            )
        if not (
            all(math.isfinite(x) for x in values)
            and 0 < low <= min(o, c) <= max(o, c) <= h
            and min(v, *values[5:]) >= 0
            and row[0] <= row[6] <= row[0] + step - 1
            and row[0] % step == 0
        ):
            malformed.append(row[0])
    return {
        "rows": len(rows),
        "expected_rows": len(expected),
        "missing": [iso(x) for x in sorted(expected - set(times))],
        "unexpected": [iso(x) for x in sorted(set(times) - expected)],
        "malformed": [iso(x) for x in malformed],
        "duplicates": 0,
        "shortened_bars": shortened,
    }


def prepare(cfg):
    frozen = json.loads((ROOT / "protocol-freeze.json").read_text())
    assert (
        hashlib.sha256((ROOT / "protocol.json").read_bytes()).hexdigest()
        == frozen["protocol_sha256"]
    )
    assert (
        hashlib.sha256((ROOT / "amendment-A1.json").read_bytes()).hexdigest()
        == (ROOT / "amendment-A1.sha256").read_text().strip()
    )
    assert (
        hashlib.sha256((ROOT / "amendment-A2.json").read_bytes()).hexdigest()
        == (ROOT / "amendment-A2.sha256").read_text().strip()
    )
    for record in json.loads((ROOT / "data-manifest.json").read_text()):
        if "error" not in record:
            assert (
                hashlib.sha256((ROOT / "raw" / (record["name"] + ".json")).read_bytes()).hexdigest()
                == record["sha256"]
            )
    daily = read_rows("binance-1d")
    hourly = read_rows("binance-1h")
    ds = stamp(cfg["raw_start_utc"])
    de = stamp(cfg["last_complete_daily_bar_utc"])
    he = stamp(cfg["execution_end_utc"])
    quality = {
        "daily": validate_rows(daily, DAY, ds, de),
        "hourly": validate_rows(hourly, HOUR, ds, he),
    }
    hd = {r[0]: r for r in hourly}
    dd = {r[0]: r for r in daily}
    mismatch = []
    incomplete_days = []
    for row in daily:
        hours = [hd[t] for t in range(row[0], row[0] + DAY, HOUR) if t in hd]
        if len(hours) != 24:
            incomplete_days.append(iso(row[0]))
            continue
        computed = [
            float(hours[0][1]),
            max(float(r[2]) for r in hours),
            min(float(r[3]) for r in hours),
            float(hours[-1][4]),
            sum(float(r[5]) for r in hours),
        ]
        observed = list(map(float, row[1:6]))
        if not np.allclose(computed, observed, rtol=1e-8, atol=1e-6):
            mismatch.append({"date": iso(row[0]), "daily": observed, "hourly_sum": computed})
    quality["aggregation"] = {
        "checked_days": len(daily) - len(incomplete_days),
        "incomplete_days_not_filled": incomplete_days,
        "mismatches": mismatch,
    }
    cb = read_rows("coinbase-1d")
    cbmap = {}
    conflicts = []
    for row in cb:
        if row[0] in cbmap and cbmap[row[0]] != row:
            conflicts.append(row[0])
        cbmap[row[0]] = row
    comparison = []
    cb_bad = []
    for row in cbmap.values():
        t, low, high, op, close, volume = row
        if not (
            all(math.isfinite(float(x)) for x in row)
            and 0 < low <= min(op, close) <= max(op, close) <= high
            and volume >= 0
            and t % 86400 == 0
        ):
            cb_bad.append(t)
            continue
        if t * 1000 in dd:
            primary = float(dd[t * 1000][4])
            comparison.append(
                {
                    "date": iso(t * 1000),
                    "binance_usdt_close": primary,
                    "coinbase_usd_close": close,
                    "relative_difference": primary / close - 1,
                }
            )
    write("venue-comparison.json", comparison)
    diffs = np.array([abs(r["relative_difference"]) for r in comparison])
    quality["cross_venue"] = {
        "note": (
            "BTCUSD vs BTCUSDT; not identical currencies. Diagnostic only; no price substitution."
        ),
        "unique_coinbase_rows": len(cbmap),
        "duplicate_boundary_rows": len(cb) - len(cbmap),
        "conflicting_duplicates": conflicts,
        "malformed_rows": cb_bad,
        "overlap": len(comparison),
        "median_absolute_relative_difference": float(np.median(diffs)) if len(diffs) else None,
        "p99_absolute_relative_difference": float(np.quantile(diffs, 0.99)) if len(diffs) else None,
        "over_one_percent": [r for r in comparison if abs(r["relative_difference"]) > 0.01],
    }
    start = stamp(cfg["execution_start_utc"])
    times = list(range(start, he + 1, DAY))
    actual, prices, delays, required_missing = [], [], [], []
    for t in times:
        chosen = t
        if t not in hd or float(hd[t][5]) <= 0:
            if t == stamp("2018-07-04T01:00:00+00:00"):
                options = [
                    x
                    for x in range(t + HOUR, t // DAY * DAY + DAY, HOUR)
                    if x in hd and float(hd[x][5]) > 0
                ]
                if options:
                    chosen = options[0]
                else:
                    required_missing.append(iso(t))
            else:
                required_missing.append(iso(t))
        if chosen in hd:
            actual.append(chosen)
            prices.append(float(hd[chosen][1]))
            if chosen != t:
                delays.append(
                    {"scheduled": iso(t), "actual": iso(chosen), "hours": (chosen - t) / HOUR}
                )
    quality["execution"] = {
        "scheduled_points": len(times),
        "actual_points": len(actual),
        "unresolved": required_missing,
        "maintenance_delays": delays,
    }
    fatal = bool(
        quality["daily"]["missing"]
        or quality["daily"]["malformed"]
        or quality["daily"]["unexpected"]
        or quality["hourly"]["malformed"]
        or quality["hourly"]["unexpected"]
        or mismatch
        or required_missing
    )
    quality["original_strict_full_hourly_grid_passed"] = not quality["hourly"]["missing"]
    quality["amended_required_input_gate_passed"] = not fatal
    write("data-quality.json", quality)
    if fatal:
        raise ValueError(
            "Required input quality failed; see data-quality.json; performance not calculated"
        )
    weights, observations = signals(dd, times[:-1], cfg["lookback_days"])
    write("signal-ledger.json", observations)
    return np.array(times), np.array(actual), np.array(prices), np.array(weights), quality


def signals(dd, times, lookback):
    weights, observations = [], []
    for t in times:
        midnight = int(t) // DAY * DAY
        dates = list(range(midnight - lookback * DAY, midnight, DAY))
        if any(d not in dd for d in dates):
            raise ValueError("Missing completed signal bar")
        closes = [float(dd[d][4]) for d in dates]
        average = math.fsum(closes) / lookback
        value = int(closes[-1] > average)
        weights.append(value)
        observations.append(
            {
                "scheduled_execution_utc": iso(t),
                "signal_available_utc": iso(midnight),
                "last_input_close_utc": iso(midnight - 1),
                "previous_close": closes[-1],
                "sma": average,
                "target_btc_weight": value,
            }
        )
    return weights, observations


def simulate(prices, weights, fee, slip, capital=10000.0, times=None):
    """Self-financing cash/units ledger. Desired weights are POST-cost at reference price."""
    n = len(weights)
    assert len(prices) == n + 1
    cash, units = capital, 0.0
    equity = [capital]
    events = []
    buy_factor, sell_factor = (1 + slip) * (1 + fee), (1 - slip) * (1 - fee)

    def transact(i, target, terminal=False):
        nonlocal cash, units
        p = float(prices[i])
        pre = cash + units * p
        difference = target * pre - units * p
        if abs(difference) < max(1e-8, pre * 1e-12):
            return
        if difference > 0:
            notional = difference / (1 + target * (buy_factor - 1))
            qty = notional / p
            fill = p * (1 + slip)
            cash -= qty * fill * (1 + fee)
            units += qty
            side = "buy"
        else:
            notional = -difference / (1 - target * (1 - sell_factor))
            qty = notional / p
            fill = p * (1 - slip)
            cash += qty * fill * (1 - fee)
            units -= qty
            side = "sell"
        if abs(cash) < 1e-8:
            cash = 0.0
        if abs(units) < 1e-12:
            units = 0.0
        assert cash >= -1e-7 and units >= -1e-10
        events.append(
            {
                "index": i,
                "time_utc": iso(times[i]) if times is not None else None,
                "side": side,
                "reference_price": p,
                "fill_price": fill,
                "quantity_btc": qty,
                "fee_usdt": qty * fill * fee,
                "slippage_usdt": qty * abs(fill - p),
                "equity_before": pre,
                "equity_after": cash + units * p,
                "cash_after": cash,
                "btc_after": units,
                "target_weight": float(target),
                "terminal_liquidation": terminal,
            }
        )

    for i, target in enumerate(weights):
        transact(i, float(target))
        value = cash + units * prices[i + 1]
        equity.append(float(value))
    transact(n, 0, terminal=True)
    equity[-1] = float(cash)
    equity = np.array(equity)
    returns = equity[1:] / equity[:-1] - 1
    independent = independent_returns(prices, weights, fee, slip)
    if not np.allclose(returns, independent, atol=1e-12, rtol=1e-10):
        raise AssertionError("Independent ledger mismatch")
    return {"returns": returns, "equity": equity, "events": events}


def independent_returns(prices, weights, fee, slip):
    """Independent weight/return recurrence: no BTC quantities or cash ledger."""
    buys = (1 + slip) * (1 + fee) - 1
    sells = 1 - (1 - slip) * (1 - fee)
    old = 0.0
    out = []
    for p, next_p, target in zip(prices[:-1], prices[1:], weights, strict=True):
        if target >= old:
            turnover = (target - old) / (1 + target * buys)
            trade_factor = 1 - buys * turnover
        else:
            turnover = (old - target) / (1 - target * sells)
            trade_factor = 1 - sells * turnover
        ratio = next_p / p
        holding_factor = 1 + target * (ratio - 1)
        out.append(trade_factor * holding_factor - 1)
        old = target * ratio / holding_factor
    out[-1] = (1 + out[-1]) * (1 - sells * old) - 1
    return np.array(out)


def sharpe(returns):
    sd = np.std(returns, ddof=1)
    return float(np.mean(returns) / sd * np.sqrt(365)) if sd > 1e-14 else 0.0


def metrics(result):
    r, eq = result["returns"], result["equity"]
    dd = eq / np.maximum.accumulate(eq) - 1
    peak, longest, ongoing = eq[0], 0, 0
    for value in eq[1:]:
        if value >= peak:
            peak, ongoing = value, 0
        else:
            ongoing += 1
            longest = max(longest, ongoing)
    buys = [e for e in result["events"] if e["side"] == "buy"]
    return {
        "days": len(r),
        "total_return": float(eq[-1] / eq[0] - 1),
        "cagr": float((eq[-1] / eq[0]) ** (365 / len(r)) - 1),
        "annualized_volatility": float(np.std(r, ddof=1) * np.sqrt(365)),
        "sharpe_cash_yield_zero": sharpe(r),
        "max_drawdown_daily_sampled": float(dd.min()),
        "longest_underwater_days_including_unrecovered": longest,
        "buy_events": len(buys),
        "total_fees_usdt": sum(e["fee_usdt"] for e in result["events"]),
        "total_slippage_usdt": sum(e["slippage_usdt"] for e in result["events"]),
        "ending_equity_usdt": float(eq[-1]),
    }


def episodes(events):
    out, entry = [], None
    for e in events:
        if e["side"] == "buy":
            assert entry is None
            entry = e
        else:
            assert entry is not None
            out.append(
                {
                    "entry_utc": entry["time_utc"],
                    "exit_utc": e["time_utc"],
                    "entry_reference": entry["reference_price"],
                    "exit_reference": e["reference_price"],
                    "entry_fill": entry["fill_price"],
                    "exit_fill": e["fill_price"],
                    "entry_quantity_btc": entry["quantity_btc"],
                    "net_pnl_usdt": e["equity_after"] - entry["equity_before"],
                    "net_return": e["equity_after"] / entry["equity_before"] - 1,
                    "holding_days": e["index"] - entry["index"],
                    "fees_usdt": e["fee_usdt"] + entry["fee_usdt"],
                    "slippage_usdt": e["slippage_usdt"] + entry["slippage_usdt"],
                    "entry_at_split_boundary": entry["index"] == 0,
                    "administrative_exit": e["terminal_liquidation"],
                }
            )
            entry = None
    assert entry is None
    return out


def bootstrap(a, b, length, reps=5000, seed=20261007):
    rng = np.random.default_rng(seed)
    n = len(a)
    starts = rng.integers(0, n, size=(reps, math.ceil(n / length)))
    offsets = np.arange(length)
    values = []
    for begin in range(0, reps, 250):
        idx = ((starts[begin : begin + 250, :, None] + offsets) % n).reshape(
            -1, starts.shape[1] * length
        )[:, :n]
        aa, bb = a[idx], b[idx]
        sa = aa.mean(axis=1) / aa.std(axis=1, ddof=1) * np.sqrt(365)
        sb = bb.mean(axis=1) / bb.std(axis=1, ddof=1) * np.sqrt(365)
        values.extend((sa - sb).tolist())
    np.savez_compressed(
        ROOT / f"bootstrap-draws-block{length}.npz",
        block_starts=starts,
        sharpe_differences=np.array(values),
    )
    return {
        "block_days": length,
        "repetitions": reps,
        "seed": seed,
        "point_difference": sharpe(a) - sharpe(b),
        "ci95": np.quantile(values, [0.025, 0.975]).tolist(),
    }


def shift_control(prices, weights, fee, slip):
    records = []
    for shift in range(len(weights)):
        w = np.roll(weights, shift)
        prev = np.r_[0, w[:-1]]
        factors = np.ones(len(w))
        factors[(w == 1) & (prev == 0)] = 1 / ((1 + slip) * (1 + fee))
        factors[(w == 0) & (prev == 1)] = (1 - slip) * (1 - fee)
        factors *= 1 + w * (prices[1:] / prices[:-1] - 1)
        if w[-1] == 1:
            factors[-1] *= (1 - slip) * (1 - fee)
        returns = factors - 1
        records.append(
            {
                "shift": shift,
                "invested_days": int(w.sum()),
                "entries": int(np.sum((w == 1) & (prev == 0))),
                "sharpe": sharpe(returns),
                "total_return": float(np.prod(factors) - 1),
            }
        )
    write("circular-shift-trials.json", records)
    actual = records[0]["sharpe"]
    return {
        "method": "Exhaustive circular shifts, not iid independent tests",
        "nonzero_shifts": len(records) - 1,
        "actual_sharpe": actual,
        "shifted_median_sharpe": float(np.median([r["sharpe"] for r in records[1:]])),
        "p_ge_actual": (1 + sum(r["sharpe"] >= actual for r in records[1:])) / len(records),
    }
