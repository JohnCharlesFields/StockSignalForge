"""The live shadow ledger must never turn stale research into forward evidence."""

from datetime import datetime, timezone

import pandas as pd
import pytest

import app_database
import long_option_shadow_service as shadow


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setattr(app_database, "DB_PATH", tmp_path / "shadow.sqlite3")
    monkeypatch.setattr(app_database, "_INITIALIZED", False)


def _snapshot(symbol="AAA", side="P", status="signal"):
    return {"data_as_of": "2026-09-25", "generated_at": "2026-09-25T20:05:00+00:00",
            "rows": [{"symbol": symbol, "status": status, "side": side,
                      "spot": 100.0, "stock_data_as_of": "2026-09-25",
                      "benchmark_data_as_of": "2026-09-25", "net_consensus": -0.3}]}


def _bars(close=100.0):
    dates = pd.bdate_range("2026-09-25", periods=12)
    return pd.DataFrame({"Open": 100.0, "High": 115.0, "Low": 90.0,
                         "Close": close, "Volume": 1_000_000}, index=dates)


def test_first_prediction_is_immutable_and_all_horizons_recorded():
    now = datetime(2026, 9, 25, 21, tzinfo=timezone.utc)
    first = shadow.record_snapshot(_snapshot(), now=now)
    assert first == {"inserted": 4, "eligible": 1, "mode": "live",
                     "signal_date": "2026-09-25", "horizons": [1, 3, 5, 10]}
    changed = _snapshot(side="C")
    assert shadow.record_snapshot(changed, now=now)["inserted"] == 0
    with app_database.connection() as conn:
        rows = conn.execute("SELECT DISTINCT direction, mode FROM long_option_direction_predictions").fetchall()
    assert [tuple(row) for row in rows] == [("P", "live")]
    score = shadow.scorecard()
    assert score["recorded_signal_dates"] == 1
    assert score["independent_signal_dates"] == 0


def test_old_or_post_open_snapshot_never_counts_as_live():
    late = datetime(2026, 9, 28, 14, tzinfo=timezone.utc)
    assert shadow.record_snapshot(_snapshot(), now=late)["mode"] == "late_replay"
    score = shadow.scorecard()
    assert score["recorded"] == 0
    assert score["late_replay_excluded"] == 1


def test_stale_or_missing_benchmark_row_is_not_logged():
    row = _snapshot()
    row["rows"][0]["stock_data_as_of"] = "2026-09-24"
    assert shadow.record_snapshot(row, now=datetime(2026, 9, 25, 21, tzinfo=timezone.utc))["eligible"] == 0
    row["rows"][0]["stock_data_as_of"] = "2026-09-25"
    row["rows"][0]["benchmark_data_as_of"] = None
    assert shadow.record_snapshot(row, now=datetime(2026, 9, 25, 21, tzinfo=timezone.utc))["eligible"] == 0


def test_put_resolves_next_open_to_exit_close_without_future_leak(monkeypatch):
    shadow.record_snapshot(_snapshot(), now=datetime(2026, 9, 25, 21, tzinfo=timezone.utc))
    stock = _bars()
    stock.iloc[1, stock.columns.get_loc("Open")] = 110.0
    stock.iloc[5, stock.columns.get_loc("Close")] = 95.0
    spy = _bars()
    monkeypatch.setattr(shadow, "get_daily_history",
                        lambda symbol, **_kw: (spy if symbol == "SPY" else stock, "cache"))
    premature = shadow.resolve_pending(now=datetime(2026, 9, 29, 22, tzinfo=timezone.utc))
    assert premature["resolved"] == 1  # 1-day result only
    assert shadow.scorecard()["resolved"] == 0  # primary horizon is 5 days
    mature = shadow.resolve_pending(now=datetime(2026, 10, 6, 22, tzinfo=timezone.utc))
    assert mature["resolved"] >= 2
    with app_database.connection() as conn:
        row = conn.execute("""SELECT entry_date, exit_date, direction, underlying_return,
                                   signed_net, signed_spy_excess
                            FROM long_option_direction_predictions WHERE horizon_days=5""").fetchone()
    assert row["entry_date"] == "2026-09-28"
    assert row["exit_date"] == stock.index[5].date().isoformat()
    assert row["direction"] == "P"
    assert row["underlying_return"] == pytest.approx(95 / 110 - 1)
    assert row["signed_net"] == pytest.approx(-(95 / 110 - 1) - 0.0006)
    assert row["signed_spy_excess"] == pytest.approx(-(95 / 110 - 1) - 0.0006)
    assert shadow.scorecard()["put"]["hit_net"] == 1.0
    assert shadow.scorecard()["matched_always_call"]["delta_policy_minus_always_call"] > 0


def test_wait_records_coverage_but_not_a_directional_win(monkeypatch):
    shadow.record_snapshot(_snapshot(side=None, status="no_direction"),
                           now=datetime(2026, 9, 25, 21, tzinfo=timezone.utc))
    stock = _bars()
    stock.iloc[5, stock.columns.get_loc("Close")] = 105.0
    monkeypatch.setattr(shadow, "get_daily_history", lambda _symbol, **_kw: (stock, "cache"))
    shadow.resolve_pending(now=datetime(2026, 10, 6, 22, tzinfo=timezone.utc))
    score = shadow.scorecard()
    assert score["wait"]["n"] == 1
    assert score["wait"]["mean_abs_underlying_move"] == pytest.approx(0.05)
    assert score["directional"]["n"] == 0
    assert score["coverage"] == 0
    assert score["matched_always_call"]["policy_mean_per_candidate"] == 0


def test_scorecard_never_reads_market_data(monkeypatch):
    monkeypatch.setattr(shadow, "get_daily_history", lambda *_args, **_kw: (_ for _ in ()).throw(AssertionError("network")))
    assert shadow.scorecard()["evidence_status"] == "insufficient_live_sample"


def test_scheduled_cache_recorder_is_idempotent(monkeypatch, tmp_path):
    import long_option_screen_service as screen

    monkeypatch.setattr(screen, "SNAPSHOT_PATH", tmp_path / "nonexistent_snapshot.json")
    monkeypatch.setattr(screen, "_screen_one", lambda symbol, session, _benchmark: {
        "symbol": symbol, "status": "signal", "side": "C", "spot": 100.0,
        "net_consensus": 0.2, "stock_data_as_of": session.isoformat(),
        "benchmark_data_as_of": session.isoformat(),
    })
    monkeypatch.setattr(shadow, "get_daily_history", lambda symbol, **_kw: (_bars().iloc[:1], "cache"))
    now = datetime(2026, 9, 25, 21, tzinfo=timezone.utc)
    first = shadow.record_daily_from_cache(now=now)
    assert first["mode"] == "live"
    assert first["eligible"] == 7
    assert first["inserted"] == 28
    assert shadow.record_daily_from_cache(now=now)["reason"] == "already_recorded"
