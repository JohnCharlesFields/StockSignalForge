"""Volume-price distribution risk detection.

Flags the pattern the user described: high level + abnormal volume + weak close
or failed breakout / long upper wick. It is a warning label, not a sell signal.
"""

from __future__ import annotations

import math
from typing import Any, Dict

import pandas as pd


def detect_distribution_risk(frame: pd.DataFrame, *, lookback: int = 60) -> Dict[str, Any]:
    """Detect high-volume stalling / upper-wick distribution risk.

    The warning is deliberately conjunctive: it requires a high-price context and
    abnormal volume, then looks for weak close / failed breakout / long upper
    wick / multi-day churn. This avoids flagging healthy high-volume breakouts.
    """
    if frame is None or frame.empty or len(frame) < 25:
        return {"available": False, "triggered": False, "level": "none", "reasons": ["insufficient_history"]}
    try:
        recent = frame.tail(max(lookback, 25)).copy()
        open_ = pd.to_numeric(recent.get("Open"), errors="coerce")
        high = pd.to_numeric(recent.get("High"), errors="coerce")
        low = pd.to_numeric(recent.get("Low"), errors="coerce")
        close = pd.to_numeric(recent.get("Close"), errors="coerce")
        volume = pd.to_numeric(recent.get("Volume"), errors="coerce")
        data = pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume}).dropna()
        data = data[(data["Open"] > 0) & (data["High"] > 0) & (data["Low"] > 0) & (data["Close"] > 0) & (data["Volume"] > 0)]
    except Exception:
        return {"available": False, "triggered": False, "level": "none", "reasons": ["bad_ohlcv"]}
    if len(data) < 25:
        return {"available": False, "triggered": False, "level": "none", "reasons": ["insufficient_clean_history"]}

    latest = data.iloc[-1]
    prev_close = float(data["Close"].iloc[-2]) if len(data) >= 2 else float(latest["Close"])
    close_v = float(latest["Close"])
    high_v = float(latest["High"])
    low_v = float(latest["Low"])
    open_v = float(latest["Open"])
    vol_v = float(latest["Volume"])
    vol20 = float(data["Volume"].tail(21).iloc[:-1].median()) if len(data) >= 21 else float(data["Volume"].iloc[:-1].median())
    if not math.isfinite(vol20) or vol20 <= 0:
        vol20 = float(data["Volume"].iloc[:-1].mean())
    volume_ratio = vol_v / vol20 if vol20 > 0 else None
    rolling_high_20_prev = float(data["High"].iloc[:-1].tail(20).max())
    rolling_high_60 = float(data["High"].tail(60).max())
    range_v = max(high_v - low_v, 1e-9)
    upper_shadow = max(0.0, high_v - max(open_v, close_v))
    lower_shadow = max(0.0, min(open_v, close_v) - low_v)
    close_location = (close_v - low_v) / range_v
    upper_shadow_pct = upper_shadow / range_v
    day_return = close_v / prev_close - 1.0 if prev_close > 0 else 0.0
    intraday_return = close_v / open_v - 1.0 if open_v > 0 else 0.0
    near_high = close_v >= rolling_high_60 * 0.92 or high_v >= rolling_high_60 * 0.98
    failed_breakout = high_v > rolling_high_20_prev * 1.003 and close_v < rolling_high_20_prev
    weak_close = close_location <= 0.45 or intraday_return <= -0.003
    long_upper_wick = upper_shadow_pct >= 0.35 and upper_shadow > lower_shadow
    volume_spike = bool(volume_ratio is not None and volume_ratio >= 1.8)
    stalling = day_return <= 0.012

    churn = False
    churn_count = 0
    if len(data) >= 23:
        tail = data.tail(5).copy()
        tail_range = max(float(tail["High"].max() - tail["Low"].min()), 1e-9)
        tail_close_range = abs(float(tail["Close"].iloc[-1] / tail["Close"].iloc[0] - 1.0)) if float(tail["Close"].iloc[0]) > 0 else 1.0
        median20 = data["Volume"].tail(25).iloc[:-5].median()
        if median20 and math.isfinite(float(median20)) and float(median20) > 0:
            churn_count = int((tail["Volume"] / float(median20) >= 1.5).sum())
            churn = bool(churn_count >= 3 and tail_close_range <= 0.035 and tail_range / close_v <= 0.12 and close_v >= rolling_high_60 * 0.9)

    signals = {
        "near_high": near_high,
        "volume_spike": volume_spike,
        "stalling": stalling,
        "weak_close": weak_close,
        "failed_breakout": failed_breakout,
        "long_upper_wick": long_upper_wick,
        "high_volume_churn": churn,
    }
    score = 0.0
    weights = {
        "near_high": 1.0,
        "volume_spike": 1.2,
        "stalling": 0.7,
        "weak_close": 0.9,
        "failed_breakout": 1.0,
        "long_upper_wick": 1.0,
        "high_volume_churn": 0.8,
    }
    for key, active in signals.items():
        if active:
            score += weights[key]
    triggered = bool(near_high and volume_spike and (weak_close or failed_breakout or long_upper_wick or churn) and stalling)
    level = "high" if triggered and score >= 4.4 else ("medium" if triggered else "none")
    reasons = []
    if volume_spike:
        reasons.append(f"成交量约为20日中位量 {volume_ratio:.1f}x")
    if near_high:
        reasons.append("处于阶段高位附近")
    if failed_breakout:
        reasons.append("盘中突破近期高点但收盘未站稳")
    if long_upper_wick:
        reasons.append(f"长上影占日内振幅 {upper_shadow_pct:.0%}")
    if weak_close:
        reasons.append(f"收盘位于日内区间 {close_location:.0%}，收盘偏弱")
    if churn:
        reasons.append(f"近5日有 {churn_count} 日放量但价格横盘")
    label = "高位放量滞涨" if level == "medium" else ("高位派发风险" if level == "high" else "无派发警示")
    return {
        "available": True,
        "triggered": triggered,
        "level": level,
        "label": label,
        "score": round(score, 2),
        "volume_ratio": round(volume_ratio, 2) if volume_ratio is not None and math.isfinite(volume_ratio) else None,
        "day_return": round(day_return, 4),
        "intraday_return": round(intraday_return, 4),
        "close_location": round(close_location, 4),
        "upper_shadow_pct": round(upper_shadow_pct, 4),
        "failed_breakout": failed_breakout,
        "high_volume_churn": churn,
        "reasons": reasons,
        "method": "near-stage-high + abnormal volume + weak close/failed breakout/upper wick/churn",
    }
