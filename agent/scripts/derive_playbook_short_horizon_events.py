#!/usr/bin/env python3
"""Derive short-horizon outcomes for already-enriched playbook events.

The signal date/features stay fixed. Only the realized forward outcome is
recomputed for shorter holding windows (e.g. 1/3/5 trading days), which matches
the user's short holding style and avoids re-running the expensive full scan.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
from pathlib import Path
from statistics import mean
from typing import Any

import pandas as pd

HERE = Path(__file__).resolve().parent
AGENT = HERE.parent
os.environ.setdefault("VIBE_MARKET_DATA_CACHE_DIR", str(AGENT / "data_cache" / "market_data"))
for path in (AGENT, HERE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import cost_model  # noqa: E402
from market_data_service import get_daily_history  # noqa: E402


RUNS_DIR = AGENT / "runs"


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _history(symbol: str, memo: dict[str, pd.DataFrame]) -> pd.DataFrame:
    sym = str(symbol or "").upper()
    if not sym:
        return pd.DataFrame()
    if sym not in memo:
        try:
            frame, _ = get_daily_history(sym, period="5y", allow_yfinance_fallback=False)
        except Exception:
            frame = pd.DataFrame()
        memo[sym] = frame if frame is not None else pd.DataFrame()
    return memo[sym]


def _align(a: pd.DataFrame, b: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    idx = a.index.intersection(b.index)
    return a.loc[idx].copy(), b.loc[idx].copy()


def _baseline_returns(frame: pd.DataFrame, horizon: int) -> list[float]:
    close = pd.to_numeric(frame.get("Close"), errors="coerce")
    open_ = pd.to_numeric(frame.get("Open"), errors="coerce")
    vals = []
    for i in range(0, len(frame) - horizon):
        entry = _num(close.iloc[i])
        exitp = _num(open_.iloc[i + horizon])
        if entry and exitp and entry > 0 and exitp > 0:
            vals.append(exitp / entry - 1.0)
    return vals


def _derive_for_horizon(payload: dict[str, Any], horizon: int, histories: dict[str, pd.DataFrame]) -> dict[str, Any]:
    rt_cost = cost_model.equity_round_trip_cost()
    baseline_cache: dict[str, float] = {}
    out_events = []
    skipped = {"missing_history": 0, "missing_date": 0, "insufficient_forward": 0}
    for row in payload.get("events") or []:
        sym = str(row.get("ticker") or row.get("symbol") or "").upper()
        bench_sym = str(row.get("benchmark") or "SPY").upper()
        stock = _history(sym, histories)
        bench = _history(bench_sym, histories)
        if stock.empty or bench.empty:
            skipped["missing_history"] += 1
            continue
        stock, bench = _align(stock, bench)
        day = pd.Timestamp(row.get("date"))
        if day not in stock.index or day not in bench.index:
            skipped["missing_date"] += 1
            continue
        idx = int(stock.index.get_loc(day))
        if idx + horizon >= len(stock) or idx + horizon >= len(bench):
            skipped["insufficient_forward"] += 1
            continue
        s_close = pd.to_numeric(stock["Close"], errors="coerce")
        s_open = pd.to_numeric(stock["Open"], errors="coerce")
        b_close = pd.to_numeric(bench["Close"], errors="coerce")
        b_open = pd.to_numeric(bench["Open"], errors="coerce")
        entry = _num(s_close.iloc[idx])
        exitp = _num(s_open.iloc[idx + horizon])
        b_entry = _num(b_close.iloc[idx])
        b_exit = _num(b_open.iloc[idx + horizon])
        if not entry or not exitp or not b_entry or not b_exit:
            skipped["insufficient_forward"] += 1
            continue
        forward = exitp / entry - 1.0
        bench_ret = b_exit / b_entry - 1.0
        if sym not in baseline_cache:
            vals = _baseline_returns(stock, horizon)
            baseline_cache[sym] = mean(vals) if vals else 0.0
        own_base = baseline_cache[sym]
        beta = _num(row.get("beta"), 1.0) or 1.0
        out = dict(row)
        out.update({
            "horizon_days": horizon,
            "forward_return": round(forward, 8),
            "benchmark_return": round(bench_ret, 8),
            "symbol_baseline_return": round(own_base, 8),
            "excess_return": round(forward - own_base, 8),
            "net_own_excess": round(forward - own_base - rt_cost, 8),
            "net_beta_alpha": round(forward - rt_cost - beta * bench_ret, 8),
        })
        out_events.append(out)
    return {
        **{k: v for k, v in payload.items() if k != "events"},
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "method": "playbook_short_horizon_derived",
        "source_input": payload.get("input_json") or payload.get("method"),
        "horizon_days": horizon,
        "exit_convention": f"close[T] -> open[T+{horizon}]",
        "cost": {"equity_round_trip_cost": rt_cost},
        "derive_stats": {"events": len(out_events), "skipped": skipped},
        "events": out_events,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="agent/runs/pullback_playbook_sector_enriched_260625.json")
    ap.add_argument("--horizons", default="1,3,5")
    ap.add_argument("--output-prefix", default="pullback_playbook_sector_enriched_short")
    args = ap.parse_args()

    source = Path(args.input)
    payload = json.loads(source.read_text(encoding="utf-8"))
    histories: dict[str, pd.DataFrame] = {}
    outputs = []
    for h in [int(x) for x in args.horizons.split(",") if x.strip()]:
        derived = _derive_for_horizon(payload, h, histories)
        out = RUNS_DIR / f"{args.output_prefix}_h{h}_260625.json"
        out.write_text(json.dumps(derived, ensure_ascii=False, indent=2), encoding="utf-8")
        outputs.append({"horizon": h, "output": str(out), "events": len(derived.get("events") or []), "skipped": derived.get("derive_stats", {}).get("skipped")})
    print(json.dumps({"outputs": outputs}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
