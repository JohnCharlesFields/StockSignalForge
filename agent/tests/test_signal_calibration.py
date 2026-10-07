"""Tests for the R1 signal -> outcome -> calibration loop.

Kept ASCII-only so Windows terminals and container builds stay clean.
"""

from __future__ import annotations

import pandas as pd
import pytest

import app_database
import signal_calibration as sc


# --------------------------------------------------------------------------- #
# Pure calibration math
# --------------------------------------------------------------------------- #

def _synthetic_events(n: int = 300) -> list[dict]:
    """Monotone synthetic events: higher score -> more positive excess."""
    events = []
    for i in range(n):
        score = i / (n - 1)
        excess = (score - 0.5) * 0.10  # -0.05 .. +0.05
        events.append({
            "score": score,
            "forward_return": excess + 0.01,
            "excess_return": excess,
            "ticker": f"T{i % 12}",
        })
    return events


def test_pav_is_monotonic_non_decreasing() -> None:
    curve = sc.build_calibration(_synthetic_events(), signal_type="launch", horizon=5, cost_bps=0.0)
    iso = [bucket["p_iso"] for bucket in curve["buckets"]]
    assert iso == sorted(iso)
    # Bottom bucket should be near 0, top bucket near 1 for this clean signal.
    assert iso[0] <= 0.1
    assert iso[-1] >= 0.9


def test_buckets_have_support_and_wilson_ci() -> None:
    curve = sc.build_calibration(_synthetic_events(), signal_type="launch", horizon=5, cost_bps=0.0)
    assert curve["global"]["events"] == 300
    assert curve["global"]["clusters"] == 12
    for bucket in curve["buckets"]:
        assert bucket["n"] >= sc.DEFAULT_MIN_BUCKET_N
        assert 0.0 <= bucket["ci_low"] <= bucket["hit_rate"] <= bucket["ci_high"] <= 1.0


def test_cost_reduces_hit_rate() -> None:
    free = sc.build_calibration(_synthetic_events(), signal_type="launch", horizon=5, cost_bps=0.0)
    pricey = sc.build_calibration(_synthetic_events(), signal_type="launch", horizon=5, cost_bps=200.0)
    assert pricey["global"]["hit_rate"] < free["global"]["hit_rate"]
    assert pricey["global"]["mean_excess_net"] < free["global"]["mean_excess_net"]


def test_apply_calibration_step_lookup() -> None:
    curve = sc.build_calibration(_synthetic_events(), signal_type="launch", horizon=5, cost_bps=0.0)
    high = sc.apply_calibration(curve, 0.95)
    low = sc.apply_calibration(curve, 0.05)
    assert high["support_ok"] is True
    assert high["p_calibrated"] >= low["p_calibrated"]
    # Out-of-range score snaps to nearest edge bucket, still returns a value.
    assert sc.apply_calibration(curve, 5.0)["p_calibrated"] is not None


def test_daily_tunnel_score_normalized_to_unit_axis() -> None:
    # daily_tunnel scores are 0..100; normalize must map into 0..1.
    assert sc.normalize_score("daily_tunnel", 80) == pytest.approx(0.8)
    assert sc.normalize_score("launch", 0.8) == pytest.approx(0.8)


# --------------------------------------------------------------------------- #
# Persistence + calibrate() read path
# --------------------------------------------------------------------------- #

@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    db = tmp_path / "calib_test.sqlite3"
    monkeypatch.setattr(app_database, "DB_PATH", db)
    monkeypatch.setattr(app_database, "_INITIALIZED", False)
    app_database.ensure_database()
    return app_database


def test_calibrate_falls_back_to_sigmoid_without_curve(temp_db) -> None:
    result = sc.calibrate("launch", 0.8, 5)
    assert result["source"] == "fallback_sigmoid"
    assert result["support_ok"] is False
    assert result["p_calibrated"] == pytest.approx(sc.legacy_sigmoid(0.8), rel=1e-6)


def test_calibrate_uses_active_curve_when_supported(temp_db) -> None:
    curve = sc.build_calibration(_synthetic_events(), signal_type="launch", horizon=5, cost_bps=0.0)
    temp_db.signal_calibration_upsert(
        signal_type="launch", horizon_days=5, curve=curve,
        event_count=curve["global"]["events"], cluster_count=curve["global"]["clusters"],
        cost_bps=0.0, make_active=True,
    )
    result = sc.calibrate("launch", 0.9, 5)
    assert result["source"] == "calibrated"
    assert result["support_ok"] is True
    assert result["p_calibrated"] >= 0.8


