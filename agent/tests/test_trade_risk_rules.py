from trade_risk_rules import (
    BLOCK_OPEN,
    EXIT_ALL,
    REDUCE_QTY,
    evaluate_exit_risk,
    validate_open_risk,
)


def test_open_risk_reduces_oversized_quantity() -> None:
    result = validate_open_risk(
        equity=1900,
        entry_price=47.37,
        stop_price=45.00,
        target_price=52.11,
        requested_qty=20,
    )

    assert result["status"] == REDUCE_QTY
    assert result["allowed_qty"] == 12
    assert result["reason"] == "原计划仓位过大"


def test_open_risk_blocks_when_one_share_exceeds_risk_budget() -> None:
    result = validate_open_risk(
        equity=1900,
        entry_price=598.29,
        stop_price=568.38,
        target_price=658.11,
        requested_qty=1,
    )

    assert result["status"] == BLOCK_OPEN
    assert result["allowed_qty"] == 0
    assert result["reason"] == "最小交易单位超过账户风险预算"


def test_exit_risk_exits_when_price_breaks_stop() -> None:
    result = evaluate_exit_risk(
        equity=1900,
        entry_price=47.37,
        current_price=44.90,
        stop_price=45.00,
        position_qty=12,
    )

    assert result["status"] == EXIT_ALL
    assert result["reason"] == "价格跌破策略失效价"


def test_exit_risk_exits_when_loss_reaches_risk_budget() -> None:
    result = evaluate_exit_risk(
        equity=1900,
        entry_price=50,
        current_price=47,
        stop_price=46,
        position_qty=10,
    )

    assert result["status"] == EXIT_ALL
    assert result["unrealized_loss"] == 30
    assert result["risk_budget"] == 28.5
    assert result["reason"] == "单笔亏损达到风险预算"
