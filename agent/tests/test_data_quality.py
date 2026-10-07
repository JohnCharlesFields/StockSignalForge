"""Tests for O2 data-quality classification and the PIT earnings seam. ASCII-only."""

from __future__ import annotations

import pandas as pd
import pytest

import market_data_service as mds


def test_classify_data_source_tiers() -> None:
    assert mds.classify_data_source("twelvedata:incremental")["tier"] == "primary"
    assert mds.classify_data_source("databento:EQUS.MINI:ohlcv-1d")["tier"] == "primary"
    assert mds.classify_data_source("cache:fresh")["tier"] == "cache"
    yf = mds.classify_data_source("yfinance:fallback")
    assert yf["tier"] == "fallback" and yf["is_fallback"] is True
    assert mds.classify_data_source("")["tier"] == "unknown"


def test_assess_data_quality_staleness() -> None:
    recent_idx = pd.bdate_range(end=pd.Timestamp.utcnow().tz_localize(None).normalize(), periods=30)
    fresh = pd.DataFrame({"Close": range(30)}, index=recent_idx)
    fresh_q = mds.assess_data_quality("AAPL", fresh, "twelvedata:incremental")
    assert fresh_q["is_stale"] is False
    assert fresh_q["tier"] == "primary"
    assert fresh_q["bars"] == 30

    old_idx = pd.bdate_range(end="2024-01-10", periods=30)
    stale = pd.DataFrame({"Close": range(30)}, index=old_idx)
    assert mds.assess_data_quality("AAPL", stale, "yfinance:fallback")["is_stale"] is True

    empty = mds.assess_data_quality("AAPL", pd.DataFrame(), "cache:ohlcv")
    assert empty["is_stale"] is True and empty["bars"] == 0


def test_aggregate_data_quality_flags_fallback_heavy() -> None:
    mostly_fallback = {
        "A": "yfinance:fallback", "B": "yfinance:bulk-fallback",
        "C": "yfinance:fallback", "D": "twelvedata:incremental",
    }
    agg = mostly_fallback and mds.aggregate_data_quality(mostly_fallback)
    assert agg["fallback_share"] == pytest.approx(0.75)
    assert agg["data_quality_limited"] is True

    mostly_primary = {"A": "twelvedata:incremental", "B": "cache:fresh", "C": "twelvedata:incremental"}
    agg2 = mds.aggregate_data_quality(mostly_primary)
    assert agg2["data_quality_limited"] is False


def test_pit_earnings_seam_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("EARNINGS_PIT_PROVIDER", raising=False)
    monkeypatch.delenv("EARNINGS_PIT_API_KEY", raising=False)
    assert mds.point_in_time_earnings_configured() is False
    assert mds.get_point_in_time_earnings("AAPL", "2025-01-01") is None
    assert mds.data_source_status()["paid_pit_earnings_configured"] is False
    assert mds.data_source_status()["cost_assumptions"]["equity"]["one_way_bps_liquid"] > 0


def test_data_source_status_reports_new_primary_feeds(monkeypatch) -> None:
    monkeypatch.setenv("TIINGO_API_KEY", "test-tiingo")
    monkeypatch.setenv("MASSIVE_API_KEY", "test-massive")
    status = mds.data_source_status()
    assert status["tiingo_configured"] is True
    assert status["massive_configured"] is True
    assert "tiingo" in status["ohlcv_priority"]
    assert "massive_grouped_daily" in status["ohlcv_priority"]
    assert status["source_quality_tiers"]["tiingo"] == "primary"
    assert status["source_quality_tiers"]["massive"] == "primary"
    assert status["source_quality_tiers"]["databento"] == "primary"


def test_pit_earnings_seam_raises_when_configured_but_unimplemented(monkeypatch) -> None:
    monkeypatch.setenv("EARNINGS_PIT_PROVIDER", "polygon")
    monkeypatch.setenv("EARNINGS_PIT_API_KEY", "test-key")
    assert mds.point_in_time_earnings_configured() is True
    with pytest.raises(NotImplementedError):
        mds.get_point_in_time_earnings("AAPL", "2025-01-01")
