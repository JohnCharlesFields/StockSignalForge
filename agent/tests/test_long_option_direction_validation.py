"""Causal and accounting checks for offline long-option direction research."""

from datetime import date

import pandas as pd
import pytest

from consensus_signal_service import consensus_feature_frame
from scripts.validate_long_option_direction import (
    collect_events,
    directional_outcome,
    selected_rules,
    split_for,
    summarize,
    valid_price_window,
)


def test_put_proxy_rewards_underlying_decline_and_is_not_option_pnl():
    gross, net = directional_outcome(-1, 100.0, 95.0, 0.0006)
    assert gross == pytest.approx(0.05)
    assert net == pytest.approx(0.0494)
    assert directional_outcome(1, 100.0, 95.0, 0.0006)[1] < 0


def test_zero_or_missing_evidence_does_not_confirm_direction():
    assert selected_rules(1, 0.25, 0.20, 0.03) == {
        "baseline": True, "change_3d": True, "rs20_align": True, "change_and_rs20": True}
    assert selected_rules(-1, -0.25, -0.20, -0.03)["change_and_rs20"]
    assert not selected_rules(1, 0.25, float("nan"), 0.0)["change_and_rs20"]
    assert not selected_rules(0, 0.0, 0.0, 0.0)["baseline"]


def test_horizon_must_finish_before_validation_boundary():
    assert split_for(date(2024, 12, 13), date(2024, 12, 31)) == "validation"
    assert split_for(date(2024, 12, 13), date(2025, 1, 2)) is None
    assert split_for(date(2025, 1, 2), date(2025, 1, 16)) == "test"


def test_bad_price_window_rejects_unadjusted_split_and_missing_bar():
    bars = pd.DataFrame({"Open": [100, 101, 50], "High": [102, 103, 52],
                         "Low": [99, 100, 49], "Close": [101, 102, 51],
                         "Volume": [1000, 1200, 800]})
    assert not valid_price_window(bars, 0, 2)
    bars.loc[2, ["Open", "High", "Low", "Close"]] = [102, 104, 101, 103]
    assert valid_price_window(bars, 0, 2)
    bars.loc[1, "Volume"] = float("nan")
    assert not valid_price_window(bars, 0, 2)


def test_summary_coverage_and_directional_hit():
    rows = [{"symbol": "AAA", "signal_date": "2025-01-02", "side": "Call",
             "rules": {"baseline": True, "change_3d": True},
             "net_5": 0.02, "alpha_5": 0.01},
            {"symbol": "BBB", "signal_date": "2025-01-02", "side": "Put",
             "rules": {"baseline": True, "change_3d": False},
             "net_5": -0.01, "alpha_5": -0.02}]
    result = summarize(rows, "change_3d", 5, bootstrap=0)
    assert result["n"] == 1
    assert result["coverage"] == pytest.approx(0.5)
    assert result["delta_net_vs_baseline"] == pytest.approx(0.015)
    assert result["by_side"]["Call"]["hit_net"] == 1.0
    assert result["by_side"]["Put"]["n"] == 0


def test_consensus_value_unchanged_when_future_bars_are_appended():
    days = pd.bdate_range("2024-01-02", periods=260)
    close = pd.Series([100.0 + i * 0.1 for i in range(260)], index=days)
    bars = pd.DataFrame({"Open": close - 0.2, "High": close + 1,
                         "Low": close - 1, "Close": close, "Volume": 1_000_000})
    full = consensus_feature_frame(bars, bars)
    prefix = consensus_feature_frame(bars.iloc[:240], bars.iloc[:240])
    for name in ("net_consensus", "rs20", "hv20"):
        assert full[name].iloc[239] == pytest.approx(prefix[name].iloc[-1])


def test_event_uses_next_open_not_signal_close(monkeypatch):
    days = pd.bdate_range("2024-01-02", periods=245)
    stock = pd.DataFrame({"Open": 100.0, "High": 102.0, "Low": 98.0,
                          "Close": 100.0, "Volume": 1_000_000}, index=days)
    stock.loc[days[221], ["Open", "High"]] = [110.0, 112.0]
    stock.loc[days[225], ["Close", "High"]] = [105.0, 106.0]
    feature = pd.DataFrame({"net_consensus": 0.0, "rs20": 0.02}, index=days)
    feature.loc[days[220], "net_consensus"] = 0.25
    monkeypatch.setattr("scripts.validate_long_option_direction.consensus_feature_frame",
                        lambda _stock, _benchmark: feature)
    events, audit = collect_events({"SPY": stock, "AAA": stock})
    assert audit["symbols"]["AAA"]["signals"] == 1
    assert events[0]["signal_date"] == days[220].date().isoformat()
    assert events[0]["gross_5"] == pytest.approx(105.0 / 110.0 - 1.0)
