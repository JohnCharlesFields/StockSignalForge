#!/usr/bin/env python3
"""Log today's launch / daily_tunnel signal scores for out-of-sample accrual.

R1 calibration is currently built from in-sample current-constituent replay,
which carries survivor / look-ahead bias.  To upgrade "statistically significant
in-sample" to "out-of-sample confirmed", we must record the signal scores *as of
today* and let realized forward returns fill in N trading days later (via
``resolve_signal_events``).  Over weeks this produces a genuine OOS sample that
``build_signal_calibration.py --source live`` can calibrate on.

We log only the BASE signal types (``launch``, ``daily_tunnel``) with the raw
native score.  The contrarian curves are derived from the same rows at build time
(``signal_calibration.normalize_score`` inverts them), so no duplicate storage.

Run from CLI (daily cron) or call ``log_daily_signals``.  Idempotent per
``(signal_type, symbol, as_of_date, horizon_days)`` so repeated runs in a day do
not double-count.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_DIR = SCRIPT_DIR.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from app_database import signal_event_log_many, signal_events_count  # noqa: E402
from launch_signal_service import _score_frame, _ticker_frame  # noqa: E402
from market_data_service import download_daily_history  # noqa: E402
from scripts.screening_framework_v2_optimized import resolve_universe  # noqa: E402

DEFAULT_HORIZONS = (5, 10)


def log_daily_signals(
    universe: str = "spx",
    *,
    max_symbols: int = 0,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Score ``universe`` today and log base launch/tunnel events for OOS accrual."""
    tickers, source, _ = resolve_universe(universe)
    if max_symbols:
        tickers = tickers[:max_symbols]
    as_of = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    run_id = run_id or f"oos_{datetime.now(timezone.utc).strftime('%Y%m%d')}"
    download, _sources = download_daily_history(tickers, period="6mo")

    events: list[dict[str, Any]] = []
    scored = 0
    for ticker in tickers:
        try:
            row = _score_frame(ticker, _ticker_frame(download, ticker), 0.5)
        except Exception:
            row = None
        if not row:
            continue
        scored += 1
        launch = float(row.get("launch_score") or 0.0)
        tunnel = float((row.get("daily_tunnel") or {}).get("score") or 0.0)
        ref = float(row.get("last_price") or 0.0)
        for horizon in horizons:
            events.append({"signal_type": "launch", "symbol": ticker, "as_of_date": as_of,
                           "horizon_days": horizon, "score": launch, "ref_price": ref, "run_id": run_id})
            events.append({"signal_type": "daily_tunnel", "symbol": ticker, "as_of_date": as_of,
                           "horizon_days": horizon, "score": tunnel, "ref_price": ref, "run_id": run_id})
    logged = signal_event_log_many(events)
    return {
        "universe": universe,
        "source": source,
        "as_of_date": as_of,
        "tickers": len(tickers),
        "scored": scored,
        "events_submitted": len(events),
        "events_logged_new": logged,
        "counts": signal_events_count(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Log today's signal scores for out-of-sample calibration.")
    parser.add_argument("--universe", default="spx")
    parser.add_argument("--max-symbols", type=int, default=0)
    parser.add_argument("--horizons", default="5,10")
    args = parser.parse_args()
    horizons = tuple(int(x) for x in args.horizons.split(",") if x.strip())
    summary = log_daily_signals(args.universe, max_symbols=args.max_symbols, horizons=horizons)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
