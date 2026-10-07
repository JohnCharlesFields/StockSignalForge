"""Tests for the shared trading-cost model (R4). ASCII-only."""

from __future__ import annotations

import importlib

import pytest

import cost_model


def test_default_one_way_and_round_trip() -> None:
    # 0 commission + 1.5 half-spread + 1.5 slippage = 3.0 bps one way.
    assert cost_model.equity_one_way_bps() == pytest.approx(3.0)
    # Round trip is 2x, expressed as a fraction of notional.
    assert cost_model.equity_round_trip_cost() == pytest.approx(2 * 3.0 / 10000.0)


def test_liquidity_multiplier_scales_thin_names() -> None:
    liquid = cost_model.equity_one_way_bps(avg_dollar_volume=500_000_000)
    thin = cost_model.equity_one_way_bps(avg_dollar_volume=5_000_000)
    mid = cost_model.equity_one_way_bps(avg_dollar_volume=30_000_000)
    assert liquid == pytest.approx(3.0)
    assert thin == pytest.approx(9.0)   # 3.0 x3
    assert mid == pytest.approx(6.0)    # 3.0 x2
    assert thin > mid > liquid


def test_low_price_premium() -> None:
    assert cost_model.equity_one_way_bps(price=100.0) == pytest.approx(3.0)
    assert cost_model.equity_one_way_bps(price=3.0) == pytest.approx(5.0)   # +2
    assert cost_model.equity_one_way_bps(price=1.0) == pytest.approx(8.0)   # +5


def test_option_trade_cost() -> None:
    # 2 legs, 1 contract, round trip: 0.65 * 2 * 1 * 2 = 2.60
    assert cost_model.option_trade_cost(2) == pytest.approx(2.60)
    # single leg one way
    assert cost_model.option_trade_cost(1, round_trip=False) == pytest.approx(0.65)
    # multiple contracts
    assert cost_model.option_trade_cost(2, contracts=3) == pytest.approx(7.80)


def test_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("COST_EQUITY_HALF_SPREAD_BPS", "5")
    monkeypatch.setenv("COST_EQUITY_SLIPPAGE_BPS", "5")
    monkeypatch.setenv("COST_OPTION_COMMISSION_PER_CONTRACT", "1.0")
    importlib.reload(cost_model)
    try:
        assert cost_model.equity_one_way_bps() == pytest.approx(10.0)
        assert cost_model.option_trade_cost(2) == pytest.approx(4.0)
    finally:
        monkeypatch.undo()
        importlib.reload(cost_model)


def test_cost_summary_shape() -> None:
    summary = cost_model.cost_summary()
    assert "equity" in summary and "option" in summary
    assert summary["equity"]["one_way_bps_liquid"] == pytest.approx(3.0)
    assert summary["option"]["round_trip_single_contract_2leg"] == pytest.approx(2.60)
