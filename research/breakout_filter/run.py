"""Offline, isolated SMA200 entry-filter experiment. Never imports production code."""

import argparse
import hashlib
import json
import math
import platform
import shutil
import sys
from pathlib import Path

import baseline as b
import numpy as np

HERE = Path(__file__).resolve().parent


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_protocol():
    freeze = json.loads((HERE / "protocol-freeze.json").read_text())
    if digest(HERE / "protocol.json") != freeze["protocol_sha256"]:
        raise ValueError("Research protocol changed after freeze")
    return json.loads((HERE / "protocol.json").read_text())


def prepare_input(source, output, protocol):
    """Copy only hash-verified data, never execute code supplied in the datapack."""
    expected = {
        "protocol.json": protocol["source_protocol_sha256"],
        "data-manifest.json": protocol["source_manifest_sha256"],
        **protocol["source_amendments"],
    }
    for name, sha in expected.items():
        if digest(source / name) != sha:
            raise ValueError(f"Wrong or modified frozen input: {name}")
    rawdir = output / "input" / "raw"
    rawdir.mkdir(parents=True)
    for name in expected:
        shutil.copyfile(source / name, rawdir.parent / name)
    # Generate these from the pinned hashes, rather than trusting incoming sidecars.
    (rawdir.parent / "protocol-freeze.json").write_text(
        json.dumps({"protocol_sha256": expected["protocol.json"]})
    )
    for name, sha in protocol["source_amendments"].items():
        (rawdir.parent / name.replace(".json", ".sha256")).write_text(sha)
    for record in json.loads((source / "data-manifest.json").read_text()):
        if "error" in record:
            raise ValueError("Frozen source contains a failed download")
        name = record["name"] + ".json"
        if Path(name).name != name:
            raise ValueError("Unsafe raw filename")
        path = source / "raw" / name
        if digest(path) != record["sha256"]:
            raise ValueError(f"Raw data hash mismatch: {name}")
        shutil.copyfile(path, rawdir / name)
    return rawdir.parent


def features(daily, times, sma_days=200, atr_days=14):
    """Only fully completed nominal UTC days; ATR is simple mean, not Wilder EMA."""
    rows = []
    for time in times:
        midnight = int(time) // b.DAY * b.DAY
        dates = range(midnight - max(sma_days, atr_days + 1) * b.DAY, midnight, b.DAY)
        if any(t not in daily for t in dates):
            raise ValueError("Missing completed feature bar")
        closes = [float(daily[t][4]) for t in dates]
        true_ranges = []
        for t in range(midnight - atr_days * b.DAY, midnight, b.DAY):
            high, low = map(float, daily[t][2:4])
            previous = float(daily[t - b.DAY][4])
            true_ranges.append(max(high - low, abs(high - previous), abs(low - previous)))
        rows.append(
            {
                "scheduled_utc": b.iso(time),
                "last_input_nominal_close_utc": b.iso(midnight - 1),
                "close": closes[-1],
                "sma": math.fsum(closes[-sma_days:]) / sma_days,
                "atr": math.fsum(true_ranges) / atr_days,
            }
        )
    return rows


def choose(rows, multiple):
    """One decision per baseline episode; rejected episode cannot re-enter later."""
    previous = False
    accepted = False
    weights, observations = [], []
    for i, row in enumerate(rows):
        active = row["close"] > row["sma"]
        start = active and not previous
        if start:
            accepted = row["close"] - row["sma"] > multiple * row["atr"]
        if not active:
            accepted = False
        weights.append(int(active and accepted))
        observations.append(
            {
                **row,
                "baseline_active": active,
                "episode_start": start,
                "boundary_entry": start and i == 0,
                "filter_pass_at_entry": accepted if start else None,
                "target": weights[-1],
            }
        )
        previous = active
    return np.array(weights), observations


def segments(weights):
    out = []
    start = None
    for i, value in enumerate([*weights, 0]):
        if value and start is None:
            start = i
        if not value and start is not None:
            out.append((start, i))
            start = None
    return out


def random_control(prices, base, selected, fee, slip, draws, seed):
    spans = segments(base)
    kept = len(segments(selected))
    actual = b.sharpe(b.independent_returns(prices, selected, fee, slip))
    rng = np.random.default_rng(seed)
    trials = []
    for _ in range(draws):
        ids = sorted(rng.choice(len(spans), size=kept, replace=False).tolist())
        weights = np.zeros(len(base))
        for index in ids:
            start, end = spans[index]
            weights[start:end] = 1
        returns = b.independent_returns(prices, weights, fee, slip)
        trials.append(
            {
                "episode_ids": ids,
                "sharpe": b.sharpe(returns),
                "total_return": float(np.prod(1 + returns) - 1),
                "invested_days": int(weights.sum()),
            }
        )
    return {
        "accepted_episodes": kept,
        "baseline_episodes": len(spans),
        "actual_sharpe": actual,
        "p_ge_actual": (1 + sum(t["sharpe"] >= actual for t in trials)) / (1 + draws),
        "seed": seed,
        "trials": trials,
        "limitation": "Exact episode count, not matched holding duration/exposure; descriptive",
    }


