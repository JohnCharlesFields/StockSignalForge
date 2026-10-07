#!/usr/bin/env python3
"""Offline rank/selectivity diagnostics for the equity-only option shadow replay.

All labels come from an existing event CSV. Optional SPY regime features use
only the close and trailing history available on the signal date. This does
not change the live board, choose new thresholds, or estimate option P/L.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

import pandas as pd

AGENT = Path(__file__).resolve().parents[1]
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

from long_option_shadow_service import HORIZONS, PRIMARY_HORIZON  # noqa: E402
from market_data_service import external_data_scope  # noqa: E402
from scripts.replay_long_option_shadow import _direction  # noqa: E402
from scripts.validate_long_option_direction import _history  # noqa: E402

PHASES = (
    "exploration_2022_2023",
    "validation_2024",
    "retrospective_2025_plus_previously_inspected",
)
SCORE_BANDS = ((0.12, 0.25), (0.25, 0.50), (0.50, 0.75), (0.75, math.inf))


def read_events(path: Path, horizon: int) -> list[dict[str, Any]]:
    if horizon not in HORIZONS:
        raise ValueError("unsupported horizon")
    frame = pd.read_csv(path, dtype={"symbol": str, "signal_date": str, "phase": str,
                                     "direction": str})
    needed = {"symbol", "signal_date", "phase", "direction", "net_consensus",
              f"signed_net_{horizon}", f"signed_spy_excess_{horizon}",
              f"policy_net_{horizon}", f"always_call_net_{horizon}"}
    missing = needed - set(frame.columns)
    if missing:
        raise ValueError(f"missing replay columns: {sorted(missing)}")
    if frame.duplicated(["symbol", "signal_date"]).any():
        raise ValueError("duplicate symbol/signal_date in replay")
    numeric = ["net_consensus", f"signed_net_{horizon}", f"signed_spy_excess_{horizon}",
               f"policy_net_{horizon}", f"always_call_net_{horizon}"]
    for key in numeric:
        frame[key] = pd.to_numeric(frame[key], errors="coerce")
    if frame[["net_consensus", f"policy_net_{horizon}",
              f"always_call_net_{horizon}"]].isna().any().any():
        raise ValueError("missing required numeric event value")
    if (frame["direction"] != frame["net_consensus"].map(_direction)).any():
        raise ValueError("replay direction differs from the current score rule")
    if frame.loc[frame.direction != "WAIT", f"signed_net_{horizon}"].isna().any():
        raise ValueError("directional event has no net return")
    if not frame["signal_date"].str.fullmatch(r"\d{4}-\d{2}-\d{2}").all():
        raise ValueError("invalid signal date")
    return frame.to_dict("records")


def spy_regimes(spy: pd.DataFrame | None) -> dict[str, dict[str, bool]]:
    if spy is None or spy.empty or "Close" not in spy:
        return {}
    close = pd.to_numeric(spy["Close"], errors="coerce").sort_index()
    close.index = pd.to_datetime(close.index).normalize()
    close = close.loc[~close.index.duplicated(keep="last")]
    ma200 = close.rolling(200, min_periods=200).mean()
    prior20 = close / close.shift(20) - 1
    result = {}
    for day in close.index:
        price, ma, move = close.loc[day], ma200.loc[day], prior20.loc[day]
        if all(math.isfinite(float(v)) for v in (price, ma, move)):
            result[day.date().isoformat()] = {
                "below_ma200": bool(price < ma),
                "negative_prior20": bool(move < 0),
            }
    return result


def _stats(rows: list[dict[str, Any]], horizon: int) -> dict[str, Any]:
    key = f"signed_net_{horizon}"
    active = [r for r in rows if r["direction"] != "WAIT"]
    return {
        "events": len(rows),
        "signal_days": len({r["signal_date"] for r in rows}),
        "active_events": len(active),
        "active_net_hit": mean(r[key] > 0 for r in active) if active else None,
        "active_mean_net": mean(r[key] for r in active) if active else None,
        "active_mean_spy_excess": mean(r[f"signed_spy_excess_{horizon}"] for r in active)
        if active else None,
        "policy_mean_per_candidate": mean(r[f"policy_net_{horizon}"] for r in rows)
        if rows else None,
        "matched_always_call_mean": mean(r[f"always_call_net_{horizon}"] for r in rows)
        if rows else None,
    }


def _month_interval(pairs: list[tuple[str, float]], bootstrap: int) -> list[float] | None:
    months: dict[str, list[float]] = defaultdict(list)
    for day, value in pairs:
        months[day[:7]].append(value)
    if len(months) < 6 or bootstrap <= 0:
        return None
    keys = sorted(months)
    rng = random.Random(173)
    draws = []
    for _ in range(bootstrap):
        sampled = [months[rng.choice(keys)] for _ in keys]
        draws.append(sum(sum(values) for values in sampled) / sum(len(values) for values in sampled))
    draws.sort()
    return [draws[int(0.025 * bootstrap)], draws[int(0.975 * bootstrap) - 1]]


def _paired_rank(rows: list[dict[str, Any]], horizon: int, top_n: int,
                 bootstrap: int) -> dict[str, Any]:
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["direction"] != "WAIT":
            by_day[row["signal_date"]].append(row)
    comparisons = []
    key = f"signed_net_{horizon}"
    for day, candidates in sorted(by_day.items()):
        if len(candidates) < max(2, top_n * 2):
            continue
        ranked = sorted(candidates, key=lambda r: (-abs(r["net_consensus"]), r["symbol"]))
        top, rest = ranked[:top_n], ranked[top_n:]
        comparisons.append({
            "day": day, "candidates": len(ranked),
            "top": mean(r[key] for r in top),
            "rest": mean(r[key] for r in rest),
            "bottom": mean(r[key] for r in ranked[-top_n:]),
            "matched_always_call": mean(r[f"always_call_net_{horizon}"] for r in top),
            "top_spy_excess": mean(r[f"signed_spy_excess_{horizon}"] for r in top),
        })
    return {
        "eligible_days": len(comparisons),
        "mean_candidates_per_day": mean(r["candidates"] for r in comparisons) if comparisons else None,
        "top_mean_net": mean(r["top"] for r in comparisons) if comparisons else None,
        "rest_mean_net": mean(r["rest"] for r in comparisons) if comparisons else None,
        "bottom_mean_net": mean(r["bottom"] for r in comparisons) if comparisons else None,
        "top_minus_rest": mean(r["top"] - r["rest"] for r in comparisons)
        if comparisons else None,
        "top_minus_bottom": mean(r["top"] - r["bottom"] for r in comparisons)
        if comparisons else None,
        "top_minus_matched_always_call": mean(r["top"] - r["matched_always_call"]
                                              for r in comparisons) if comparisons else None,
        "top_mean_spy_excess": mean(r["top_spy_excess"] for r in comparisons)
        if comparisons else None,
        "ci95_top_minus_rest_month_descriptive": _month_interval(
            [(r["day"], r["top"] - r["rest"]) for r in comparisons], bootstrap),
        "ci95_top_minus_matched_always_call_month_descriptive": _month_interval(
            [(r["day"], r["top"] - r["matched_always_call"]) for r in comparisons], bootstrap),
    }


def _daily_quartiles(rows: list[dict[str, Any]], horizon: int) -> dict[str, Any]:
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["direction"] != "WAIT":
            by_day[row["signal_date"]].append(row)
    quartiles: dict[int, list[float]] = defaultdict(list)
    for candidates in by_day.values():
        if len(candidates) < 8:
            continue
        ranked = sorted(candidates, key=lambda r: (-abs(r["net_consensus"]), r["symbol"]))
        daily: dict[int, list[float]] = defaultdict(list)
        for pos, row in enumerate(ranked):
            daily[min(3, pos * 4 // len(ranked))].append(row[f"signed_net_{horizon}"])
        for group in range(4):
            quartiles[group].append(mean(daily[group]))
    return {f"q{group + 1}_strongest_first": {
        "days": len(quartiles[group]),
        "mean_daily_net": mean(quartiles[group]) if quartiles[group] else None,
    } for group in range(4)}


def diagnose(events: list[dict[str, Any]], *, horizon: int = PRIMARY_HORIZON,
             regimes: dict[str, dict[str, bool]] | None = None,
             bootstrap: int = 500) -> dict[str, Any]:
    if horizon not in HORIZONS:
        raise ValueError("unsupported horizon")
    regimes = regimes or {}
    output: dict[str, Any] = {}
    for phase in PHASES:
        rows = [r for r in events if r["phase"] == phase]
        strength = {}
        for side in ("C", "P"):
            side_rows = [r for r in rows if r["direction"] == side]
            strength[side] = {
                f"{lo:.2f}-{hi:.2f}" if math.isfinite(hi) else f">={lo:.2f}":
                _stats([r for r in side_rows if lo <= abs(r["net_consensus"]) < hi], horizon)
                for lo, hi in SCORE_BANDS
            }
        puts = [r for r in rows if r["direction"] == "P"]
        known = [r for r in puts if r["signal_date"] in regimes]
        regime_stats = {
            "put_events_with_spy_context": len(known),
            "put_events_without_spy_context": len(puts) - len(known),
            "below_ma200_and_prior20_negative": _stats(
                [r for r in known if regimes[r["signal_date"]]["below_ma200"]
                 and regimes[r["signal_date"]]["negative_prior20"]], horizon),
            "other_spy_states": _stats(
                [r for r in known if not (regimes[r["signal_date"]]["below_ma200"]
                                            and regimes[r["signal_date"]]["negative_prior20"])],
                horizon),
        }
        output[phase] = {
            "all_candidates": _stats(rows, horizon),
            "call": _stats([r for r in rows if r["direction"] == "C"], horizon),
            "put": _stats(puts, horizon),
            "wait": _stats([r for r in rows if r["direction"] == "WAIT"], horizon),
            "top1_vs_rest": _paired_rank(rows, horizon, 1, bootstrap),
            "top3_vs_rest": _paired_rank(rows, horizon, 3, bootstrap),
            "call_top1_vs_call_rest": _paired_rank(
                [r for r in rows if r["direction"] == "C"], horizon, 1, bootstrap),
            "call_top3_vs_call_rest": _paired_rank(
                [r for r in rows if r["direction"] == "C"], horizon, 3, bootstrap),
            "put_top1_vs_put_rest": _paired_rank(puts, horizon, 1, bootstrap),
            "put_top3_vs_put_rest": _paired_rank(puts, horizon, 3, bootstrap),
            "daily_score_quartiles": _daily_quartiles(rows, horizon),
            "score_strength_by_side": strength,
            "put_by_spy_regime": regime_stats,
        }
    return {
        "status": "completed", "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "retrospective_diagnostic_only", "horizon_trading_days": horizon,
        "score_order": "abs(net_consensus) descending, ticker ascending on ties",
        "source_event_count": len(events), "spy_regime_available": bool(regimes),
        "phases": output,
        "limitations": [
            "Today's fixed stock cohort is not historical point-in-time sector leadership.",
            "All calendar phases have been inspected before; findings are hypothesis generation, not fresh OOS evidence.",
            "Rank comparisons pair stocks on each signal date; overlapping holding windows remain and month intervals are descriptive.",
            "Score bands and SPY regime were fixed for diagnosis; no threshold was fitted or deployed.",
            "Equity directional proxy and 0.06% equity cost are not option premiums, IV, spread, or option P/L.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events-csv", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--horizon", type=int, choices=HORIZONS, default=PRIMARY_HORIZON)
    parser.add_argument("--bootstrap", type=int, default=500)
    parser.add_argument("--skip-spy-regime", action="store_true")
    args = parser.parse_args()
    events = read_events(args.events_csv, args.horizon)
    spy = None
    if not args.skip_spy_regime:
        with external_data_scope(False):
            spy = _history("SPY")
    result = diagnose(events, horizon=args.horizon, regimes=spy_regimes(spy),
                      bootstrap=args.bootstrap)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                           encoding="utf-8")
    phase = result["phases"]["retrospective_2025_plus_previously_inspected"]
    print(json.dumps({"status": result["status"], "events": len(events),
                      "spy_regime_available": result["spy_regime_available"],
                      "top1_vs_rest": phase["top1_vs_rest"],
                      "top3_vs_rest": phase["top3_vs_rest"]}, allow_nan=False))


if __name__ == "__main__":
    main()
