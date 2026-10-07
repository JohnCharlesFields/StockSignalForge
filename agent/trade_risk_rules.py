"""Simple trade risk rules for research-sized stock probes.

The module is intentionally small and dependency-free.  It is used as the
final gate after the existing signal and sizing logic has produced a proposed
stock probe.
"""

from __future__ import annotations

from typing import Any, Dict


# Central risk configuration.  Keep these constants together so the research
# workflow can tune the first version without touching the rule logic.
RISK_PER_TRADE = 0.015
MAX_POSITION_RATIO = 0.35
MAX_STOP_LOSS_PCT = 0.05
MIN_REWARD_RISK_RATIO = 2.0

ALLOW_OPEN = "ALLOW_OPEN"
REDUCE_QTY = "REDUCE_QTY"
BLOCK_OPEN = "BLOCK_OPEN"
HOLD = "HOLD"
EXIT_ALL = "EXIT_ALL"

_EPSILON = 1e-9
_PCT_TOLERANCE = 0.0005


def _base_open_payload(
    *,
    status: str,
    reason: str,
    equity: float,
    entry_price: float,
    stop_price: float,
    target_price: float,
    stop_loss_pct: float,
    risk_budget: float,
    reward_risk_ratio: float,
    requested_qty: int,
    allowed_qty: int,
) -> Dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "equity": round(equity, 2),
        "entry_price": round(entry_price, 2),
        "stop_price": round(stop_price, 2),
        "target_price": round(target_price, 2),
        "stop_loss_pct": round(stop_loss_pct, 6),
        "risk_budget": round(risk_budget, 2),
        "reward_risk_ratio": round(reward_risk_ratio, 4),
        "requested_qty": int(requested_qty),
        "allowed_qty": int(max(0, allowed_qty)),
    }


def validate_open_risk(
    *,
    equity: float,
    entry_price: float,
    stop_price: float,
    target_price: float,
    requested_qty: int,
) -> Dict[str, Any]:
    """Validate a proposed stock entry against the first-version risk rules."""
    equity = max(0.0, float(equity or 0.0))
    entry_price = float(entry_price or 0.0)
    stop_price = float(stop_price or 0.0)
    target_price = float(target_price or 0.0)
    requested_qty = max(0, int(requested_qty or 0))
    risk_budget = equity * RISK_PER_TRADE

    if entry_price <= 0:
        return _base_open_payload(
            status=BLOCK_OPEN,
            reason="买入价配置错误",
            equity=equity,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            stop_loss_pct=0.0,
            risk_budget=risk_budget,
            reward_risk_ratio=0.0,
            requested_qty=requested_qty,
            allowed_qty=0,
        )

    per_share_risk = entry_price - stop_price
    stop_loss_pct = per_share_risk / entry_price if entry_price else 0.0
    qty_by_risk = int(risk_budget / per_share_risk) if per_share_risk > 0 else 0
    qty_by_position = int(equity * MAX_POSITION_RATIO / entry_price) if entry_price > 0 else 0
    allowed_qty = min(qty_by_risk, qty_by_position)
    reward_risk_ratio = (target_price - entry_price) / per_share_risk if per_share_risk > 0 else 0.0

    if stop_price >= entry_price:
        return _base_open_payload(
            status=BLOCK_OPEN,
            reason="止损价配置错误",
            equity=equity,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            stop_loss_pct=stop_loss_pct,
            risk_budget=risk_budget,
            reward_risk_ratio=reward_risk_ratio,
            requested_qty=requested_qty,
            allowed_qty=0,
        )
    if stop_loss_pct > MAX_STOP_LOSS_PCT + _PCT_TOLERANCE:
        return _base_open_payload(
            status=BLOCK_OPEN,
            reason="止损距离超过 5%",
            equity=equity,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            stop_loss_pct=stop_loss_pct,
            risk_budget=risk_budget,
            reward_risk_ratio=reward_risk_ratio,
            requested_qty=requested_qty,
            allowed_qty=allowed_qty,
        )
    if reward_risk_ratio + _EPSILON < MIN_REWARD_RISK_RATIO:
        return _base_open_payload(
            status=BLOCK_OPEN,
            reason="预期盈亏比不足 2:1",
            equity=equity,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            stop_loss_pct=stop_loss_pct,
            risk_budget=risk_budget,
            reward_risk_ratio=reward_risk_ratio,
            requested_qty=requested_qty,
            allowed_qty=allowed_qty,
        )
    if allowed_qty < 1:
        return _base_open_payload(
            status=BLOCK_OPEN,
            reason="最小交易单位超过账户风险预算",
            equity=equity,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            stop_loss_pct=stop_loss_pct,
            risk_budget=risk_budget,
            reward_risk_ratio=reward_risk_ratio,
            requested_qty=requested_qty,
            allowed_qty=0,
        )
    if requested_qty > allowed_qty:
        return _base_open_payload(
            status=REDUCE_QTY,
            reason="原计划仓位过大",
            equity=equity,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            stop_loss_pct=stop_loss_pct,
            risk_budget=risk_budget,
            reward_risk_ratio=reward_risk_ratio,
            requested_qty=requested_qty,
            allowed_qty=allowed_qty,
        )
    return _base_open_payload(
        status=ALLOW_OPEN,
        reason="通过基础交易风控",
        equity=equity,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        stop_loss_pct=stop_loss_pct,
        risk_budget=risk_budget,
        reward_risk_ratio=reward_risk_ratio,
        requested_qty=requested_qty,
        allowed_qty=allowed_qty,
    )


def evaluate_exit_risk(
    *,
    equity: float,
    entry_price: float,
    current_price: float,
    stop_price: float,
    position_qty: int,
) -> Dict[str, Any]:
    """Evaluate whether an existing stock probe must be fully exited."""
    equity = max(0.0, float(equity or 0.0))
    entry_price = float(entry_price or 0.0)
    current_price = float(current_price or 0.0)
    stop_price = float(stop_price or 0.0)
    position_qty = max(0, int(position_qty or 0))
    risk_budget = equity * RISK_PER_TRADE
    unrealized_pnl = (current_price - entry_price) * position_qty
    unrealized_loss = abs(min(unrealized_pnl, 0.0))

    if current_price <= stop_price:
        status = EXIT_ALL
        reason = "价格跌破策略失效价"
    elif unrealized_loss >= risk_budget:
        status = EXIT_ALL
        reason = "单笔亏损达到风险预算"
    else:
        status = HOLD
        reason = "尚未触发退出条件"
    return {
        "status": status,
        "reason": reason,
        "equity": round(equity, 2),
        "entry_price": round(entry_price, 2),
        "current_price": round(current_price, 2),
        "stop_price": round(stop_price, 2),
        "position_qty": position_qty,
        "risk_budget": round(risk_budget, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "unrealized_loss": round(unrealized_loss, 2),
    }