def bootstrap(a, base, seed):
    rng = np.random.default_rng(seed)
    n, length, reps = len(a), 30, 5000
    starts = rng.integers(0, n, (reps, math.ceil(n / length)))
    values = []
    undefined = 0
    for block in starts:
        idx = ((block[:, None] + np.arange(length)) % n).ravel()[:n]
        x, y = a[idx], base[idx]
        if np.std(x, ddof=1) <= 1e-14 or np.std(y, ddof=1) <= 1e-14:
            undefined += 1
            continue
        values.append(b.sharpe(x) - b.sharpe(y))
    # Do not hide undefined all-cash samples behind Sharpe=0.
    ci = np.quantile(values, [0.025, 0.975]).tolist() if values and not undefined else None
    return (
        {
            "ci95": ci,
            "undefined_draws": undefined,
            "draws": reps,
            "seed": seed,
            "point_difference": b.sharpe(a) - b.sharpe(base),
        },
        starts,
        np.array(values),
    )


def assess(reports):
    main = reports["historical_test_already_seen"]
    a, filt = main["costs"]["1"]["baseline"], main["costs"]["1"]["filtered"]
    ci = main["bootstrap"]["ci95"]
    gates = {
        "positive_base_and_double_cagr": all(
            main["costs"][str(c)]["filtered"]["cagr"] > 0 for c in (1, 2)
        ),
        "all_main_splits_sharpe_improved": all(
            reports[s]["costs"]["1"]["filtered"]["sharpe_cash_yield_zero"]
            > reports[s]["costs"]["1"]["baseline"]["sharpe_cash_yield_zero"]
            for s in ("development", "validation", "historical_test_already_seen")
        ),
        "drawdown_no_worse": filt["max_drawdown_daily_sampled"] >= a["max_drawdown_daily_sampled"],
        "bootstrap_lower_positive": ci is not None and ci[0] > 0,
        "random_p_at_most_005": main["random_control"]["p_ge_actual"] <= 0.05,
        "natural_completed_episodes_at_least_20": main["natural_completed_episodes"] >= 20,
    }
    if not gates["natural_completed_episodes_at_least_20"]:
        verdict = "INSUFFICIENT_EVIDENCE"
    elif all(gates.values()):
        verdict = "CONSIDER_PROSPECTIVE_PAPER_ONLY"
    else:
        verdict = "STOP_THIS_HYPOTHESIS"
    return {
        "verdict": verdict,
        "gates": gates,
        "production_authorized": False,
        "unseen_holdout": False,
        "status": "RESEARCH_ONLY",
    }