def test_calibration_activation_is_exclusive(temp_db) -> None:
    curve = sc.build_calibration(_synthetic_events(), signal_type="launch", horizon=5, cost_bps=0.0)
    first = temp_db.signal_calibration_upsert(signal_type="launch", horizon_days=5, curve=curve, make_active=True)
    second = temp_db.signal_calibration_upsert(signal_type="launch", horizon_days=5, curve=curve, make_active=True)
    active = temp_db.signal_calibration_active("launch", 5)
    assert active["calibration_id"] == second
    assert first != second
    all_rows = temp_db.signal_calibration_list()
    active_ids = [row["calibration_id"] for row in all_rows if row["is_active"]]
    assert active_ids == [second]


# --------------------------------------------------------------------------- #
# Event logging + resolution flow
# --------------------------------------------------------------------------- #

def test_event_log_is_idempotent(temp_db) -> None:
    events = [{
        "signal_type": "launch", "symbol": "AAPL", "as_of_date": "2025-01-15",
        "horizon_days": 5, "score": 0.7, "ref_price": 100.0, "run_id": "r1",
    }]
    assert temp_db.signal_event_log_many(events) == 1
    assert temp_db.signal_event_log_many(events) == 0  # same key -> no duplicate
    assert temp_db.signal_events_count() == {"total": 1, "resolved": 0, "pending": 1}


def test_resolve_flow_fills_forward_returns(temp_db, monkeypatch) -> None:
    import scripts.resolve_signal_events as resolver

    dates = pd.bdate_range("2025-01-01", periods=60)
    frame = pd.DataFrame({"Close": [100.0 + i for i in range(60)]}, index=dates)
    monkeypatch.setattr(resolver, "get_daily_history", lambda symbol, period="2y": (frame, "test"))

    as_of = dates[10].strftime("%Y-%m-%d")
    temp_db.signal_event_log_many([{
        "signal_type": "launch", "symbol": "AAPL", "as_of_date": as_of,
        "horizon_days": 5, "score": 0.7, "ref_price": 110.0, "run_id": "r1",
    }])

    summary = resolver.resolve_pending_events(limit=10, history_period="2y")
    assert summary["resolved"] == 1
    assert summary["counts"] == {"total": 1, "resolved": 1, "pending": 0}

    resolved = temp_db.signal_events_resolved(signal_type="launch", horizon_days=5)
    assert len(resolved) == 1
    # Close rises by 1/bar: entry=110, exit=115 -> +5/110.
    assert resolved[0]["forward_return"] == pytest.approx(5.0 / 110.0, rel=1e-6)


def test_resolve_skips_events_without_elapsed_horizon(temp_db, monkeypatch) -> None:
    import scripts.resolve_signal_events as resolver

    dates = pd.bdate_range("2025-01-01", periods=12)
    frame = pd.DataFrame({"Close": [100.0 + i for i in range(12)]}, index=dates)
    monkeypatch.setattr(resolver, "get_daily_history", lambda symbol, period="2y": (frame, "test"))

    # as_of near the end so entry + horizon runs past the available history.
    as_of = dates[10].strftime("%Y-%m-%d")
    temp_db.signal_event_log_many([{
        "signal_type": "launch", "symbol": "AAPL", "as_of_date": as_of,
        "horizon_days": 5, "score": 0.7, "ref_price": 110.0, "run_id": "r1",
    }])
    summary = resolver.resolve_pending_events(limit=10)
    assert summary["resolved"] == 0
    assert summary["not_ready"] == 1


def _inverted_events(n: int = 600) -> list[dict]:
    """Synthetic events where LOW raw score carries positive excess."""
    events = []
    for i in range(n):
        raw = i / (n - 1)
        excess = (0.5 - raw) * 0.10  # low raw -> positive excess
        events.append({
            "score": raw,
            "forward_return": excess + 0.01,
            "excess_return": excess,
            "ticker": f"T{i % 15}",
        })
    return events


def test_contrarian_normalization_inverts() -> None:
    assert sc.normalize_score("launch_contrarian", 0.8) == pytest.approx(0.2)
    assert sc.normalize_score("daily_tunnel_contrarian", 80) == pytest.approx(0.2)


def test_contrarian_curve_validates_edge_when_raw_does_not() -> None:
    events = _inverted_events()
    contrarian = sc.build_calibration(events, signal_type="launch_contrarian", horizon=5, cost_bps=3.0)
    raw = sc.build_calibration(events, signal_type="launch", horizon=5, cost_bps=3.0)
    # Contrarian top bucket (= low raw) should show positive excess with CI > 0.
    assert contrarian["global"]["edge"]["validated"] is True
    assert contrarian["buckets"][-1]["excess_ci_low"] > 0
    # The raw signal on the same data must NOT validate (its top bucket loses).
    assert raw["global"]["edge"]["validated"] is False
