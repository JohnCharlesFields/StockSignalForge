"""Focused tests for Shadow Account signal scanning."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from src.shadow_account.models import ShadowProfile, ShadowRule
from src.shadow_account.scanner import scan_today_signals


def _profile(entry_condition: dict[str, object] | None = None) -> ShadowProfile:
    """Build a minimal ShadowProfile for scanner tests."""
    rule = ShadowRule(
        rule_id="R1",
        human_text="momentum entry",
        entry_condition=entry_condition or {"market": "us"},
        exit_condition={},
        holding_days_range=(3, 7),
        support_count=5,
        coverage_rate=0.5,
        sample_trades=("AAPL@2026-01-01",),
    )
    return ShadowProfile(
        shadow_id="shadow_test",
        created_at="2026-01-01T00:00:00Z",
        journal_hash="hash",
        source_market="us",
        profitable_roundtrips=5,
        total_roundtrips=8,
        date_range=("2026-01-01", "2026-02-01"),
        profile_text="test profile",
        rules=(rule,),
        preferred_markets=("us",),
        typical_holding_days=(5.0, 7.0),
    )


def _bars(closes: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    """Create a dated OHLCV frame ending on the scanner target date."""
    index = pd.date_range("2026-04-01", periods=len(closes), freq="D")
    data: dict[str, list[float]] = {
        "open": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
    }
    if volumes is not None:
        data["volume"] = volumes
    return pd.DataFrame(data, index=index)


@pytest.mark.unit
def test_scan_today_signals_matches_price_features() -> None:
    profile = _profile()
    frames = {
        "AAPL": _bars(
            [10, 10.2, 10.4, 10.5, 10.7, 11.4],
            [100, 100, 100, 100, 100, 180],
        ),
    }

    matches = scan_today_signals(profile, target_date=date(2026, 4, 6), price_frames=frames)

    assert matches == [
        {
            "symbol": "AAPL",
            "market": "us",
            "rule_id": "R1",
            "reason": "R1 price features matched (hold 3-7d)",
        }
    ]


@pytest.mark.unit
def test_scan_today_signals_returns_no_match_when_features_fail() -> None:
    profile = _profile()
    frames = {
        "AAPL": _bars(
            [11.5, 11.2, 11.0, 10.9, 10.7, 10.5],
            [180, 160, 150, 140, 130, 100],
        ),
    }

    assert scan_today_signals(profile, target_date="2026-04-06", price_frames=frames) == []


@pytest.mark.unit
def test_scan_today_signals_skips_missing_and_empty_data() -> None:
    profile = _profile()
    frames = {"AAPL": pd.DataFrame(), "MSFT": pd.DataFrame({"open": [1, 2]})}

    assert scan_today_signals(profile, target_date="2026-04-06", price_frames=frames) == []


@pytest.mark.unit
def test_scan_today_signals_keeps_backwards_compatible_call_signature() -> None:
    profile = _profile()

    assert scan_today_signals(profile, target_date="2026-04-06", per_market=1) == []


@pytest.mark.unit
def test_scan_today_signals_respects_per_market_cap() -> None:
    profile = _profile({"market": "us", "prior_5d_return": (">", 0.05)})
    frame = _bars(
        [10, 10.2, 10.4, 10.5, 10.7, 11.4],
        [100, 100, 100, 100, 100, 180],
    )
    frames = {symbol: frame for symbol in ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL"]}

    matches = scan_today_signals(
        profile,
        target_date="2026-04-06",
        per_market=2,
        price_frames=frames,
    )

    assert [match["symbol"] for match in matches] == ["AAPL", "MSFT"]
