from __future__ import annotations

import pandas as pd
import pytest

import exit_model


def _series(vals):
    idx = pd.bdate_range("2025-01-01", periods=len(vals))
    return pd.Series(vals, index=idx)


def test_close_to_open_uses_open_at_exit(monkeypatch):
    monkeypatch.setenv("SIGNAL_EXIT_MODE", "close_to_open")
    close = _series([100.0, 101.0, 102.0, 103.0, 104.0])
    open_ = _series([100.0, 110.0, 120.0, 130.0, 140.0])
    # entry=close[0]=100, exit=open[0+3]=130 -> +0.30
    assert exit_model.forward_return(close, open_, 0, 3) == pytest.approx(0.30)


def test_close_to_close_legacy_mode(monkeypatch):
    monkeypatch.setenv("SIGNAL_EXIT_MODE", "close_to_close")
    close = _series([100.0, 101.0, 102.0, 103.0, 104.0])
    open_ = _series([100.0, 110.0, 120.0, 130.0, 140.0])
    # legacy: exit=close[3]=103 -> +0.03 (open ignored)
    assert exit_model.forward_return(close, open_, 0, 3) == pytest.approx(0.03)


def test_open_none_falls_back_to_close(monkeypatch):
    monkeypatch.setenv("SIGNAL_EXIT_MODE", "close_to_open")
    close = _series([100.0, 101.0, 102.0, 103.0])
    assert exit_model.forward_return(close, None, 0, 2) == pytest.approx(0.02)


def test_horizon_past_end_returns_none():
    close = _series([100.0, 101.0, 102.0])
    assert exit_model.forward_return(close, None, 1, 5) is None


def test_baseline_spans_all_entry_days(monkeypatch):
    monkeypatch.setenv("SIGNAL_EXIT_MODE", "close_to_open")
    close = _series([100.0, 100.0, 100.0, 100.0, 100.0])
    open_ = _series([100.0, 100.0, 100.0, 100.0, 100.0])
    base = exit_model.baseline_forward_returns(close, open_, 2)
    assert len(base) == 3  # entries at i=0,1,2 (i+2 < 5)
    assert all(abs(b) < 1e-9 for b in base)


def test_aligned_open_reindexes_to_close():
    idx = pd.bdate_range("2025-01-01", periods=4)
    frame = pd.DataFrame({"Open": [1.0, 2.0, 3.0, 4.0], "Close": [10.0, 20.0, 30.0, 40.0]}, index=idx)
    close = frame["Close"].dropna()
    op = exit_model.aligned_open(frame, close)
    assert list(op.values) == [1.0, 2.0, 3.0, 4.0]


def test_aligned_open_missing_column_returns_none():
    idx = pd.bdate_range("2025-01-01", periods=3)
    frame = pd.DataFrame({"Close": [10.0, 20.0, 30.0]}, index=idx)
    assert exit_model.aligned_open(frame, frame["Close"]) is None
