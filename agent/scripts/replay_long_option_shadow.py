#!/usr/bin/env python3
"""Retrospective counterpart to the live long-option direction ledger.

This reads cached underlying OHLCV only. It never writes to the forward ledger
and cannot estimate actual option P/L without historical contract quotes.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

import pandas as pd

AGENT = Path(__file__).resolve().parents[1]
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

from consensus_signal_service import consensus_feature_frame  # noqa: E402
from cost_model import equity_round_trip_cost  # noqa: E402
from long_option_shadow_service import HORIZONS, MODEL_VERSION, PRIMARY_HORIZON  # noqa: E402
from market_data_service import external_data_scope  # noqa: E402
from scripts.validate_long_option_direction import SYMBOLS, _history, valid_price_window  # noqa: E402


def _phase(signal_day: date, exit_day: date) -> str | None:
    if date(2022, 1, 1) <= signal_day < date(2024, 1, 1) and exit_day < date(2024, 1, 1):
        return "exploration_2022_2023"
    if date(2024, 1, 1) <= signal_day < date(2025, 1, 1) and exit_day < date(2025, 1, 1):
        return "validation_2024"
    if signal_day >= date(2025, 1, 1):
        return "retrospective_2025_plus_previously_inspected"
    return None


def _direction(net: float) -> str:
    if not math.isfinite(net):
        return "WAIT"
    rounded = round(net, 4)
    return "C" if rounded > 0.12 else "P" if rounded < -0.12 else "WAIT"


def _price_frame(frame: pd.DataFrame) -> pd.DataFrame:
    cols = ("Open", "High", "Low", "Close", "Volume")
    if frame is None or frame.empty or not set(cols).issubset(frame.columns):
        return pd.DataFrame()
    clean = frame[list(cols)].apply(pd.to_numeric, errors="coerce").sort_index()
    clean.index = pd.to_datetime(clean.index).normalize()
    return clean.loc[~clean.index.duplicated(keep="last")]


def collect_replay_events(histories: dict[str, pd.DataFrame], *,
                          start: date | None = None, end: date | None = None
                          ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Compute each signal from T-or-earlier data, then settle T+1 through T+h."""
    benchmark = _price_frame(histories.get("SPY"))
    if benchmark.empty:
        return [], {"reason": "missing_spy", "symbols": {}}
    benchmark_dates = set(benchmark.index)
    cost = equity_round_trip_cost()
    events: list[dict[str, Any]] = []
    audit: dict[str, dict[str, Any]] = {}
    for symbol, raw in sorted(histories.items()):
        if symbol == "SPY":
            continue
        stock = _price_frame(raw)
        counts = {"bars": len(stock), "first": str(stock.index[0].date()) if len(stock) else None,
                  "last": str(stock.index[-1].date()) if len(stock) else None,
                  "eligible_days": 0, "missing_spy": 0, "invalid_price_path": 0,
                  "missing_signal": 0, "outside_window": 0,
                  "by_direction": {"C": 0, "P": 0, "WAIT": 0}}
        audit[symbol] = counts
        if len(stock) < 220 + max(HORIZONS):
            continue
        features = consensus_feature_frame(stock, benchmark)
        if features.empty or "net_consensus" not in features:
            continue
        for i in range(219, len(stock) - max(HORIZONS)):
            signal_day = stock.index[i]
            exit_day = stock.index[i + max(HORIZONS)]
            phase = _phase(signal_day.date(), exit_day.date())
            if (phase is None or (start and signal_day.date() < start)
                    or (end and signal_day.date() > end)):
                counts["outside_window"] += 1
                continue
            entry_day = stock.index[i + 1]
            needed = (signal_day, entry_day, *(stock.index[i + h] for h in HORIZONS))
            if any(day not in benchmark_dates for day in needed):
                counts["missing_spy"] += 1
                continue
            if not valid_price_window(stock, i - 200, i + max(HORIZONS)):
                counts["invalid_price_path"] += 1
                continue
            entry = float(stock["Open"].iloc[i + 1])
            spy_entry = float(benchmark.at[entry_day, "Open"])
            if not all(math.isfinite(value) and value > 0 for value in (entry, spy_entry)):
                counts["invalid_price_path"] += 1
                continue
            net = float(features["net_consensus"].iloc[i])
            if not math.isfinite(net):
                counts["missing_signal"] += 1
                continue
            direction = _direction(net)
            side = {"C": 1, "P": -1, "WAIT": 0}[direction]
            row: dict[str, Any] = {
                "symbol": symbol, "signal_date": signal_day.date().isoformat(),
                "entry_date": entry_day.date().isoformat(), "entry_open": entry,
                "direction": direction, "net_consensus": round(net, 4), "phase": phase,
            }
            for horizon in HORIZONS:
                exit_index = i + horizon
                exit_date = stock.index[exit_index]
                underlying = float(stock["Close"].iloc[exit_index]) / entry - 1.0
                spy_return = float(benchmark.at[exit_date, "Close"]) / spy_entry - 1.0
                if not all(math.isfinite(value) for value in (underlying, spy_return)):
                    break
                row[f"exit_date_{horizon}"] = exit_date.date().isoformat()
                row[f"underlying_return_{horizon}"] = underlying
                row[f"signed_net_{horizon}"] = side * underlying - cost if side else None
                row[f"signed_spy_excess_{horizon}"] = side * (underlying - spy_return) - cost if side else None
                row[f"policy_net_{horizon}"] = side * underlying - cost if side else 0.0
                row[f"always_call_net_{horizon}"] = underlying - cost
            else:
                events.append(row)
                counts["eligible_days"] += 1
                counts["by_direction"][direction] += 1
                continue
            counts["invalid_price_path"] += 1
    return events, {"symbols": audit, "equity_round_trip_cost_fraction": cost,
                    "signal_rule": "current rounded net consensus: C>0.12, P<-0.12, else WAIT; every eligible stock-day"}


