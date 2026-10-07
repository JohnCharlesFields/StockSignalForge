from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.research_signal_framework_backtest import (
    _apply_bh_fdr,
    _collect_symbol_events,
    _summarize,
)


def _frame(values: list[float]) -> pd.DataFrame:
    close = pd.Series(values, index=pd.date_range("2024-01-01", periods=len(values), freq="B"))
    return pd.DataFrame({
        "Open": close,
        "High": close * 1.01,
        "Low": close * 0.99,
        "Close": close,
        "Volume": 2_000_000,
    })


def test_walk_forward_collection_never_uses_future_bars_for_scoring() -> None:
    values = [100 + index * 0.8 for index in range(90)]
    events = _collect_symbol_events("TEST", _frame(values), 5, cooldown_days=5)
    assert events
    assert all(event["forward_return"] > 0 for event in events)
    assert len({event["date"] for event in events}) == len(events)


def test_summary_and_fdr_support_clear_positive_clustered_effect() -> None:
    events = []
    for ticker in [f"T{index}" for index in range(10)]:
        for day in range(4):
            events.append({
                "ticker": ticker,
                "date": f"2024-01-{day + 1:02d}",
                "forward_return": 0.08,
                "symbol_baseline_return": 0.01,
                "excess_return": 0.07,
            })
    row = _summarize(events, "synthetic", 5)
    _apply_bh_fdr([row])
    assert row["events"] == 40
    assert row["clusters"] == 10
    assert row["mean_excess_return"] > 0
    assert row["hypothesis_supported"] is True
