"""Causal, rule-based Elliott-style candidate structures from daily OHLCV.

This is a description of confirmed price pivots, not a forecast or a calibrated
probability. The most recent ``right_bars`` candles cannot be confirmed pivots.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd


LEFT_BARS = 3
RIGHT_BARS = 3
MIN_SWING_PCT = 0.025
ATR_MULTIPLE = 1.0
MIN_WAVE_BARS = 2


def _pivot_points(frame: pd.DataFrame) -> list[dict[str, Any]]:
    high = frame["High"].to_numpy(dtype=float)
    low = frame["Low"].to_numpy(dtype=float)
    close = frame["Close"].to_numpy(dtype=float)
    previous_close = pd.Series(close).shift(1).fillna(close[0]).to_numpy()
    true_range = pd.Series(
        [max(h - l, abs(h - pc), abs(l - pc)) for h, l, pc in zip(high, low, previous_close)]
    )
    atr = true_range.rolling(14, min_periods=5).mean().to_numpy()
    points: list[dict[str, Any]] = []
    for index in range(LEFT_BARS, len(frame) - RIGHT_BARS):
        is_high = high[index] > max(high[index - LEFT_BARS:index]) and high[index] >= max(
            high[index + 1:index + RIGHT_BARS + 1]
        )
        is_low = low[index] < min(low[index - LEFT_BARS:index]) and low[index] <= min(
            low[index + 1:index + RIGHT_BARS + 1]
        )
        if is_high == is_low:
            continue
        kind = "high" if is_high else "low"
        price = float(high[index] if is_high else low[index])
        point = {
            "kind": kind, "price": round(price, 4), "bar": index,
            "date": pd.Timestamp(frame.index[index]).date().isoformat(),
            "confirmed_on": pd.Timestamp(frame.index[index + RIGHT_BARS]).date().isoformat(),
        }
        if points and points[-1]["kind"] == kind:
            if (is_high and price > points[-1]["price"]) or (is_low and price < points[-1]["price"]):
                points[-1] = point
            continue
        threshold = max(MIN_SWING_PCT * close[index], ATR_MULTIPLE * (atr[index] if math.isfinite(atr[index]) else 0))
        if points and abs(price - points[-1]["price"]) < threshold:
            continue
        points.append(point)
    return points


def _impulse_rules(points: list[dict[str, Any]]) -> list[str] | None:
    if len(points) not in {5, 6}:
        return None
    direction = 1 if points[0]["kind"] == "low" else -1
    expected = ["low", "high"] if direction == 1 else ["high", "low"]
    if any(point["kind"] != expected[index % 2] for index, point in enumerate(points)):
        return None
    if any(right["bar"] - left["bar"] < MIN_WAVE_BARS for left, right in zip(points, points[1:])):
        return None
    value = [direction * point["price"] for point in points]
    if not (value[1] > value[0] and value[0] < value[2] < value[1]
            and value[3] > value[1] and value[3] > value[2]
            and value[1] < value[4] < value[3]):
        return None
    rules = ["wave2_above_start", "wave3_beyond_wave1", "wave4_no_overlap"]
    if len(points) == 6:
        wave1, wave3, wave5 = value[1] - value[0], value[3] - value[2], value[5] - value[4]
        if value[5] <= value[3] or wave3 < min(wave1, wave5):
            return None
        rules += ["wave5_beyond_wave3", "wave3_not_shortest"]
    return rules


def _abc_rules(points: list[dict[str, Any]]) -> list[str] | None:
    if len(points) != 9 or _impulse_rules(points[:6]) is None:
        return None
    if any(right["bar"] - left["bar"] < MIN_WAVE_BARS for left, right in zip(points[5:], points[6:])):
        return None
    direction = 1 if points[0]["kind"] == "low" else -1
    value = [direction * point["price"] for point in points]
    if not (value[6] < value[5] and value[6] < value[7] < value[5] and value[8] < value[6]):
        return None
    return ["b_below_impulse_end", "c_beyond_a"]


def detect_wave_structure(daily: pd.DataFrame) -> dict[str, Any]:
    """Classify only pivots confirmed by bars already present in ``daily``."""
    result: dict[str, Any] = {
        "status": "unverified", "stage": None, "direction": None,
        "points": [], "recent_pivots": [], "rule_checks": [],
        "pivot_confirmation_bars": RIGHT_BARS,
        "is_predictive_probability": False,
    }
    if not {"High", "Low", "Close"}.issubset(daily.columns):
        result["reason"] = "missing_ohlc"
        return result
    frame = daily.tail(180).copy()
    if len(frame) < 35:
        result["reason"] = "insufficient_history"
        return result
    frame[["High", "Low", "Close"]] = frame[["High", "Low", "Close"]].apply(pd.to_numeric, errors="coerce")
    values = frame[["High", "Low", "Close"]]
    if values.isna().any().any() or not all(math.isfinite(float(value)) for value in values.to_numpy().flat):
        result["reason"] = "invalid_ohlc"
        return result
    if (frame["High"] < frame["Low"]).any() or (frame["Close"] <= 0).any():
        result["reason"] = "invalid_ohlc"
        return result

    pivots = _pivot_points(frame)
    result["recent_pivots"] = [{key: point[key] for key in ("kind", "price", "date", "confirmed_on")}
                               for point in pivots[-5:]]
    if not pivots:
        result["status"] = "no_pattern"
        result["reason"] = "no_confirmed_pivots"
        return result

    candidate: tuple[str, str, list[dict[str, Any]], list[str]] | None = None
    if len(pivots) >= 9:
        segment = pivots[-9:]
        checks = _abc_rules(segment)
        if checks:
            candidate = ("abc_candidate", "abc_complete", segment, _impulse_rules(segment[:6]) + checks)
    if candidate is None:
        for trailing in range(min(2, len(pivots) - 6) + 1):
            segment = pivots[-6 - trailing:len(pivots) - trailing or None]
            checks = _impulse_rules(segment)
            if checks:
                candidate = ("impulse_candidate", "wave5_complete", segment, checks)
                break
    if candidate is None and len(pivots) >= 5:
        segment = pivots[-5:]
        checks = _impulse_rules(segment)
        if checks:
            candidate = ("impulse_forming", "wave5_pending", segment, checks)
    if candidate is None:
        result["status"] = "no_pattern"
        result["reason"] = "no_standard_structure"
        return result

    status, stage, points, checks = candidate
    last_age = len(frame) - 1 - points[-1]["bar"]
    labels = ["0", "1", "2", "3", "4", "5", "A", "B", "C"][:len(points)]
    result.update({
        "status": status if last_age <= 20 else "stale_pattern",
        "stage": stage,
        "direction": "up" if points[0]["kind"] == "low" else "down",
        "points": [{"label": label, **{key: point[key] for key in ("kind", "price", "date", "confirmed_on")}}
                   for label, point in zip(labels, points)],
        "rule_checks": checks,
        "last_pivot_age_bars": last_age,
        "reason": "confirmed_pivots_only" if last_age <= 20 else "latest_pattern_is_old",
    })
    return result
