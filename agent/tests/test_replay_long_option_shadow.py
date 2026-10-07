"""Historical replay is separate from, and comparable with, the live ledger."""

from datetime import date

import pandas as pd
import pytest

from consensus_signal_service import consensus_feature_frame
from scripts import replay_long_option_shadow as replay


def _bars(days, *, entry_open=100.0, exit_close=100.0, event_pos=280):
    bars = pd.DataFrame({"Open": 100.0, "High": 125.0, "Low": 85.0,
                         "Close": 100.0, "Volume": 1_000_000}, index=days)
    bars.iloc[event_pos + 1, bars.columns.get_loc("Open")] = entry_open
    bars.iloc[event_pos + 5, bars.columns.get_loc("Close")] = exit_close
    return bars


def test_rounding_matches_live_direction_threshold():
    assert replay._direction(0.12004) == "WAIT"
    assert replay._direction(0.12006) == "C"
    assert replay._direction(-0.12006) == "P"


def test_replay_records_call_put_wait_and_uses_next_open(monkeypatch):
    days = pd.bdate_range("2024-01-02", periods=330)
    target = days[280].date()
    spy = _bars(days)
    call = _bars(days, entry_open=110.0, exit_close=121.0)
    put = _bars(days, entry_open=100.0, exit_close=90.0)
    wait = _bars(days, entry_open=100.0, exit_close=105.0)
    for name, frame in (("AAA", call), ("BBB", put), ("CCC", wait)):
        frame.attrs["symbol"] = name

    def features(frame, _benchmark):
        value = {"AAA": 0.3, "BBB": -0.3, "CCC": 0.0}[frame.attrs["symbol"]]
        return pd.DataFrame({"net_consensus": value}, index=frame.index)

    monkeypatch.setattr(replay, "consensus_feature_frame", features)
    events, audit = replay.collect_replay_events({"SPY": spy, "AAA": call, "BBB": put, "CCC": wait},
                                                 start=target, end=target)
    assert audit["symbols"]["AAA"]["eligible_days"] == 1
    assert {row["direction"] for row in events} == {"C", "P", "WAIT"}
    by_symbol = {row["symbol"]: row for row in events}
    assert by_symbol["AAA"]["signed_net_5"] == pytest.approx(0.10 - 0.0006)
    assert by_symbol["BBB"]["signed_net_5"] == pytest.approx(0.10 - 0.0006)
    assert by_symbol["CCC"]["signed_net_5"] is None
    assert by_symbol["CCC"]["policy_net_5"] == 0
    summary = replay.summarize(events, 5, bootstrap=0)
    assert summary["coverage"] == pytest.approx(2 / 3)
    assert summary["call"]["n"] == 1
    assert summary["put"]["n"] == 1
    assert summary["wait"]["n"] == 1


def test_missing_benchmark_and_split_path_are_excluded(monkeypatch):
    days = pd.bdate_range("2024-01-02", periods=330)
    target = days[280].date()
    stock = _bars(days)
    stock.attrs["symbol"] = "AAA"
    monkeypatch.setattr(replay, "consensus_feature_frame",
                        lambda frame, _benchmark: pd.DataFrame({"net_consensus": 0.3}, index=frame.index))
    spy = _bars(days).drop(days[285])
    events, audit = replay.collect_replay_events({"SPY": spy, "AAA": stock}, start=target, end=target)
    assert events == []
    assert audit["symbols"]["AAA"]["missing_spy"] == 1
    stock.loc[days[281], ["Open", "High", "Low", "Close"]] = [50.0, 52.0, 49.0, 51.0]
    events, audit = replay.collect_replay_events({"SPY": _bars(days), "AAA": stock},
                                                 start=target, end=target)
    assert events == []
    assert audit["symbols"]["AAA"]["invalid_price_path"] == 1


def test_validation_exit_cannot_cross_into_next_phase():
    assert replay._phase(date(2024, 12, 20), date(2025, 1, 2)) is None
    assert replay._phase(date(2024, 12, 20), date(2024, 12, 31)) == "validation_2024"
    assert replay._phase(date(2025, 1, 2), date(2025, 1, 16)) == "retrospective_2025_plus_previously_inspected"


def test_full_history_feature_equals_live_280_bar_window():
    days = pd.bdate_range("2023-01-03", periods=340)
    close = pd.Series([100.0 + i * 0.04 + (i % 11) * 0.3 for i in range(len(days))], index=days)
    stock = pd.DataFrame({"Open": close - 0.2, "High": close + 1.1,
                          "Low": close - 1.2, "Close": close,
                          "Volume": [1_000_000 + (i % 7) * 40_000 for i in range(len(days))]}, index=days)
    spy = stock.copy()
    spy["Close"] = spy["Close"] * 0.98
    full = consensus_feature_frame(stock, spy)
    for index in (280, 315, 330):
        live = consensus_feature_frame(stock.iloc[:index + 1].tail(280),
                                       spy.iloc[:index + 1].tail(280))
        assert full["net_consensus"].iloc[index] == pytest.approx(live["net_consensus"].iloc[-1])


def test_missing_consensus_is_excluded_not_called_wait(monkeypatch):
    days = pd.bdate_range("2024-01-02", periods=330)
    target = days[280].date()
    stock = _bars(days)
    feature = pd.DataFrame({"net_consensus": 0.0}, index=days)
    feature.loc[days[280], "net_consensus"] = float("nan")
    monkeypatch.setattr(replay, "consensus_feature_frame", lambda *_args: feature)
    events, audit = replay.collect_replay_events({"SPY": _bars(days), "AAA": stock},
                                                 start=target, end=target)
    assert events == []
    assert audit["symbols"]["AAA"]["missing_signal"] == 1


def test_month_cluster_intervals_keep_overlapping_stock_days_together():
    events = [{"symbol": "AAA", "signal_date": f"2025-{month:02d}-03", "direction": "C",
               "signed_net_5": 0.02, "signed_spy_excess_5": 0.01,
               "underlying_return_5": 0.0206, "policy_net_5": 0.02,
               "always_call_net_5": 0.02}
              for month in range(1, 7)]
    result = replay.summarize(events, 5, bootstrap=100)
    assert result["ci95_policy_mean_month_cluster_descriptive"] == pytest.approx([0.02, 0.02])
    assert result["ci95_directional_hit_month_cluster_descriptive"] == pytest.approx([1.0, 1.0])
    assert result["ci95_delta_month_cluster_descriptive"] == pytest.approx([0.0, 0.0])


def test_replay_does_not_import_or_write_forward_database(monkeypatch):
    monkeypatch.setattr(replay, "_history", lambda _symbol: None)
    result = replay.run(("AAA",), bootstrap=0)
    assert result["status"] == "missing_spy"
