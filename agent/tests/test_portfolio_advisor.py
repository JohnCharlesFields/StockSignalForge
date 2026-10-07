"""Tests for the portfolio holding decision engine. ASCII-only."""

from __future__ import annotations

from portfolio_advisor_service import ADD, CLOSE, HOLD, TRIM, decide_holding


def _base(**over):
    """A neutral HOLD-ish baseline; override fields per test."""
    args = dict(
        shares=100,
        avg_cost=100.0,
        current_price=102.0,
        atr=2.0,
        calibrated_win_rate=0.50,
        confidence_badge="low_edge",
        pullback_state="偏强(回调有限)",
        hv_state="波动平稳",
        relative_strength=0.0,
        regime_mult=1.0,
        available_cash=100000.0,
        total_equity=110000.0,
    )
    args.update(over)
    return decide_holding(**args)


def test_close_when_price_below_hard_stop() -> None:
    # 8% below cost (100) is 92; price 90 -> protective stop breached.
    d = _base(current_price=90.0)
    assert d["action"] == CLOSE
    assert "止损" in d["reason"]


def test_close_when_loss_exceeds_risk_budget() -> None:
    # equity 110k * 1.5% = 1650 risk budget. 1000 shares * (100-99) = 1000 < budget,
    # but a bigger position at a small loss above the hard stop can exceed it.
    d = _base(shares=2000, current_price=99.0, total_equity=110000.0)
    # loss = 2000 * 1 = 2000 >= 1650 -> CLOSE, and price 99 > hard stop 92.
    assert d["action"] == CLOSE


def test_trim_when_overconcentrated() -> None:
    # One position worth way more than 35% of equity.
    d = _base(shares=1000, current_price=100.0, avg_cost=80.0, total_equity=120000.0)
    # position 100k of 120k equity ~ 83% >> 35% -> TRIM (gain keeps it off the stop).
    assert d["action"] == TRIM
    assert d["levels"].get("trim_shares", 0) > 0


def test_trim_on_overextended_gain() -> None:
    d = _base(
        avg_cost=100.0, current_price=130.0, shares=10,
        pullback_state="强势追高(非回调)", total_equity=1000000.0,
    )
    # +30% gain, not a pullback, small position (not concentrated) -> TRIM to lock.
    assert d["action"] == TRIM


def test_add_when_validated_edge_and_pullback() -> None:
    d = _base(
        calibrated_win_rate=0.58,
        confidence_badge="validated",
        pullback_state="深度回调(超卖)",
        hv_state="波动扩张(临近启动)",
        regime_mult=1.0,
        current_price=100.0,
        avg_cost=100.0,
    )
    assert d["action"] == ADD
    assert d["add_qty"] >= 1
    assert d["levels"]["add_zone_low"] <= d["levels"]["add_zone_high"]


def test_no_add_when_regime_risk_off() -> None:
    d = _base(
        calibrated_win_rate=0.58,
        confidence_badge="validated",
        pullback_state="深度回调(超卖)",
        regime_mult=0.5,  # risk-off gate
    )
    assert d["action"] == HOLD
    assert "择时" in d["reason"]


def test_no_add_when_winrate_below_threshold() -> None:
    d = _base(
        calibrated_win_rate=0.49,
        confidence_badge="validated",
        pullback_state="深度回调(超卖)",
        regime_mult=1.0,
    )
    assert d["action"] == HOLD


def test_hold_default_above_stop() -> None:
    d = _base()
    assert d["action"] == HOLD
    assert d["levels"]["stop_price"] > 0
    assert d["levels"]["take_profit"] > d["levels"]["stop_price"]


def test_stop_lifts_to_breakeven_in_profit() -> None:
    # Up 10% -> stop should be at least breakeven (cost), not 8% below cost.
    d = _base(avg_cost=100.0, current_price=110.0, atr=1.0)
    assert d["levels"]["stop_price"] >= 100.0


def test_missing_price_is_safe() -> None:
    d = _base(current_price=0.0)
    assert d["action"] == HOLD
    assert d["action_cn"] == "数据不足"
