from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module():
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "backtest_sector_signal_from_components.py"
    spec = importlib.util.spec_from_file_location("sector_backtest", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_dynamic_sector_features_and_variants_are_reported():
    mod = _load_module()
    base_rows = [
        {
            "date": "2026-01-02",
            "sector_etf": "XLK",
            "components": ["AAA", "BBB", "CCC"],
            "signal_breadth": 0.2,
            "sector_signal_score": 0.35,
            "stock_vs_sector_ratio": 0.4,
            "market_liquid_rs_ratio": 0.5,
        },
        {
            "date": "2026-01-03",
            "sector_etf": "XLK",
            "components": ["AAA", "DDD", "EEE"],
            "signal_breadth": 0.5,
            "sector_signal_score": 0.55,
            "stock_vs_sector_ratio": 0.7,
            "market_liquid_rs_ratio": 0.8,
        },
    ]

    dynamic = mod._attach_dynamic_features(base_rows, lookback=1)
    assert dynamic[1]["fresh_component_ratio_5d"] > 0
    assert dynamic[1]["sector_diffusion_score"] > dynamic[0]["sector_diffusion_score"]

    rows = []
    for row in dynamic:
        item = dict(row)
        item.update({
            "sector_net_return": 0.01,
            "alpha_vs_spy": 0.002,
            "alpha_vs_qqq": 0.001,
            "gated_sector_score": item["sector_diffusion_score"],
        })
        rows.append(item)

    analysis = mod._analyze_horizon(rows)
    assert set(analysis["score_variants"]) == {"static", "diffusion", "gated"}
    assert analysis["score_variants"]["diffusion"]["daily_selection"]["top1_net_return"]["score_key"] == "sector_diffusion_score"
