"""Tests for R3 portfolio-level timing gate. ASCII-only."""

from __future__ import annotations

import pandas as pd

import portfolio_timing_service as pts


def test_exposure_crisis_guard_goes_to_cash() -> None:
    exposure, regime, flags = pts._exposure_from_conditions(
        vix_mode="crisis_guard",
        spy_ok=True,
        qqq_ok=True,
        breadth50=0.9,
        breadth200=0.9,
    )

    assert exposure == 0.0
    assert regime == "cash_guard"
    assert "vix_crisis_guard" in flags


def test_exposure_risk_on_when_trend_and_breadth_are_strong() -> None:
    exposure, regime, flags = pts._exposure_from_conditions(
        vix_mode="normal",
        spy_ok=True,
        qqq_ok=True,
        breadth50=0.75,
        breadth200=0.65,
    )

    assert exposure == 1.0
    assert regime == "risk_on"
    assert flags == []


def test_exposure_risk_off_when_indexes_and_breadth_break() -> None:
    exposure, regime, flags = pts._exposure_from_conditions(
        vix_mode="normal",
        spy_ok=False,
        qqq_ok=False,
        breadth50=0.25,
        breadth200=0.25,
    )

    assert exposure == 0.25
    assert regime == "risk_off"
    assert "index_below_ma200" in flags
    assert "breadth_weak" in flags


def test_portfolio_timing_gate_uses_existing_market_data(monkeypatch) -> None:
    dates = pd.bdate_range(end="2026-06-16", periods=260)
    up = pd.DataFrame(
        {
            "Open": range(260),
            "High": range(260),
            "Low": range(260),
            "Close": [100 + i for i in range(260)],
            "Volume": [1_000_000] * 260,
        },
        index=dates,
    )

    monkeypatch.setattr(
        pts,
        "get_vix_regime",
        lambda force_refresh=False: {
            "available": True,
            "mode": "normal",
            "value": 18.0,
            "source_type": "real_vix",
        },
    )
    monkeypatch.setattr(pts, "get_daily_history", lambda symbol, period="1y", allow_yfinance_fallback=True: (up, "cache:fresh"))

    gate = pts.portfolio_timing_gate(force_refresh=True)

    assert gate["regime"] == "risk_on"
    assert gate["gross_exposure_multiplier"] == 1.0
    assert gate["index_trend"]["SPY"]["trend_ok"] is True
    assert gate["breadth"]["available"] is True

