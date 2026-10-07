"""Per-symbol panic proxy for VIX-aware equity research.

This module does not attempt to recreate an exchange-grade single-name
implied-volatility index.  It builds a transparent Stock VIX Proxy from the
data the project already has: OHLCV, market proxy returns, GEX and event risk.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from macro_panic_service import get_vix_regime
from market_data_service import get_daily_history


def _frame_from_ohlcv(rows: Any) -> pd.DataFrame:
    if not isinstance(rows, list) or not rows:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    frame = pd.DataFrame(rows)
    rename = {
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    }
    frame = frame.rename(columns={key: value for key, value in rename.items() if key in frame.columns})
    if "date" in frame.columns:
        frame.index = pd.to_datetime(frame["date"], errors="coerce")
    elif "Date" in frame.columns:
        frame.index = pd.to_datetime(frame["Date"], errors="coerce")
    for column in ("Open", "High", "Low", "Close", "Volume"):
        if column not in frame:
            frame[column] = 0.0 if column == "Volume" else float("nan")
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["Close"]).sort_index()
    return frame[["Open", "High", "Low", "Close", "Volume"]]


def _frame_from_closes(closes: Any) -> pd.DataFrame:
    values = [float(value) for value in (closes or []) if value is not None and float(value or 0) > 0]
    if not values:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    return pd.DataFrame({
        "Open": values,
        "High": values,
        "Low": values,
        "Close": values,
        "Volume": [0.0] * len(values),
    })


def _market_returns() -> pd.Series:
    series: list[pd.Series] = []
    for symbol in ("SPY", "QQQ"):
        frame, _source = get_daily_history(symbol, period="4mo", allow_yfinance_fallback=True)
        close = pd.to_numeric(frame.get("Close"), errors="coerce").dropna() if not frame.empty else pd.Series(dtype=float)
        if len(close) >= 25:
            returns = close.pct_change().dropna()
            returns.name = symbol
            series.append(returns)
    if not series:
        return pd.Series(dtype=float)
    return pd.concat(series, axis=1).mean(axis=1).dropna()


def _annualized_vol(returns: pd.Series, window: int) -> float | None:
    if len(returns) < max(5, window):
        return None
    value = returns.rolling(window).std().iloc[-1]
    if pd.isna(value):
        return None
    return float(value * math.sqrt(252) * 100)


def _beta(stock_returns: pd.Series, market_returns: pd.Series) -> float | None:
    if stock_returns.empty or market_returns.empty:
        return None
    try:
        merged = pd.concat([stock_returns.rename("stock"), market_returns.rename("market")], axis=1).dropna()
    except ValueError:
        merged = pd.DataFrame()
    if len(merged) < 20:
        # Fallback for cached report snippets that have no real date index.
        n = min(len(stock_returns), len(market_returns))
        if n < 20:
            return None
        merged = pd.DataFrame({
            "stock": stock_returns.iloc[-n:].to_numpy(),
            "market": market_returns.iloc[-n:].to_numpy(),
        }).dropna()
    market_var = float(merged["market"].var())
    if market_var <= 0:
        return None
    return float(merged["stock"].cov(merged["market"]) / market_var)


def _level(score: float) -> str:
    if score >= 75:
        return "very_high"
    if score >= 60:
        return "high"
    if score >= 40:
        return "medium"
    if score >= 20:
        return "low"
    return "quiet"


def _level_cn(level: str) -> str:
    return {
        "very_high": "极高",
        "high": "高",
        "medium": "中",
        "low": "低",
        "quiet": "平静",
    }.get(level, "未知")


def stock_panic_proxy(
    symbol: str,
    opportunity: dict[str, Any] | None = None,
    macro_regime: dict[str, Any] | None = None,
    event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute per-symbol Stock VIX Proxy and panic score.

    Returns an empty-unavailable payload when not enough price data exists.
    """
    symbol = str(symbol or "").upper()
    opportunity = opportunity or {}
    macro_regime = macro_regime or get_vix_regime()
    event = event or {}

    frame = _frame_from_ohlcv(opportunity.get("recent_ohlcv"))
    if frame.empty:
        frame = _frame_from_closes(opportunity.get("recent_closes"))
    source = "opportunity_recent_ohlcv" if not frame.empty else "unavailable"
    if len(frame) < 8 and symbol:
        fetched, fetched_source = get_daily_history(symbol, period="4mo", allow_yfinance_fallback=True)
        if not fetched.empty:
            frame = fetched
            source = fetched_source
    close = pd.to_numeric(frame.get("Close"), errors="coerce").dropna() if not frame.empty else pd.Series(dtype=float)
    if len(close) < 8:
        return {
            "available": False,
            "symbol": symbol,
            "reason": "not_enough_price_history",
            "source": source,
        }

    returns = close.pct_change().dropna()
    hv20 = _annualized_vol(returns, 20)
    hv5 = _annualized_vol(returns, 5)
    hv_base = hv20 if hv20 is not None else hv5
    if hv_base is None:
        return {
            "available": False,
            "symbol": symbol,
            "reason": "not_enough_returns",
            "source": source,
        }
    high_window = min(60, len(close))
    high = float(close.rolling(high_window).max().iloc[-1])
    last = float(close.iloc[-1])
    drawdown_60d = max(0.0, (high - last) / high) if high > 0 else 0.0
    vol_accel = float(hv5 / hv20) if hv5 is not None and hv20 and hv20 > 0 else 1.0
    volume = pd.to_numeric(frame.get("Volume"), errors="coerce").dropna() if "Volume" in frame else pd.Series(dtype=float)
    volume_spike = 1.0
    if len(volume) >= 20 and float(volume.iloc[-20:].mean() or 0) > 0:
        volume_spike = float(volume.iloc[-1] / volume.iloc[-20:].mean())

    beta = _beta(returns, _market_returns())
    beta_for_calc = max(0.0, min(2.5, beta if beta is not None else 1.0))
    macro_value = float(macro_regime.get("value") or 20.0)
    gex = opportunity.get("gamma_exposure") or {}
    gex_risk = str(gex.get("risk_level") or "").lower()
    gex_regime = str(gex.get("regime") or "").lower()
    gex_penalty = 0.0
    if gex_risk == "high" or gex_regime == "negative_high":
        gex_penalty = 6.0
    elif gex_regime == "negative":
        gex_penalty = 3.0
    elif gex_regime == "positive":
        gex_penalty = -2.0
    event_signal = str(event.get("signal") or event.get("signal_cn") or "").lower()
    event_penalty = 6.0 if event_signal in {"avoid", "回避"} else 0.0

    stock_vix = (
        0.32 * macro_value * max(0.55, min(1.8, beta_for_calc))
        + 0.38 * hv_base
        + 12.0 * max(0.0, min(2.5, vol_accel - 1.0))
        + 45.0 * drawdown_60d
        + 2.5 * max(0.0, min(3.0, volume_spike - 1.0))
        + gex_penalty
        + event_penalty
    )
    stock_vix = max(5.0, min(120.0, stock_vix))
    panic_score = (
        0.30 * min(1.0, hv_base / 80.0)
        + 0.20 * min(1.0, max(0.0, vol_accel - 1.0) / 1.5)
        + 0.20 * min(1.0, drawdown_60d / 0.35)
        + 0.10 * min(1.0, beta_for_calc / 2.0)
        + 0.10 * min(1.0, max(0.0, volume_spike - 1.0) / 3.0)
        + 0.10 * min(1.0, max(0.0, gex_penalty + event_penalty) / 12.0)
    ) * 100.0
    panic_score = max(0.0, min(100.0, panic_score))
    level = _level(panic_score)
    drivers: list[str] = []
    if hv_base >= 55:
        drivers.append("high_realized_vol")
    if vol_accel >= 1.25:
        drivers.append("volatility_acceleration")
    if drawdown_60d >= 0.12:
        drivers.append("drawdown")
    if beta_for_calc >= 1.25:
        drivers.append("high_beta")
    if volume_spike >= 1.8:
        drivers.append("volume_spike")
    if gex_penalty > 0:
        drivers.append("gex_risk")
    if event_penalty > 0:
        drivers.append("event_risk")
    if not drivers:
        drivers.append("baseline_market_transmission")

    return {
        "available": True,
        "symbol": symbol,
        "stock_vix_equivalent": round(stock_vix, 2),
        "stock_panic_score": round(panic_score, 1),
        "panic_level": level,
        "panic_level_cn": _level_cn(level),
        "drivers": drivers,
        "hv5": round(float(hv5), 2) if hv5 is not None else None,
        "hv20": round(float(hv20), 2) if hv20 is not None else None,
        "vol_accel": round(vol_accel, 2),
        "drawdown_60d": round(drawdown_60d, 4),
        "beta_spy_qqq": round(float(beta), 3) if beta is not None else None,
        "volume_spike": round(volume_spike, 2),
        "gex_penalty": round(gex_penalty, 2),
        "event_penalty": round(event_penalty, 2),
        "macro_vix_value": macro_regime.get("value"),
        "macro_mode": macro_regime.get("mode"),
        "source": source,
        "method": "stock_realized_vol + vol_accel + drawdown + beta + volume + gex/event adjustment",
    }


