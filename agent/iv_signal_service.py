"""ATM implied-volatility capture + the IV-rise launch signal.

User thesis: after a pullback completes and the stock is about to launch, its
implied volatility rises.  We capture that by logging each candidate's ATM IV
daily (``iv_history``) and deriving an ``iv_rise`` trend.  IV is the *rise*, not
the level, so it needs a few days of history to populate -- same as the OOS
signal log.  Until then ``iv_state`` reads "IV数据积累中".

Honest note: ``iv_rise`` is surfaced as context now; turning it into a *validated
ranking* signal requires the same calibration treatment (does "oversold + IV
rising" beat baseline?) once enough history accrues.  Keep ASCII for code.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Optional

from app_database import iv_history_log_many, iv_history_recent
from market_data_service import get_daily_history

IV_RISE_LOOKBACK = 5  # trading days


def _finite(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def fetch_atm_iv(symbol: str, spot: Optional[float] = None) -> Optional[float]:
    """Return the ATM implied volatility from the nearest option expiry, or None."""
    try:
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        expiries = list(ticker.options or [])
        if not expiries:
            return None
        if spot is None:
            frame, _src = get_daily_history(symbol, period="1mo")
            if frame.empty or "Close" not in frame:
                return None
            spot = float(frame["Close"].dropna().iloc[-1])
        if not spot or spot <= 0:
            return None
        chain = ticker.option_chain(expiries[0])
        ivs = []
        for side in (chain.calls, chain.puts):
            if side is None or side.empty or "impliedVolatility" not in side or "strike" not in side:
                continue
            idx = (side["strike"] - spot).abs().idxmin()
            iv = _finite(side.loc[idx, "impliedVolatility"])
            if iv is not None and 0.01 <= iv <= 5.0:
                ivs.append(iv)
        return round(sum(ivs) / len(ivs), 4) if ivs else None
    except Exception:
        return None


def log_candidate_iv(symbols: list[str], limit: int = 30) -> dict[str, Any]:
    """Fetch + log ATM IV for up to ``limit`` symbols for today (idempotent)."""
    as_of = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    items: list[dict[str, Any]] = []
    for symbol in symbols[: max(0, int(limit))]:
        iv = fetch_atm_iv(symbol)
        if iv is None:
            continue
        items.append({"symbol": symbol, "as_of_date": as_of, "atm_iv": iv})
    logged = iv_history_log_many(items)
    return {"as_of_date": as_of, "attempted": min(len(symbols), limit), "captured": len(items), "logged_new": logged}


def iv_features(symbol: str) -> dict[str, Any]:
    """Derive current IV + IV-rise trend + a launch-context label from history."""
    recent = iv_history_recent(symbol, IV_RISE_LOOKBACK + 2)
    if not recent:
        return {"atm_iv": None, "iv_rise": None, "iv_state": "IV数据积累中"}
    latest = _finite(recent[0].get("atm_iv"))
    prior_row = recent[min(IV_RISE_LOOKBACK, len(recent) - 1)]
    prior = _finite(prior_row.get("atm_iv"))
    iv_rise = (latest / prior - 1.0) if (latest and prior and prior > 0) else None
    if iv_rise is None:
        state = "IV数据积累中"
    elif iv_rise > 0.05:
        state = "IV上升(临近启动)"
    elif iv_rise < -0.05:
        state = "IV回落"
    else:
        state = "IV平稳"
    return {
        "atm_iv": latest,
        "iv_rise": round(iv_rise, 4) if iv_rise is not None else None,
        "iv_state": state,
        "samples": len(recent),
    }
