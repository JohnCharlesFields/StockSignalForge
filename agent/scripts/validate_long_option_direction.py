#!/usr/bin/env python3
"""Offline, causal stock-direction validation for the long-option research board.

This cannot estimate option P/L: historical contract quotes and IV are absent.
The cohort is fixed today, not a point-in-time leader universe.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

AGENT = Path(__file__).resolve().parents[1]
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

from consensus_signal_service import consensus_feature_frame  # noqa: E402
from cost_model import equity_round_trip_cost  # noqa: E402
from market_data_service import external_data_scope, get_daily_history  # noqa: E402

SYMBOLS = (
    "AAPL", "AMD", "AMZN", "GOOGL", "INTC", "MARA", "META", "MSFT", "MSTR",
    "MU", "NFLX", "NVDA", "ORCL", "PLTR", "SMCI", "SOFI", "TSLA",
)
HORIZONS = (1, 3, 5, 10)
PRIMARY = 5
RULES = ("baseline", "change_3d", "rs20_align", "change_and_rs20")
MIN_VALIDATION = 30
MIN_TEST = 60


def split_for(day: date, exit_day: date) -> str | None:
    """Disjoint calendar windows; the outcome must finish within its window."""
    if date(2022, 1, 3) <= day <= date(2023, 12, 15) and exit_day <= date(2023, 12, 29):
        return "train"
    if date(2024, 1, 2) <= day <= date(2024, 12, 13) and exit_day <= date(2024, 12, 31):
        return "validation"
    if date(2025, 1, 2) <= day <= date(2026, 9, 9) and exit_day <= date(2026, 9, 25):
        return "test"
    return None


def selected_rules(side: int, net: float, net_3d_ago: float, rs20: float) -> dict[str, bool]:
    """Predetermined filters. Zero/missing data never count as confirmation."""
    baseline = side in (-1, 1) and np.isfinite(net) and side * net > 0.12
    change = baseline and np.isfinite(net_3d_ago) and side * (net - net_3d_ago) > 0
    rs = baseline and np.isfinite(rs20) and side * rs20 > 0
    return {"baseline": baseline, "change_3d": change,
            "rs20_align": rs, "change_and_rs20": change and rs}


def directional_outcome(side: int, entry: float, exit_price: float, cost: float) -> tuple[float, float]:
    """Signed underlying move, not the return on a long Call or Put."""
    if side not in (-1, 1) or not all(np.isfinite(x) and x > 0 for x in (entry, exit_price)):
        raise ValueError("invalid side or equity price")
    gross = side * (exit_price / entry - 1.0)
    return gross, gross - cost


def valid_price_window(stock: pd.DataFrame, start: int, stop: int) -> bool:
    """Reject malformed bars and likely unadjusted splits near the trade path."""
    bars = stock.iloc[start:stop + 1]
    if bars.empty or bars[list(("Open", "High", "Low", "Close", "Volume"))].isna().any().any():
        return False
    if (bars[["Open", "High", "Low", "Close"]] <= 0).any().any() or (bars["Volume"] <= 0).any():
        return False
    if (bars["High"] < bars[["Open", "Close", "Low"]].max(axis=1)).any():
        return False
    if (bars["Low"] > bars[["Open", "Close", "High"]].min(axis=1)).any():
        return False
    return bool(bars["Close"].pct_change().abs().fillna(0).lt(0.50).all())


def _history(symbol: str) -> pd.DataFrame | None:
    frame, _ = get_daily_history(symbol, period="5y", allow_yfinance_fallback=False)
    if frame is None or len(frame) < 240:
        return None
    cols = ["Open", "High", "Low", "Close", "Volume"]
    frame = frame[cols].apply(pd.to_numeric, errors="coerce").sort_index()
    frame.index = pd.to_datetime(frame.index).normalize()
    return frame.loc[~frame.index.duplicated(keep="last")]


def collect_events(histories: dict[str, pd.DataFrame]) -> tuple[list[dict], dict]:
    benchmark = histories["SPY"]
    benchmark_dates = set(benchmark.index)
    events: list[dict] = []
    audit: dict[str, dict] = {}
    cost = equity_round_trip_cost()
    for symbol, stock in histories.items():
        if symbol == "SPY":
            continue
        feature = consensus_feature_frame(stock, benchmark)
        net_values = feature["net_consensus"].to_numpy()
        rs_values = feature["rs20"].to_numpy()
        last_event = -100
        counts = {"bars": len(stock), "first": str(stock.index[0].date()),
                  "last": str(stock.index[-1].date()), "signals": 0,
                  "missing_benchmark": 0, "bad_price_window": 0}
        for i in range(220, len(stock) - max(HORIZONS)):
            net = float(net_values[i])
            side = 1 if net > 0.12 else -1 if net < -0.12 else 0
            if not side or i - last_event < 10:
                continue
            signal_day = stock.index[i]
            exit_day = stock.index[i + max(HORIZONS)]
            split = split_for(signal_day.date(), exit_day.date())
            if split is None:
                continue
            needed = [signal_day, stock.index[i + 1], *(stock.index[i + h] for h in HORIZONS)]
            if any(day not in benchmark_dates for day in needed):
                counts["missing_benchmark"] += 1
                continue
            # A raw split can distort MA200/RS20 long after the split day itself.
            if not valid_price_window(stock, i - 200, i + max(HORIZONS)):
                counts["bad_price_window"] += 1
                continue
            entry = float(stock["Open"].iloc[i + 1])
            bench_entry = float(benchmark.at[stock.index[i + 1], "Open"])
            if not np.isfinite(bench_entry) or bench_entry <= 0:
                counts["missing_benchmark"] += 1
                continue
            event = {"symbol": symbol, "signal_date": signal_day.date().isoformat(),
                     "split": split, "side": "Call" if side == 1 else "Put",
                     "net_consensus": round(net, 6), "rs20": round(float(rs_values[i]), 6),
                     "rules": selected_rules(side, net, float(net_values[i - 3]), float(rs_values[i]))}
            for horizon in HORIZONS:
                gross, net_proxy = directional_outcome(side, entry, float(stock["Close"].iloc[i + horizon]), cost)
                benchmark_return = float(benchmark.at[stock.index[i + horizon], "Close"]) / bench_entry - 1
                event[f"gross_{horizon}"] = gross
                event[f"net_{horizon}"] = net_proxy
                event[f"alpha_{horizon}"] = side * (gross * side - benchmark_return)
            events.append(event)
            counts["signals"] += 1
            last_event = i
        audit[symbol] = counts
    return events, {"symbols": audit, "equity_round_trip_cost_fraction": cost,
                    "signal_rule": "abs(net_consensus)>0.12; >=10 stock sessions between events"}


def _mean(rows: list[dict], key: str) -> float | None:
    return float(np.mean([row[key] for row in rows])) if rows else None


def summarize(rows: list[dict], rule: str, horizon: int, *, bootstrap: int = 500) -> dict:
    chosen = [row for row in rows if row["rules"][rule]]
    base_mean = _mean(rows, f"net_{horizon}")
    result = {"n": len(chosen), "baseline_n": len(rows),
              "coverage": len(chosen) / len(rows) if rows else None,
              "symbols": len({row["symbol"] for row in chosen}),
              "call_n": sum(row["side"] == "Call" for row in chosen),
              "put_n": sum(row["side"] == "Put" for row in chosen),
              "hit_net": _mean([{**row, "hit": float(row[f"net_{horizon}"] > 0)} for row in chosen], "hit"),
              "mean_net_stock_proxy": _mean(chosen, f"net_{horizon}"),
              "mean_signed_spy_excess": _mean(chosen, f"alpha_{horizon}"),
              "delta_net_vs_baseline": None, "ci95_delta": None}
    result["by_side"] = {
        side: {"n": len(group), "hit_net": sum(row[f"net_{horizon}"] > 0 for row in group) / len(group) if group else None,
               "mean_net_stock_proxy": _mean(group, f"net_{horizon}")}
        for side in ("Call", "Put")
        for group in [[row for row in chosen if row["side"] == side]]
    }
    if not chosen or not rows:
        return result
    result["delta_net_vs_baseline"] = result["mean_net_stock_proxy"] - base_mean
    if rule == "baseline" or bootstrap <= 0 or len(chosen) < 25:
        return result
    clusters: dict[str, list[dict]] = {}
    for row in rows:
        clusters.setdefault(row["signal_date"][:7], []).append(row)
    keys = sorted(clusters)
    if len(keys) < 6:
        return result
    rng = random.Random(173)
    deltas = []
    for _ in range(bootstrap):
        sample = [row for month in (rng.choice(keys) for _ in keys) for row in clusters[month]]
        selected = [row for row in sample if row["rules"][rule]]
        if selected:
            deltas.append(_mean(selected, f"net_{horizon}") - _mean(sample, f"net_{horizon}"))
    if len(deltas) >= 100:
        result["ci95_delta"] = [float(np.quantile(deltas, q)) for q in (0.025, 0.975)]
    return result


def run(symbols: tuple[str, ...] = SYMBOLS) -> dict:
    histories: dict[str, pd.DataFrame] = {}
    with external_data_scope(False):
        for symbol in ("SPY", *symbols):
            frame = _history(symbol)
            if frame is not None:
                histories[symbol] = frame
    if "SPY" not in histories:
        return {"status": "missing_spy", "available_symbols": sorted(histories)}
    events, audit = collect_events(histories)
    by_split = {split: [row for row in events if row["split"] == split]
                for split in ("train", "validation", "test")}
    validation = {rule: summarize(by_split["validation"], rule, PRIMARY) for rule in RULES}
    qualifying = [rule for rule in RULES[1:] if validation[rule]["n"] >= MIN_VALIDATION
                  and validation[rule]["delta_net_vs_baseline"] is not None
                  and validation[rule]["delta_net_vs_baseline"] > 0]
    selected = max(qualifying, key=lambda rule: validation[rule]["delta_net_vs_baseline"]) if qualifying else "baseline"
    test = {rule: {str(h): summarize(by_split["test"], rule, h) for h in HORIZONS}
            for rule in ("baseline", selected) if rule == "baseline" or selected != "baseline"}
    primary_test = test[selected][str(PRIMARY)]
    eligible_promotion = bool(selected != "baseline" and primary_test["n"] >= MIN_TEST
                              and primary_test["symbols"] >= 5
                              and primary_test["ci95_delta"] is not None
                              and primary_test["ci95_delta"][0] > 0
                              and primary_test["mean_net_stock_proxy"] > 0)
    return {
        "status": "completed", "generated_at": datetime.now(timezone.utc).isoformat(),
        "cohort": list(symbols), "available_symbols": sorted(set(histories) - {"SPY"}),
        "missing_symbols": sorted(set(symbols) - set(histories)), "data_as_of": str(histories["SPY"].index[-1].date()),
        "events_by_split": {name: len(rows) for name, rows in by_split.items()},
        "primary_horizon_sessions": PRIMARY, "horizons": HORIZONS,
        "validation": validation, "selected_rule_locked_before_test": selected,
        "test": test, "eligible_for_live_pilot": eligible_promotion,
        "audit": audit,
        "limits": ["Only cached daily underlying OHLCV; no historical option quotes, IV, Greeks or option P/L.",
                   "Today-fixed cohort is not a point-in-time leader universe; survivorship/selection bias remains.",
                   "Signed stock returns for Put are directional proxies, not executable put returns.",
                   "Split/corporate-action adjustments and provider provenance cannot be fully verified from cache CSV.",
                   "Rules and validation selection are exploratory; CI is month-block bootstrap, not proof of profit."],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write JSON audit report")
    args = parser.parse_args()
    report = run()
    payload = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()
