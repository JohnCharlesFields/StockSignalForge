from __future__ import annotations

import pandas as pd

from distribution_risk_service import detect_distribution_risk


def _frame(last):
    rows = []
    price = 100.0
    for i in range(59):
        price += 0.35
        rows.append({
            "Open": price - 0.2,
            "High": price + 0.5,
            "Low": price - 0.6,
            "Close": price,
            "Volume": 1_000_000,
        })
    rows.append(last)
    return pd.DataFrame(rows)


def test_high_volume_upper_wick_stalling_triggers_distribution_warning():
    frame = _frame({
        "Open": 121.0,
        "High": 126.5,
        "Low": 119.8,
        "Close": 120.4,
        "Volume": 2_700_000,
    })

    risk = detect_distribution_risk(frame)

    assert risk["available"] is True
    assert risk["triggered"] is True
    assert risk["level"] in {"medium", "high"}
    assert risk["volume_ratio"] >= 1.8
    assert risk["upper_shadow_pct"] >= 0.35


def test_healthy_high_volume_breakout_does_not_trigger_distribution_warning():
    frame = _frame({
        "Open": 121.0,
        "High": 126.5,
        "Low": 120.6,
        "Close": 126.1,
        "Volume": 2_700_000,
    })

    risk = detect_distribution_risk(frame)

    assert risk["available"] is True
    assert risk["triggered"] is False
    assert risk["level"] == "none"