def summarize(events: list[dict[str, Any]], horizon: int, *, bootstrap: int = 500) -> dict[str, Any]:
    if horizon not in HORIZONS:
        raise ValueError("unsupported horizon")
    directional = [row for row in events if row["direction"] != "WAIT"]
    waiting = [row for row in events if row["direction"] == "WAIT"]
    key = f"signed_net_{horizon}"
    delta = [row[f"policy_net_{horizon}"] - row[f"always_call_net_{horizon}"] for row in events]
    by_month: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in events:
        by_month[row["signal_date"][:7]].append(row)
    ci_delta = ci_policy = ci_directional_hit = None
    if len(by_month) >= 6 and bootstrap > 0:
        months = sorted(by_month)
        rng = random.Random(173)
        month_totals = {}
        for month, rows in by_month.items():
            active = [row for row in rows if row["direction"] != "WAIT"]
            month_totals[month] = (
                len(rows),
                sum(row[f"policy_net_{horizon}"] for row in rows),
                sum(row[f"policy_net_{horizon}"] - row[f"always_call_net_{horizon}"] for row in rows),
                len(active),
                sum(row[key] > 0 for row in active),
            )
        draws_delta, draws_policy, draws_hit = [], [], []
        for _ in range(bootstrap):
            sampled = [month_totals[rng.choice(months)] for _ in months]
            count = sum(item[0] for item in sampled)
            active_count = sum(item[3] for item in sampled)
            draws_delta.append(sum(item[2] for item in sampled) / count)
            draws_policy.append(sum(item[1] for item in sampled) / count)
            if active_count:
                draws_hit.append(sum(item[4] for item in sampled) / active_count)

        def interval(draws: list[float]) -> list[float] | None:
            if not draws:
                return None
            draws.sort()
            return [draws[int(0.025 * len(draws))], draws[int(0.975 * len(draws)) - 1]]

        ci_delta = interval(draws_delta)
        ci_policy = interval(draws_policy)
        ci_directional_hit = interval(draws_hit)

    def side_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
        values = [row[key] for row in rows]
        return {"n": len(rows), "hit_net": mean(value > 0 for value in values) if values else None,
                "mean_signed_net": mean(values) if values else None,
                "mean_signed_spy_excess": mean(row[f"signed_spy_excess_{horizon}"] for row in rows)
                if rows else None}

    return {
        "candidate_days": len(events), "independent_signal_days": len({row["signal_date"] for row in events}),
        "symbols": len({row["symbol"] for row in events}),
        "coverage": len(directional) / len(events) if events else None,
        "directional": side_stats(directional),
        "call": side_stats([row for row in directional if row["direction"] == "C"]),
        "put": side_stats([row for row in directional if row["direction"] == "P"]),
        "wait": {"n": len(waiting), "mean_abs_underlying_move":
                 mean(abs(row[f"underlying_return_{horizon}"]) for row in waiting) if waiting else None},
        "policy_mean_per_candidate": mean(row[f"policy_net_{horizon}"] for row in events) if events else None,
        "always_call_mean_per_candidate": mean(row[f"always_call_net_{horizon}"] for row in events)
        if events else None,
        "delta_policy_minus_always_call": mean(delta) if delta else None,
        "ci95_delta_month_cluster_descriptive": ci_delta,
        "ci95_policy_mean_month_cluster_descriptive": ci_policy,
        "ci95_directional_hit_month_cluster_descriptive": ci_directional_hit,
    }


