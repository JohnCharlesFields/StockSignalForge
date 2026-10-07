"""Pullback rejection and confirmation rules for the three-layer signal system."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import pandas as pd


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class PullbackConfig:
    enable_pullback_rejection: bool = _env_bool("ENABLE_PULLBACK_REJECTION", True)
    enable_pullback_rejection_confirmation: bool = _env_bool("ENABLE_PULLBACK_REJECTION_CONFIRMATION", True)
    enable_pullback_ranking_adjustment: bool = _env_bool("ENABLE_PULLBACK_RANKING_ADJUSTMENT", True)
    enable_tunnel_fallback: bool = _env_bool("ENABLE_TUNNEL_FALLBACK", True)
    lower_shadow_ratio_threshold: float = float(os.getenv("LOWER_SHADOW_RATIO_THRESHOLD", "1.5"))
    lower_shadow_range_threshold: float = float(os.getenv("LOWER_SHADOW_RANGE_THRESHOLD", "0.30"))
    close_location_threshold: float = float(os.getenv("CLOSE_LOCATION_THRESHOLD", "0.55"))
    pullback_min_pct: float = float(os.getenv("PULLBACK_MIN_PCT", "0.04"))
    pullback_max_pct: float = float(os.getenv("PULLBACK_MAX_PCT", "0.30"))
    support_tolerance_pct: float = float(os.getenv("SUPPORT_TOLERANCE_PCT", "0.04"))
    confirmation_window_days: int = int(os.getenv("CONFIRMATION_WINDOW_DAYS", "3"))
    require_intraday_breakout: bool = _env_bool("REQUIRE_INTRADAY_BREAKOUT", True)
    strong_confirmation_requires_close_above_high: bool = _env_bool("STRONG_CONFIRMATION_REQUIRES_CLOSE_ABOVE_HIGH", True)
    hide_invalidated_by_default: bool = _env_bool("HIDE_INVALIDATED_BY_DEFAULT", True)


DEFAULT_PULLBACK_CONFIG = PullbackConfig()


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if pd.notna(number) else default


def _frame(rows: Any) -> pd.DataFrame:
    frame = pd.DataFrame(rows or [])
    rename = {col: str(col).capitalize() for col in frame.columns}
    frame = frame.rename(columns=rename)
    required = ["Open", "High", "Low", "Close"]
    for col in required:
        if col not in frame:
            return pd.DataFrame(columns=required)
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    if "Volume" in frame:
        frame["Volume"] = pd.to_numeric(frame["Volume"], errors="coerce").fillna(0)
    else:
        frame["Volume"] = 0
    return frame.dropna(subset=required).reset_index(drop=True)


def detect_pullback_rejection(rows: Any, ticker: str = "", config: PullbackConfig = DEFAULT_PULLBACK_CONFIG) -> dict[str, Any]:
    if not config.enable_pullback_rejection:
        return {"signal_name": "pullback_rejection", "signal_stage": "NONE", "score": 0.0, "risk_flags": ["disabled"]}
    frame = _frame(rows)
    if len(frame) < 12:
        return {"signal_name": "pullback_rejection", "signal_stage": "NONE", "score": 0.0, "risk_flags": ["insufficient_ohlcv"]}

    row = frame.iloc[-1]
    open_, high, low, close = (_num(row["Open"]), _num(row["High"]), _num(row["Low"]), _num(row["Close"]))
    eps = 1e-9
    candle_range = max(high - low, eps)
    body = max(abs(close - open_), eps)
    lower_shadow = max(min(open_, close) - low, 0.0)
    close_location = (close - low) / candle_range
    lower_shadow_ratio = lower_shadow / body
    lower_shadow_pct = lower_shadow / candle_range

    closes = frame["Close"]
    lookback = frame.tail(min(20, len(frame)))
    recent_high = float(lookback["High"].max())
    pullback_pct = (recent_high - close) / recent_high if recent_high > 0 else 0.0
    pullback_days = 0
    for value in reversed(closes.tolist()):
        if value < recent_high:
            pullback_days += 1
        else:
            break

    ma20 = float(closes.rolling(20, min_periods=min(20, len(closes))).mean().iloc[-1])
    ma5 = float(closes.rolling(5, min_periods=5).mean().iloc[-1]) if len(closes) >= 5 else close
    trend_status = "up_or_repairing" if config.enable_tunnel_fallback and (close >= ma20 * 0.97 or ma5 >= ma20) else "damaged"
    nearest_support = min([ma20, float(lookback["Low"].tail(10).min())], key=lambda x: abs(close - x)) if ma20 > 0 else low
    support_distance_pct = abs(close - nearest_support) / close if close > 0 else 1.0
    support_ok = support_distance_pct <= config.support_tolerance_pct

    score = 0.0
    if config.pullback_min_pct <= pullback_pct <= config.pullback_max_pct:
        score += 22
    elif pullback_pct > 0:
        score += 8
    if lower_shadow_ratio >= config.lower_shadow_ratio_threshold:
        score += 22
    if lower_shadow_pct >= config.lower_shadow_range_threshold:
        score += 18
    if close_location >= config.close_location_threshold:
        score += 16
    if config.enable_tunnel_fallback and trend_status == "up_or_repairing":
        score += 14
    if config.enable_tunnel_fallback and support_ok:
        score += 8
    score = max(0.0, min(100.0, score))

    risk_flags: list[str] = []
    if trend_status == "damaged":
        risk_flags.append("trend_structure_damaged")
    if pullback_pct > config.pullback_max_pct:
        risk_flags.append("pullback_too_deep")
    if close_location < config.close_location_threshold:
        risk_flags.append("weak_close_location")
    if config.enable_tunnel_fallback and not support_ok:
        risk_flags.append("far_from_nearest_support")

    wick_core_ok = (
        lower_shadow_pct >= config.lower_shadow_range_threshold
        and close_location >= config.close_location_threshold
        and pullback_pct >= config.pullback_min_pct * 0.5
    )
    if not wick_core_ok:
        stage = "NONE"
    elif score >= 74:
        stage = "STRONG_WATCH"
    elif score >= 55:
        stage = "WATCH"
    else:
        stage = "NONE"

    return {
        "ticker": ticker,
        "signal_name": "pullback_rejection",
        "signal_stage": stage,
        "signal_date": str(row.get("Date", "")),
        "score": round(score, 1),
        "score_breakdown": {
            "pullback_pct": round(pullback_pct, 4),
            "lower_shadow_ratio": round(lower_shadow_ratio, 4),
            "lower_shadow_pct_of_range": round(lower_shadow_pct, 4),
            "close_location": round(close_location, 4),
            "support_distance_pct": round(support_distance_pct, 4),
        },
        "close": round(close, 4),
        "pullback_pct": round(pullback_pct, 4),
        "pullback_days": int(pullback_days),
        "lower_shadow_ratio": round(lower_shadow_ratio, 4),
        "lower_shadow_pct_of_range": round(lower_shadow_pct, 4),
        "close_location": round(close_location, 4),
        "trend_status": trend_status,
        "tunnel_status": "near_support" if support_ok else "not_near_support",
        "nearest_support": round(float(nearest_support), 4),
        "support_distance_pct": round(support_distance_pct, 4),
        "waiting_breakout_price": round(high, 4),
        "invalidation_price": round(low, 4),
        "risk_flags": risk_flags,
        "human_readable_reason": (
            f"回调 {pullback_pct:.1%} 后出现长下影承接，收盘位于日内区间 {close_location:.1%}；"
            f"等待突破 ${high:.2f}，跌破 ${low:.2f} 视为失效。"
        ),
    }


def confirm_pullback_rejection(
    rows: Any,
    rejection: dict[str, Any],
    ticker: str = "",
    config: PullbackConfig = DEFAULT_PULLBACK_CONFIG,
) -> dict[str, Any]:
    if not config.enable_pullback_rejection_confirmation:
        return {"signal_name": "pullback_rejection_confirmation", "signal_stage": "NONE", "confirmation_score": 0.0, "risk_flags": ["disabled"]}
    frame = _frame(rows)
    if not rejection or rejection.get("signal_stage") not in {"WATCH", "STRONG_WATCH"} or frame.empty:
        return {"signal_name": "pullback_rejection_confirmation", "signal_stage": "NONE", "confirmation_score": 0.0, "risk_flags": []}

    high = _num(rejection.get("waiting_breakout_price"))
    low = _num(rejection.get("invalidation_price"))
    window = frame.tail(max(1, config.confirmation_window_days))
    latest = window.iloc[-1]
    risk_flags: list[str] = []
    stage = "WAITING_CONFIRMATION"
    ctype = "waiting"
    score = 35.0 if rejection.get("signal_stage") == "WATCH" else 45.0
    confirmation_row = latest

    for _, row in window.iterrows():
        open_, row_high, row_low, close = (_num(row["Open"]), _num(row["High"]), _num(row["Low"]), _num(row["Close"]))
        if row_low < low:
            stage, ctype, score, confirmation_row = "INVALIDATED", "break_rejection_low", 0.0, row
            risk_flags.append("broke_rejection_low")
            break
        intraday_break = row_high > high if config.require_intraday_breakout else close > high
        close_above = close > high
        bullish_close = close > open_
        if intraday_break and close_above and bullish_close:
            stage, ctype, score, confirmation_row = "STRONG_CONFIRMED", "close_above_rejection_high", 88.0, row
        elif intraday_break:
            stage, ctype, score, confirmation_row = "CONFIRMED", "intraday_breakout", 72.0, row
        elif bullish_close or ((close - row_low) / max(row_high - row_low, 1e-9) >= 0.62):
            stage, ctype, score, confirmation_row = "WEAK_CONFIRMED", "constructive_close", max(score, 56.0), row

    return {
        "ticker": ticker,
        "signal_name": "pullback_rejection_confirmation",
        "signal_stage": stage,
        "rejection_date": rejection.get("signal_date", ""),
        "confirmation_date": str(confirmation_row.get("Date", "")),
        "confirmation_type": ctype,
        "confirmation_score": round(score, 1),
        "rejection_high": round(high, 4),
        "rejection_low": round(low, 4),
        "current_close": round(_num(confirmation_row.get("Close")), 4),
        "waiting_breakout_price": round(high, 4),
        "invalidation_price": round(low, 4),
        "risk_flags": risk_flags,
        "human_readable_reason": f"回调承接后状态：{stage}；突破参考 ${high:.2f}，失效参考 ${low:.2f}。",
    }


def detect_pullback_setup(
    rows: Any,
    ticker: str = "",
    config: PullbackConfig = DEFAULT_PULLBACK_CONFIG,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Find a recent rejection candle, then confirm it with later candles.

    A live page should not require the rejection candle to be today's last bar:
    the common workflow is "yesterday showed lower-wick rejection, today either
    confirms or invalidates it". This helper searches the recent confirmation
    window without peeking beyond each candidate rejection bar.
    """
    frame = _frame(rows)
    empty_rejection = detect_pullback_rejection([], ticker, config)
    empty_confirmation = confirm_pullback_rejection([], empty_rejection, ticker, config)
    if len(frame) < 12:
        return empty_rejection, empty_confirmation

    candidates: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    max_lookback = max(1, int(config.confirmation_window_days)) + 1
    start = max(11, len(frame) - max_lookback)
    for idx in range(start, len(frame)):
        prefix = frame.iloc[: idx + 1].to_dict(orient="records")
        rejection = detect_pullback_rejection(prefix, ticker, config)
        if rejection.get("signal_stage") not in {"WATCH", "STRONG_WATCH"}:
            continue
        confirmation_rows = frame.iloc[idx : min(len(frame), idx + 1 + max(1, config.confirmation_window_days))].to_dict(orient="records")
        confirmation = confirm_pullback_rejection(confirmation_rows, rejection, ticker, config)
        candidates.append((idx, rejection, confirmation))

    if not candidates:
        return detect_pullback_rejection(frame.to_dict(orient="records"), ticker, config), empty_confirmation

    priority = {
        "STRONG_CONFIRMED": 6,
        "CONFIRMED": 5,
        "WEAK_CONFIRMED": 4,
        "WAITING_CONFIRMATION": 3,
        "INVALIDATED": 1,
        "NONE": 0,
    }
    candidates.sort(
        key=lambda item: (
            priority.get(str(item[2].get("signal_stage") or "NONE"), 0),
            float(item[1].get("score", 0) or 0),
            item[0],
        ),
        reverse=True,
    )
    return candidates[0][1], candidates[0][2]