def stock_panic_evidence(stock_panic: dict[str, Any] | None, macro_regime: dict[str, Any] | None = None) -> dict[str, Any]:
    """Convert Stock VIX Proxy into a research evidence component."""
    if not stock_panic or not stock_panic.get("available"):
        return {}
    score = float(stock_panic.get("stock_panic_score") or 0.0)
    macro_mode = str((macro_regime or {}).get("mode") or stock_panic.get("macro_mode") or "")
    gex_penalty = float(stock_panic.get("gex_penalty") or 0.0)
    event_penalty = float(stock_panic.get("event_penalty") or 0.0)
    if macro_mode == "crisis_guard" or event_penalty > 0:
        probability = 0.42 - min(0.05, score / 1000.0)
        label = "个股恐慌风险"
    elif macro_mode in {"panic_reclaim", "panic_watch"} and 45 <= score <= 82 and gex_penalty <= 3:
        probability = 0.52 + min(0.08, (score - 45.0) / 500.0)
        label = "个股恐慌承接"
    elif score >= 85:
        probability = 0.46
        label = "个股恐慌过热"
    else:
        probability = 0.50
        label = "个股恐慌中性"
    return {
        "id": "stock_vix_proxy",
        "label": label,
        "probability": max(0.35, min(0.62, probability)),
        "source_probability": max(0.35, min(0.62, probability)),
        "reliability": 0.62,
        "weight": 0.10,
        "stock_panic": stock_panic,
    }