def run(symbols: tuple[str, ...] = SYMBOLS, *, start: date | None = None,
        end: date | None = None, histories: dict[str, pd.DataFrame] | None = None,
        bootstrap: int = 500) -> dict[str, Any]:
    symbols = tuple(dict.fromkeys(symbol.upper() for symbol in symbols if symbol.upper() != "SPY"))
    if histories is None:
        with external_data_scope(False):
            histories = {symbol: frame for symbol in ("SPY", *symbols)
                         if (frame := _history(symbol)) is not None}
    if "SPY" not in histories:
        return {"status": "missing_spy", "requested_symbols": list(symbols), "available_symbols": []}
    events, audit = collect_replay_events(histories, start=start, end=end)
    phases = ("exploration_2022_2023", "validation_2024",
              "retrospective_2025_plus_previously_inspected")
    summaries = {phase: {str(h): summarize([row for row in events if row["phase"] == phase], h,
                                               bootstrap=bootstrap) for h in HORIZONS}
                 for phase in phases}
    return {
        "status": "completed", "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "historical_replay_only", "model_version": MODEL_VERSION,
        "requested_symbols": list(symbols),
        "available_symbols": sorted(set(histories) - {"SPY"}),
        "missing_symbols": sorted(set(symbols) - set(histories)),
        "spy_data_as_of": str(_price_frame(histories["SPY"]).index[-1].date()),
        "primary_horizon_days": PRIMARY_HORIZON, "horizons": list(HORIZONS),
        "start": start.isoformat() if start else None, "end": end.isoformat() if end else None,
        "events": events, "summaries": summaries, "audit": audit,
        "limitations": [
            "Today-fixed cohort is not point-in-time sector-leader membership; selection bias remains.",
            "2025+ has already been inspected in earlier research and is not a fresh untouched test.",
            "Repeated daily signals and holding windows overlap; summaries are not a portfolio equity curve.",
            "Underlying signed returns and equity cost are not actual long-option P/L or option costs.",
            "Only cached daily OHLCV is read; missing prices are excluded, never fabricated.",
            "Historical replay is not inserted into the live forward ledger.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", help="Comma-separated fixed research cohort; defaults to prior 17 stocks")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--output", type=Path, help="Summary JSON, without individual events")
    parser.add_argument("--events-csv", type=Path, help="Optional auditable stock-day results")
    args = parser.parse_args()
    symbols = tuple(symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()) if args.symbols else SYMBOLS
    result = run(symbols, start=args.start, end=args.end)
    events = result.pop("events", [])
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    if args.events_csv:
        args.events_csv.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(events).to_csv(args.events_csv, index=False)
    print(json.dumps({"status": result["status"], "symbols": len(result.get("available_symbols", [])),
                      "events": len(events), "primary_2025_plus": result.get("summaries", {}).get(
                          "retrospective_2025_plus_previously_inspected", {}).get(str(PRIMARY_HORIZON))},
                     ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
