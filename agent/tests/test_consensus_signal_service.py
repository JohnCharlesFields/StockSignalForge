from __future__ import annotations

import numpy as np
import pandas as pd

from consensus_signal_service import consensus_feature_frame, latest_consensus_snapshot


def _frame(prices: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=len(prices), freq="B")
    close = pd.Series(prices, index=idx)
    open_ = close.shift(1).fillna(close.iloc[0]) * 0.998
    high = pd.concat([open_, close], axis=1).max(axis=1) * 1.012
    low = pd.concat([open_, close], axis=1).min(axis=1) * 0.988
    vol = pd.Series(volumes or [1_000_000.0] * len(prices), index=idx)
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol}, index=idx)


def test_uptrend_accumulation_has_positive_net_consensus() -> None:
    prices = list(np.linspace(50, 80, 120))
    volumes = [1_000_000 + i * 5_000 for i in range(120)]
    features = consensus_feature_frame(_frame(prices, volumes))
    last = features.iloc[-1]
    assert last["bull_consensus"] > last["bear_consensus"]
    assert last["consensus_direction_score"] > 0.5
    assert 0.0 <= last["consensus_launch_score"] <= 1.0


def test_failed_breakout_and_upper_shadow_raise_bear_consensus() -> None:
    prices = list(np.linspace(30, 50, 90)) + [51, 52, 53, 54, 54.2, 54.1]
    frame = _frame(prices, [1_000_000.0] * len(prices))
    # Last bar: intraday breakout, weak close near low, heavy volume.
    frame.iloc[-1, frame.columns.get_loc("High")] = frame["High"].iloc[:-1].max() * 1.05
    frame.iloc[-1, frame.columns.get_loc("Close")] = frame["Low"].iloc[-1] * 1.01
    frame.iloc[-1, frame.columns.get_loc("Volume")] = 5_000_000.0
    features = consensus_feature_frame(frame)
    last = features.iloc[-1]
    assert last["bear_consensus"] >= 0.35
    assert last["consensus_direction_score"] < 0.65


def test_latest_snapshot_is_available_for_sufficient_history() -> None:
    prices = list(np.linspace(20, 35, 100))
    snapshot = latest_consensus_snapshot(_frame(prices))
    assert snapshot["available"] is True
    assert snapshot["direction"] in {"bullish", "bearish", "neutral"}
    assert 0.0 <= snapshot["launch_potential_score"] <= 1.0
