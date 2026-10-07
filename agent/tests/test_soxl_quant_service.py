from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, "agent")

from soxl_quant_service import DEFAULT_CONFIG, SYMBOL, prepare_bars, simulate


def _fixture_bbo() -> pd.DataFrame:
    rows: list[dict[str, float]] = []
    start = datetime(2026, 1, 5, 20, 0, tzinfo=timezone.utc)
    rng = np.random.default_rng(7)
    for session in range(4):
        base = 30.0 + session * 0.5
        for minute in range(120):
            # Alternating drift creates both positive and negative score regimes.
            drift = 0.0015 if session % 2 == 0 else -0.0015
            price = base * np.exp(drift * minute + rng.normal(0, 0.0008))
            rows.append(
                {
                    "timestamp": start + timedelta(days=session, minutes=minute),
                    "bid_px_00": price - 0.01,
                    "ask_px_00": price + 0.01,
                    "bid_sz_00": 100.0,
                    "ask_sz_00": 100.0,
                }
            )
    frame = pd.DataFrame(rows).set_index("timestamp")
    return frame


def test_soxl_features_are_point_in_time_and_symbol_is_fixed():
    bars = prepare_bars(_fixture_bbo(), DEFAULT_CONFIG)
    assert not bars.empty
    assert bars.index.is_monotonic_increasing
    assert {SYMBOL} == {"SOXL"}
    assert "signal_score" in bars.columns
    assert bars["signal_score"].notna().any()


def test_soxl_simulation_allows_only_long_and_short_and_returns_audit_fields():
    result = simulate(_fixture_bbo(), DEFAULT_CONFIG, initial_capital=2000.0)
    assert result["initial_capital"] == 2000.0
    assert result["final_equity"] >= 0
    assert set(result["side_stats"]) == {"LONG", "SHORT"}
    assert all(trade["symbol"] == "SOXL" for trade in result["trades"])
    assert all(trade["side"] in {"LONG", "SHORT"} for trade in result["trades"])