def run(source, output):
    if sys.flags.optimize:
        raise ValueError("Do not use python -O: baseline contains verification assertions")
    protocol = load_protocol()
    # Refuse all overwrites, including partial prior runs. Keep originals untouched.
    output.mkdir(parents=True, exist_ok=False)
    b.ROOT = prepare_input(source, output, protocol)
    cfg = json.loads((b.ROOT / "protocol.json").read_text())
    scheduled, actual, prices, base, quality = b.prepare(cfg)
    daily = {r[0]: r for r in b.read_rows("binance-1d")}
    rows = features(daily, scheduled[:-1], protocol["sma_days"], protocol["atr_days"])
    b.ROOT = output
    for name in ("protocol.json", "protocol-freeze.json"):
        shutil.copyfile(HERE / name, output / name)
    reports = {}
    splits = dict(protocol["splits"])
    splits.update({f"year_{y}": [f"{y}-01-01", f"{y + 1}-01-01"] for y in (2023, 2024, 2025)})
    splits["year_2026_partial"] = ["2026-01-01", "2026-10-06"]
    risk_weight = None
    for split, (start, end) in splits.items():
        lo = scheduled.tolist().index(b.stamp(start + "T01:00:00+00:00"))
        hi = scheduled.tolist().index(b.stamp(end + "T01:00:00+00:00"))
        p, t, original = prices[lo : hi + 1], actual[lo : hi + 1], base[lo:hi]
        selected, observations = choose(rows[lo:hi], protocol["atr_multiple"])
        b.write(f"{split}-decisions.json", observations)
        report = {"start": start, "end": end, "costs": {}}
        models = {"baseline": original, "filtered": selected, "buy_hold": np.ones(len(original))}
        if risk_weight is None:
            r = b.simulate(p, original, 0.001, 0.0005)["returns"]
            h = b.simulate(p, np.ones(len(original)), 0.001, 0.0005)["returns"]
            risk_weight = min(1.0, float(np.std(r, ddof=1) / np.std(h, ddof=1)))
        models["btc_cash"] = np.full(len(original), risk_weight)
        for cost in protocol["cost_multipliers"]:
            report["costs"][str(cost)] = {}
            for model, weights in models.items():
                result = b.simulate(p, weights, 0.001 * cost, 0.0005 * cost, times=t)
                report["costs"][str(cost)][model] = b.metrics(result)
                b.write(f"{split}-{model}-cost{cost}-ledger.json", result["events"])
                b.write(
                    f"{split}-{model}-cost{cost}-equity.json",
                    {
                        "times": [b.iso(x) for x in t],
                        "equity": result["equity"].tolist(),
                        "returns": result["returns"].tolist(),
                        "note": "Initial capital then next reference valuations; final liquidated",
                    },
                )
                if model in ("baseline", "filtered"):
                    trades = b.episodes(result["events"])
                    b.write(f"{split}-{model}-cost{cost}-episodes.json", trades)
                    if cost == 1 and model == "baseline":
                        base_result, base_trades = result, trades
                    elif cost == 1 and model == "filtered":
                        filtered_result, filtered_trades = result, trades
        rejected = [
            trade
            for trade, (entry, _) in zip(base_trades, segments(original), strict=True)
            if selected[entry] == 0
        ]
        report["rejected_opportunities"] = {
            "count": len(rejected),
            "profitable_missed": sum(x["net_return"] > 0 for x in rejected),
            "losing_avoided": sum(x["net_return"] < 0 for x in rejected),
            "baseline_trade_returns": [x["net_return"] for x in rejected],
            "note": "Per-trade percentage outcomes, not additive portfolio PnL attribution",
        }
        report["natural_completed_episodes"] = sum(
            not x["administrative_exit"] and not x["entry_at_split_boundary"]
            for x in filtered_trades
        )
        if split in protocol["splits"]:
            control = random_control(
                p, original, selected, 0.001, 0.0005, protocol["random_draws"], protocol["seed"]
            )
            b.write(f"{split}-random-trials.json", control.pop("trials"))
            report["random_control"] = control
            stats, starts, values = bootstrap(
                filtered_result["returns"], base_result["returns"], protocol["seed"]
            )
            np.savez_compressed(
                output / f"{split}-bootstrap.npz", starts=starts, differences=values
            )
            report["bootstrap"] = stats
        reports[split] = report
    results = {
        "study_id": protocol["study_id"],
        "decision": assess(reports),
        "btc_cash_weight_from_development_baseline": risk_weight,
        "splits": reports,
        "data_quality": quality,
    }
    b.write("results.json", results)
    b.write(
        "provenance.json",
        {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "source_manifest_sha256": protocol["source_manifest_sha256"],
            "code_sha256": {n: digest(HERE / n) for n in ("baseline.py", "run.py", "uv.lock")},
        },
    )
    lines = [
        "# BTC 突破过滤：探索性研究",
        "",
        f"结论：{results['decision']['verdict']}",
        "",
        "历史已查看；不代表新的样本外验证；不授权信号、纸面服务或实盘部署。",
        "",
        "|区间|原策略净收益|过滤版净收益|过滤版回撤|接受交易数|",
        "|---|---:|---:|---:|---:|",
    ]
    for split, report in reports.items():
        a, f = report["costs"]["1"]["baseline"], report["costs"]["1"]["filtered"]
        lines.append(
            f"|{split}|{a['total_return']:.2%}|{f['total_return']:.2%}|"
            f"{f['max_drawdown_daily_sampled']:.2%}|{f['buy_events']}|"
        )
    lines.extend(
        [
            "",
            "完整成本压力、随机对照、置信区间、机会损失与全部失败门槛见 results.json。",
            "阈值只有一个；没有按结果选择参数。云端重复运行仅证明可复现性。",
        ]
    )
    (output / "研究结果.md").write_text("\n".join(lines) + "\n")
    b.write(
        "SHA256SUMS.json",
        {
            str(p.relative_to(output)): digest(p)
            for p in sorted(output.rglob("*"))
            if p.is_file() and p.name != "SHA256SUMS.json"
        },
    )
    print(json.dumps(results["decision"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--datapack", type=Path, required=True, help="Extracted original v1 data folder"
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="New, nonexistent result directory"
    )
    args = parser.parse_args()
    run(args.datapack.resolve(), args.output.resolve())
