#!/usr/bin/env python3
"""Resolve logged live signal events into realized forward statistics (R1).

The running cockpit logs every emitted launch / daily-tunnel signal into the
``signal_events`` table (see ``app_database.signal_event_log_many``).  This
job walks the unresolved backlog and, once a signal is at least ``horizon``
trading days old and price history is available, fills in:

* ``forward_return``  -- gross close-to-close return over the horizon
* ``baseline_return`` -- the symbol's unconditional mean horizon-forward return
* ``excess_return``   -- ``forward_return - baseline_return`` (gross)

Costs are intentionally *not* applied here; ``signal_calibration.build_calibration``
applies the round-trip cost uniformly so replay and live curves stay consistent.

Run from CLI (suitable for a daily cron) or call ``resolve_pending_events``.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_DIR = SCRIPT_DIR.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import exit_model  # noqa: E402
from app_database import (  # noqa: E402
    signal_event_resolve,
    signal_events_count,
    signal_events_pending,
)
from market_data_service import get_daily_history  # noqa: E402


def _maybe_ready(as_of: str, horizon: int, today: datetime.date) -> bool:
    """Cheap pre-check: could ``horizon`` trading days have elapsed by ``today``?

    This avoids a (slow, rate-limited) per-symbol price download for events that
    are obviously too young to resolve.  It only ever SKIPS clearly-immature
    events; anything that passes still goes through the exact ``exit_idx`` guard
    in ``_resolve_one`` (which uses real bars, including holidays), so there is no
    correctness risk -- ``np.busday_count`` ignoring holidays just means a few
    borderline events get fetched and then correctly left pending.
    """
    try:
        start = datetime.date.fromisoformat(str(as_of)[:10])
    except (TypeError, ValueError):
        return True  # unparseable -> let the slow path decide
    if start > today:
        return False
    # Need the entry bar plus ``horizon`` subsequent closed bars.
    elapsed = int(np.busday_count(start, today))
    return elapsed >= horizon + 1


def _baseline_forward_returns(close: pd.Series, horizon: int, open_: pd.Series | None = None) -> list[float]:
    return exit_model.baseline_forward_returns(close, open_, horizon)


def _resolve_one(event: dict[str, Any], history_period: str) -> dict[str, Any] | None:
    """Compute gross forward/baseline/excess for one event, or None if not ready.

    Entry = close[T] (盘末买入), exit = open[T+H] (盘初卖出) per ``exit_model``.
    """
    symbol = event["symbol"]
    horizon = int(event["horizon_days"])
    as_of = str(event["as_of_date"])[:10]
    frame, _source = get_daily_history(symbol, period=history_period)
    if frame.empty or "Close" not in frame:
        return None
    close = pd.to_numeric(frame["Close"], errors="coerce").dropna()
    if len(close) <= horizon:
        return None
    open_ = exit_model.aligned_open(frame, close)
    as_of_ts = pd.Timestamp(as_of)
    # Entry = first bar on or after the as-of date.
    entry_positions = [pos for pos, ts in enumerate(close.index) if ts >= as_of_ts]
    if not entry_positions:
        return None
    entry_idx = entry_positions[0]
    if entry_idx + horizon >= len(close):
        return None  # horizon has not fully elapsed yet -> leave pending
    forward_return = exit_model.forward_return(close, open_, entry_idx, horizon)
    if forward_return is None:
        return None
    baseline_samples = _baseline_forward_returns(close, horizon, open_)
    baseline = sum(baseline_samples) / len(baseline_samples) if baseline_samples else 0.0
    return {
        "forward_return": forward_return,
        "baseline_return": baseline,
        "excess_return": forward_return - baseline,
    }


def resolve_pending_events(limit: int = 500, history_period: str = "2y") -> dict[str, Any]:
    """Resolve as many pending events as are ready. Returns a summary dict."""
    pending = signal_events_pending(limit=limit)
    resolved = 0
    skipped = 0
    immature = 0
    today = datetime.date.today()
    for event in pending:
        # Skip obviously-too-young events without any network call. This is what
        # keeps each cycle nearly free until events actually mature (otherwise we
        # would re-download thousands of symbols every pass for zero resolutions).
        if not _maybe_ready(event.get("as_of_date"), int(event["horizon_days"]), today):
            immature += 1
            continue
        try:
            stats = _resolve_one(event, history_period)
        except Exception:
            stats = None
        if not stats:
            skipped += 1
            continue
        signal_event_resolve(
            event["event_id"],
            forward_return=stats["forward_return"],
            excess_return=stats["excess_return"],
            baseline_return=stats["baseline_return"],
        )
        resolved += 1
    return {
        "examined": len(pending),
        "resolved": resolved,
        "not_ready": skipped,
        "immature_skipped": immature,
        "counts": signal_events_count(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve logged signal events into realized forward returns.")
    parser.add_argument("--limit", type=int, default=500, help="Max pending events to examine in one pass.")
    parser.add_argument("--history-period", default="2y", help="History window for baseline + forward lookup.")
    args = parser.parse_args()
    summary = resolve_pending_events(limit=args.limit, history_period=args.history_period)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
