#!/usr/bin/env python3
"""Ingest whole-market daily bars from Massive/Polygon grouped-daily into the
per-symbol OHLCV cache, so the BULK paths (board candidate scoring, backtests)
read clean, complete data from cache instead of hammering yfinance.

Each grouped-daily call returns ~12k tickers for ONE day, so N trading days = N
calls (5/min free). We only persist the symbols in the requested universe (spx)
to keep the cache focused. Existing cache is merged (not overwritten).

Usage:
  Seed ~6mo:   python agent/scripts/ingest_grouped_daily.py --universe spx --days 130
  Incremental: python agent/scripts/ingest_grouped_daily.py --universe spx --days 4
  (or import ingest_recent_grouped_daily() from the daily scan for a cheap refresh)
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_DIR = SCRIPT_DIR.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import market_data_service as mds  # noqa: E402
from scripts.screening_framework_v2_optimized import resolve_universe  # noqa: E402


def ingest_recent_grouped_daily(
    universe: str = "spx",
    days: int = 4,
    sleep: float = 12.5,
    *,
    target_session: str | None = None,
    universe_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Pull the last ``days`` trading days of whole-market bars and merge the
    universe symbols into the per-symbol cache. Returns a summary dict.
    """
    keep: set[str] = set()
    universe_errors: dict[str, str] = {}
    for universe_id in dict.fromkeys([universe, *(universe_ids or [])]):
        try:
            tickers, _src, _ = resolve_universe(universe_id)
            keep.update(str(t).strip().upper() for t in tickers if str(t).strip())
        except Exception as exc:
            universe_errors[universe_id] = str(exc)[-160:]
    if not keep:
        return {"ok": False, "error": f"universe resolution failed: {universe_errors}"}

    per_symbol: dict[str, list[dict[str, Any]]] = {}
    calls = 0
    d = date.fromisoformat(target_session) if target_session else date.today()
    seen_days = 0
    latest_data_date = None
    latest_session_symbols = 0
    # Walk back over calendar days; grouped-daily returns {} on weekends/holidays.
    span = 0
    while seen_days < days and span < (1 if target_session else days * 2 + 10):
        span += 1
        ds = d.isoformat()
        d -= timedelta(days=1)
        if date.fromisoformat(ds).weekday() >= 5:
            continue
        calls += 1
        try:
            grouped = mds.massive_grouped_daily(ds)
        except Exception as exc:
            return {"ok": False, "universe": universe, "calls": calls,
                    "requested_session": target_session or ds, "error": str(exc)[-240:]}
        if not grouped:
            continue
        seen_days += 1
        if latest_data_date is None or ds > latest_data_date:
            latest_data_date = ds
        ts = pd.Timestamp(ds)
        for sym in keep:
            bar = grouped.get(sym)
            if not bar or not bar.get("close"):
                continue
            if ds == latest_data_date:
                latest_session_symbols += 1
            per_symbol.setdefault(sym, []).append({
                "Date": ts, "Open": bar["open"], "High": bar["high"],
                "Low": bar["low"], "Close": bar["close"], "Volume": bar["volume"],
            })
        if sleep > 0 and not target_session:
            time.sleep(sleep)

    written = 0
    for sym, rows in per_symbol.items():
        if not rows:
            continue
        fresh = pd.DataFrame(rows).set_index("Date").sort_index()
        merged = mds._merge_daily(mds._read_daily_cache(sym), fresh)
        if not merged.empty:
            mds._write_daily_cache(sym, merged)
            written += 1
    return {"ok": True, "universe": universe, "trading_days": seen_days, "calls": calls,
            "latest_data_date": latest_data_date,
            "latest_session_symbols": latest_session_symbols,
            "symbols_written": written, "symbols_in_universe": len(keep),
            "universe_errors": universe_errors}


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest grouped-daily whole-market bars into per-symbol cache.")
    ap.add_argument("--universe", default="spx")
    ap.add_argument("--days", type=int, default=130)
    ap.add_argument("--sleep", type=float, default=12.5, help="Seconds between day-calls (5/min free cap).")
    args = ap.parse_args()
    print(f"ingest grouped-daily: universe={args.universe} days={args.days}", flush=True)
    summary = ingest_recent_grouped_daily(args.universe, days=args.days, sleep=args.sleep)
    print(summary, flush=True)


if __name__ == "__main__":
    main()
