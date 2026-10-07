from __future__ import annotations

import pandas as pd

from launch_signal_service import _score_frame
from technical_signal_metrics import compute_daily_tunnel_score


def _frame(prices: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {"Close": prices, "Volume": [1_000_000] * len(prices)},
        index=pd.date_range("2026-01-02", periods=len(prices), freq="B"),
    )


def test_daily_tunnel_scores_persistent_up_cycle_highly() -> None:
    result = compute_daily_tunnel_score(_frame([100 + index * 1.2 for index in range(60)]))

    assert result["score"] >= 85
    assert result["label"] == "强上行周期"
    assert result["cycle_state"] == "up_cycle"
    assert result["current_zone"] == "上行周期：沿 MA5 推进"
    assert result["longest_up_cycle_days"] >= 20
    assert result["up_cycle_power"] >= 0.7


def test_daily_tunnel_penalizes_unconfirmed_deep_pullbacks() -> None:
    prices = [100 + index * 0.55 for index in range(35)]
    prices += [118, 114, 111, 108, 105, 103, 101, 99, 97, 96, 95, 94, 93, 92, 91]
    result = compute_daily_tunnel_score(_frame(prices))

    assert result["score"] < 55
    assert result["cycle_state"] == "down_cycle"
    assert result["turning_point_score"] < 0.65


def test_daily_tunnel_rewards_down_to_up_turn_after_pullback() -> None:
    prices = [100 + index * 1.0 for index in range(35)]
    prices += [134, 131, 128, 125, 122, 120, 119, 121, 123, 126, 129, 132]
    result = compute_daily_tunnel_score(_frame(prices))

    assert result["cycle_state"] == "down_to_up_turn"
    assert result["label"] == "拐点观察"
    assert result["turning_point_score"] >= 0.65
    assert result["longest_up_cycle_days"] >= 15
    assert result["score"] >= 55


def test_launch_score_exposes_cycle_tunnel_and_base_score() -> None:
    row = _score_frame("TEST", _frame([100 + index * 0.8 for index in range(60)]), 0.5)

    assert row is not None
    assert row["daily_tunnel"]["score"] >= 85
    assert row["daily_tunnel"]["cycle_state"] == "up_cycle"
    assert 0 <= row["base_launch_score"] <= 1
    assert 0 <= row["launch_score"] <= 1
