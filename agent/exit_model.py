"""Single source of truth for trade EXIT mechanics (backtests + live resolution).

The user trades short swings: BUY at the CLOSE of day T (盘末买入), SELL at the
OPEN of some day within T+1..T+10 (盘初卖出). That is NOT a fixed close-to-close
hold, which is what the resolvers/backtests historically measured.

Picking the best exit day ex-post is look-ahead. For resolution and calibration we
therefore use a FIXED hold ``H`` (no look-ahead):

    entry      = close[T]
    exit       = open[T+H]            (mode "close_to_open", default)
    ret        = open[T+H]/close[T] - 1
    baseline   = mean over all t of (open[t+H]/close[t] - 1)   (same convention)

Backtests (backtest_close_to_open.py) showed the edge is significant across
H = 3..10 and grows with hold length, peaking around H=8; any fixed hold in the
T+6..T+10 window is supported. The single default hold is therefore 8.

Everything is env-overridable so the convention stays reversible:
    SIGNAL_EXIT_MODE       close_to_open (default) | close_to_close (legacy)
    SIGNAL_EXIT_HOLD_DAYS  default fixed hold for resolution (default 8)

``open_`` MUST be positionally aligned with ``close`` (same index). When it is
None (or mode is close_to_close) the exit falls back to the close price, so this
module degrades safely if a frame lacks an Open column.
"""
from __future__ import annotations

import math
import os
from typing import Optional

try:  # pandas is always present in this project; import guarded for tooling.
    import pandas as pd
except Exception:  # pragma: no cover
    pd = None  # type: ignore


def exit_mode() -> str:
    mode = os.environ.get("SIGNAL_EXIT_MODE", "close_to_open").strip().lower()
    return mode if mode in ("close_to_open", "close_to_close") else "close_to_open"


def default_hold_days() -> int:
    try:
        h = int(os.environ.get("SIGNAL_EXIT_HOLD_DAYS", "8"))
        return h if h >= 1 else 8
    except (TypeError, ValueError):
        return 8


def _finite_pos(value: float) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def forward_return(close, open_, index: int, horizon: int, mode: Optional[str] = None):
    """entry=close[index] -> exit at index+horizon per mode. Returns float or None.

    close/open_ are pandas Series with matching positional index (open_ may be None).
    """
    mode = mode or exit_mode()
    n = len(close)
    if index + horizon >= n or index < 0:
        return None
    entry = float(close.iloc[index])
    if not _finite_pos(entry):
        return None
    if mode == "close_to_close" or open_ is None:
        exit_price = float(close.iloc[index + horizon])
    else:
        exit_price = float(open_.iloc[index + horizon])
        # Fall back to close if that bar's open is missing/invalid.
        if not _finite_pos(exit_price):
            exit_price = float(close.iloc[index + horizon])
    if not _finite_pos(exit_price):
        return None
    return exit_price / entry - 1.0


def baseline_forward_returns(close, open_, horizon: int, mode: Optional[str] = None) -> list:
    """Unconditional same-convention forward returns over every entry day."""
    mode = mode or exit_mode()
    out: list[float] = []
    for i in range(len(close) - horizon):
        r = forward_return(close, open_, i, horizon, mode)
        if r is not None:
            out.append(r)
    return out


def aligned_open(frame, close) -> Optional["pd.Series"]:
    """Build an Open series positionally aligned to ``close`` (a possibly-dropna'd
    Close series taken from ``frame``). Returns None if no usable Open column."""
    if pd is None or frame is None or "Open" not in getattr(frame, "columns", []):
        return None
    try:
        op = pd.to_numeric(frame["Open"], errors="coerce").reindex(close.index)
        return op
    except Exception:
        return None


def describe() -> dict:
    return {
        "mode": exit_mode(),
        "default_hold_days": default_hold_days(),
        "entry": "close[T]",
        "exit": "open[T+H]" if exit_mode() == "close_to_open" else "close[T+H]",
        "note": "盘末买入->盘初卖出; H 在 T+6..T+10 均受支持, 默认 8",
    }
