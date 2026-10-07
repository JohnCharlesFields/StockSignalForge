"""Sanity constraints for the open-session action plan. ASCII-only.

Guards the bug where a "pullback (dip) buy" level came out ABOVE the current
price (because nearest_support was above price). Basic invariants:
  - dip-buy level < current price
  - breakout-buy level > current price
  - stop-loss < current price < take-profit
"""

from __future__ import annotations

import pandas as pd
import pytest

from api_server import _single_open_action_plan


def _frame(n: int = 40) -> pd.DataFrame:
    idx = pd.bdate_range("2025-01-01", periods=n)
    close = [150.0 + i * 0.1 for i in range(n)]
    high = [c * 1.012 for c in close]
    low = [c * 0.988 for c in close]
    return pd.DataFrame(
        {"Open": close, "High": high, "Low": low, "Close": close, "Volume": [1_000_000] * n},
        index=idx,
    )


def _plan(nearest_support: float) -> dict:
    frame = _frame()
    close = float(frame["Close"].iloc[-1])
    row = {"current_price": close, "nearest_support": nearest_support}
    return _single_open_action_plan("T", row, frame)


def test_dip_below_and_breakout_above_when_support_is_above_price() -> None:
    # The regression case: support above current price must NOT push the dip-buy
    # level above the current price.
    frame = _frame()
    close = float(frame["Close"].iloc[-1])
    ap = _plan(nearest_support=close * 1.03)
    assert ap["available"] is True
    assert ap["buy_pullback_price"] < ap["as_of_price"], "dip-buy must be below current price"
    assert ap["buy_strength_price"] > ap["as_of_price"], "breakout-buy must be above current price"


def test_dip_below_when_support_is_below_price() -> None:
    frame = _frame()
    close = float(frame["Close"].iloc[-1])
    ap = _plan(nearest_support=close * 0.95)
    assert ap["buy_pullback_price"] < ap["as_of_price"]
    assert ap["buy_pullback_pct"] < 0  # labeled as a dip (negative vs current)


def test_stop_below_target_above() -> None:
    frame = _frame()
    close = float(frame["Close"].iloc[-1])
    ap = _plan(nearest_support=close * 0.95)
    assert ap["stop_loss"] < ap["as_of_price"] < ap["take_profit"]
    assert ap["stop_buying_below"] < ap["as_of_price"]
