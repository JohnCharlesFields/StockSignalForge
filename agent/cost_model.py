"""Single source of truth for trading-cost assumptions (R4).

Before R4 there were three inconsistent cost assumptions in the codebase:
the R1 calibration used a flat 5bps, ``backtest/engines/global_equity.py``
hardcoded US as zero-commission + 5bps slippage, and the options EV in
``scripts/screening_framework_v2_optimized.py`` subtracted no commission at all.
This module centralises them so the validation numbers cannot be dismissed as a
cost artifact.

Defaults reflect a retail US setup (confirmed with the user):
* Equities: $0 commission, ~1.5bps half bid/ask spread + ~1.5bps slippage one
  way, scaled up for illiquid / low-priced names.
* Options: ~$0.65 per contract per leg; the bid/ask spread is already crossed
  by ``executable_price`` (bid-to-sell, ask-to-buy), so it is not re-added here.

All knobs are overridable via ``COST_*`` environment variables.  Keep this
module ASCII-only for clean Windows / container builds.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# One-way equity cost components, in basis points of notional.
def _equity_commission_bps() -> float:
    return _env_float("COST_EQUITY_COMMISSION_BPS", 0.0)


def _equity_half_spread_bps() -> float:
    return _env_float("COST_EQUITY_HALF_SPREAD_BPS", 1.5)


def _equity_slippage_bps() -> float:
    return _env_float("COST_EQUITY_SLIPPAGE_BPS", 1.5)


def _option_commission_per_contract() -> float:
    return _env_float("COST_OPTION_COMMISSION_PER_CONTRACT", 0.65)


def _liquidity_multiplier(avg_dollar_volume: Optional[float]) -> float:
    """Scale up costs for thinly traded names. 1.0 when liquidity is unknown."""
    if avg_dollar_volume is None or avg_dollar_volume <= 0:
        return 1.0
    if avg_dollar_volume < 10_000_000:
        return 3.0
    if avg_dollar_volume < 50_000_000:
        return 2.0
    if avg_dollar_volume < 200_000_000:
        return 1.3
    return 1.0


def _low_price_premium_bps(price: Optional[float]) -> float:
    """Sub-$5 names trade wider; add a small one-way premium."""
    if price is None or price <= 0:
        return 0.0
    if price < 2.0:
        return 5.0
    if price < 5.0:
        return 2.0
    return 0.0


def equity_one_way_bps(price: Optional[float] = None, avg_dollar_volume: Optional[float] = None) -> float:
    """One-way equity trading cost in basis points of notional."""
    base = _equity_commission_bps() + _equity_half_spread_bps() + _equity_slippage_bps()
    scaled = base * _liquidity_multiplier(avg_dollar_volume) + _low_price_premium_bps(price)
    return round(scaled, 4)


def equity_round_trip_cost(price: Optional[float] = None, avg_dollar_volume: Optional[float] = None) -> float:
    """Round-trip equity cost as a fraction of notional (enter + exit)."""
    return round(2.0 * equity_one_way_bps(price, avg_dollar_volume) / 10000.0, 8)


def option_trade_cost(num_legs: int, contracts: int = 1, round_trip: bool = True) -> float:
    """Total option commission in dollars.

    The spread is already crossed by executable bid/ask pricing, so only the
    per-contract commission is charged here.
    """
    legs = max(0, int(num_legs))
    qty = max(1, int(contracts))
    sides = 2 if round_trip else 1
    return round(_option_commission_per_contract() * legs * qty * sides, 4)


def cost_summary() -> Dict[str, Any]:
    """Return the active cost assumptions for display / audit."""
    return {
        "equity": {
            "commission_bps": _equity_commission_bps(),
            "half_spread_bps": _equity_half_spread_bps(),
            "slippage_bps": _equity_slippage_bps(),
            "one_way_bps_liquid": equity_one_way_bps(),
            "round_trip_cost_liquid": equity_round_trip_cost(),
            "liquidity_tiers": "<$10M x3, <$50M x2, <$200M x1.3",
        },
        "option": {
            "commission_per_contract": _option_commission_per_contract(),
            "spread": "crossed via executable bid/ask (not re-added)",
            "round_trip_single_contract_2leg": option_trade_cost(2),
        },
    }
