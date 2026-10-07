"""Shared, auditable technical metrics for cash-equity research signals.

The daily-tunnel score is a research ranking feature, not a trading rule.  It
models where the tape sits inside an up/down cycle instead of requiring every
good candidate to stay above MA5 every day.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

import pandas as pd


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _round(value: Any, digits: int = 4) -> float:
    return round(_finite(value), digits)


def _runs(flags: Iterable[bool]) -> list[int]:
    lengths: list[int] = []
    current = 0
    for flag in flags:
        if flag:
            current += 1
        elif current:
            lengths.append(current)
            current = 0
    if current:
        lengths.append(current)
    return lengths


def _tail_run(flags: Iterable[bool]) -> int:
    streak = 0
    for flag in reversed(list(flags)):
        if not flag:
            break
        streak += 1
    return streak


def _empty_daily_tunnel(note: str) -> dict[str, Any]:
    return {
        "score": 0.0,
        "label": "数据不足",
        "current_zone": "无法判断",
        "cycle_state": "insufficient_data",
        "sample_days": 0,
        "ma5": None,
        "ma10": None,
        "ma15": None,
        "ma20": None,
        "above_ma5_rate": 0.0,
        "above_ma20_rate": 0.0,
        "bullish_alignment_rate": 0.0,
        "current_above_ma5_streak": 0,
        "max_ma5_break_pct": 0.0,
        "current_up_cycle_days": 0,
        "prior_down_cycle_days": 0,
        "longest_up_cycle_days": 0,
        "average_up_cycle_days": 0.0,
        "up_cycle_power": 0.0,
        "turning_point_score": 0.0,
        "components": {},
        "note": note,
    }


def compute_daily_tunnel_score(frame: pd.DataFrame, lookback: int = 20) -> dict[str, Any]:
    """Score MA-cycle quality and down-to-up turning points.

    Old logic over-penalized candidates during normal pullbacks.  The updated
    score treats MA5 as a fast guide during an up-cycle, then separately asks:
    did a recent down-cycle already start curling back into an up-cycle?  Long
    historical up-cycles are rewarded because they imply persistent demand.
    """
    if frame is None or frame.empty:
        return _empty_daily_tunnel("缺少日线行情，无法计算日隧道协商。")
    close_column = "Close" if "Close" in frame else ("close" if "close" in frame else None)
    if not close_column:
        return _empty_daily_tunnel("日线行情缺少收盘价，无法计算日隧道协商。")
    close = pd.to_numeric(frame[close_column], errors="coerce").dropna()
    if len(close) < max(25, lookback + 5):
        return _empty_daily_tunnel("有效日线不足 25 个交易日，暂不计算上下行周期。")

    tunnel = pd.DataFrame({"close": close})
    for window in (5, 10, 15, 20):
        tunnel[f"ma{window}"] = close.rolling(window).mean()
    tunnel["ma5_slope"] = tunnel["ma5"].diff()
    tunnel["ret_5d"] = tunnel["close"].pct_change(5)
    tunnel = tunnel.dropna()
    if len(tunnel) < max(8, lookback // 3):
        return _empty_daily_tunnel("均线样本不足，暂不计算上下行周期。")

    full = tunnel.tail(max(80, lookback * 4))
    sample = tunnel.tail(lookback)
    latest = sample.iloc[-1]

    up_phase = (full["close"] >= full["ma5"]) & (full["ma5"] >= full["ma10"]) & (full["ma5_slope"] >= 0)
    constructive_pullback = (
        (full["close"] < full["ma5"])
        & (full["close"] >= full["ma20"])
        & (full["ma10"] >= full["ma20"])
        & (full["ret_5d"] > -0.08)
    )
    down_phase = (
        ((full["close"] < full["ma10"]) & (full["ma5_slope"] < 0))
        | ((full["ma5"] < full["ma10"]) & (full["ret_5d"] < 0))
    )
    constructive_up = up_phase | constructive_pullback

    up_lengths = _runs(up_phase.tolist())
    current_up_cycle_days = _tail_run(constructive_up.tolist())
    recent_down_days = int(down_phase.tail(12).sum())
    prior_down_cycle_days = 0
    if current_up_cycle_days:
        before_current = down_phase.iloc[: max(0, len(down_phase) - current_up_cycle_days)]
        prior_down_cycle_days = _tail_run(before_current.tolist())
    elif len(down_phase) > 1:
        prior_down_cycle_days = _tail_run(down_phase.iloc[:-1].tolist())

    longest_up_cycle = max(up_lengths or [0])
    average_up_cycle = sum(up_lengths) / len(up_lengths) if up_lengths else 0.0
    up_cycle_power = min(1.0, (0.65 * min(longest_up_cycle, 30) / 30.0) + (0.35 * min(average_up_cycle, 18) / 18.0))

    above_ma5 = sample["close"] >= sample["ma5"]
    above_ma20 = sample["close"] >= sample["ma20"]
    bullish_alignment = (
        (sample["ma5"] >= sample["ma10"])
        & (sample["ma10"] >= sample["ma15"])
        & (sample["ma15"] >= sample["ma20"])
    )
    above_ma5_rate = _finite(above_ma5.mean())
    above_ma20_rate = _finite(above_ma20.mean())
    alignment_rate = _finite(bullish_alignment.mean())
    ma5_break = ((sample["ma5"] - sample["close"]) / sample["ma5"]).clip(lower=0.0)
    max_break = _finite(ma5_break.max())
    rejection_depth = max(0.0, 1.0 - max_break / 0.10)
    current_above_ma5_streak = _tail_run(above_ma5.tolist())

    latest_up = bool(up_phase.iloc[-1])
    latest_pullback = bool(constructive_pullback.iloc[-1])
    latest_down = bool(down_phase.iloc[-1])
    recovered_ma10 = bool(latest["close"] >= latest["ma10"])
    recovered_ma5 = bool(latest["close"] >= latest["ma5"])
    ma5_turning_up = bool(latest["ma5_slope"] > 0)
    turning_point_score = 0.0
    if recent_down_days:
        turning_point_score += min(0.35, recent_down_days / 12.0 * 0.35)
    if recovered_ma10:
        turning_point_score += 0.25
    if ma5_turning_up:
        turning_point_score += 0.25
    if recovered_ma5:
        turning_point_score += 0.15
    turning_point_score = min(1.0, turning_point_score)

    if turning_point_score >= 0.65 and recent_down_days >= 2:
        cycle_state = "down_to_up_turn"
        current_zone = "下行转上行拐点：重新站回短均线"
        zone_score = 0.9
    elif latest_up and current_above_ma5_streak >= 3:
        cycle_state = "up_cycle"
        current_zone = "上行周期：沿 MA5 推进"
        zone_score = 1.0
    elif latest_pullback:
        cycle_state = "constructive_pullback"
        current_zone = "上行回撤：仍在 MA20 上方修复"
        zone_score = 0.68
    elif latest_down:
        cycle_state = "down_cycle"
        current_zone = "下行周期：等待重新站回 MA10/MA5"
        zone_score = 0.25
    else:
        cycle_state = "neutral"
        current_zone = "震荡区间：方向未确认"
        zone_score = 0.45

    components = {
        "cycle_turn": round(turning_point_score * 24.0, 1),
        "up_cycle_power": round(up_cycle_power * 20.0, 1),
        "current_zone": round(zone_score * 18.0, 1),
        "above_ma20": round(above_ma20_rate * 12.0, 1),
        "bullish_alignment": round(alignment_rate * 10.0, 1),
        "rejection_depth": round(rejection_depth * 8.0, 1),
        "above_ma5": round(above_ma5_rate * 8.0, 1),
    }
    score = round(min(100.0, sum(components.values())), 1)

    if cycle_state == "down_to_up_turn" and score >= 55:
        label = "拐点观察"
    elif score >= 85:
        label = "强上行周期"
    elif score >= 70:
        label = "上行偏强"
    elif score >= 55:
        label = "修复观察"
    elif score >= 40:
        label = "弱修复"
    else:
        label = "下行未确认"

    return {
        "score": score,
        "label": label,
        "current_zone": current_zone,
        "cycle_state": cycle_state,
        "sample_days": len(sample),
        "ma5": round(_finite(latest["ma5"]), 2),
        "ma10": round(_finite(latest["ma10"]), 2),
        "ma15": round(_finite(latest["ma15"]), 2),
        "ma20": round(_finite(latest["ma20"]), 2),
        "above_ma5_rate": round(above_ma5_rate, 4),
        "above_ma20_rate": round(above_ma20_rate, 4),
        "bullish_alignment_rate": round(alignment_rate, 4),
        "current_above_ma5_streak": current_above_ma5_streak,
        "max_ma5_break_pct": round(max_break, 4),
        "current_up_cycle_days": current_up_cycle_days,
        "prior_down_cycle_days": prior_down_cycle_days,
        "longest_up_cycle_days": int(longest_up_cycle),
        "average_up_cycle_days": round(average_up_cycle, 1),
        "up_cycle_power": round(up_cycle_power, 4),
        "turning_point_score": round(turning_point_score, 4),
        "components": components,
        "note": (
            "日隧道协商已改为上下行周期识别：奖励历史长上行周期，重点寻找下行转上行拐点；"
            "跌破 MA5 不再自动视为失败，需结合 MA10/MA20、MA5 斜率和最近下行段判断。"
        ),
    }


def calculate_ma(frame: pd.DataFrame, window: int, column: str = "Close") -> pd.Series:
    """Calculate simple moving average for the specified column.

    Args:
        frame: DataFrame with OHLCV data.
        window: Rolling window size in trading days.
        column: Column name to calculate MA on (default: "Close").

    Returns:
        Series with MA values (NaN for initial periods without enough data).
    """
    if frame is None or frame.empty:
        return pd.Series(dtype=float)
    col = column if column in frame else ("close" if "close" in frame else None)
    if not col:
        return pd.Series(dtype=float)
    values = pd.to_numeric(frame[col], errors="coerce")
    return values.rolling(window=window, min_periods=window).mean()


def calculate_atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculate Average True Range (ATR).

    ATR measures volatility by decomposing the entire range of an asset price
    for each period.  Uses the standard Wilder smoothing method.

    Args:
        frame: DataFrame with High, Low, Close columns.
        period: ATR period (default: 14).

    Returns:
        Series with ATR values.
    """
    if frame is None or frame.empty:
        return pd.Series(dtype=float)

    high_col = "High" if "High" in frame else ("high" if "high" in frame else None)
    low_col = "Low" if "Low" in frame else ("low" if "low" in frame else None)
    close_col = "Close" if "Close" in frame else ("close" if "close" in frame else None)

    if not all([high_col, low_col, close_col]):
        return pd.Series(dtype=float)

    high = pd.to_numeric(frame[high_col], errors="coerce")
    low = pd.to_numeric(frame[low_col], errors="coerce")
    close = pd.to_numeric(frame[close_col], errors="coerce")

    if len(close) < period + 1:
        return pd.Series(dtype=float)

    # True Range calculation
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # Wilder smoothing (equivalent to EMA with alpha = 1/period)
    atr = true_range.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    return atr


def calculate_volume_ratio(frame: pd.DataFrame, window: int = 20) -> float:
    """Calculate volume ratio: latest volume / median of last N days.

    Uses median instead of mean to be more robust against outlier volume spikes.

    Args:
        frame: DataFrame with Volume column.
        window: Lookback window for median calculation (default: 20).

    Returns:
        Volume ratio as float.  Returns 0.0 if data is insufficient.
    """
    if frame is None or frame.empty:
        return 0.0

    vol_col = "Volume" if "Volume" in frame else ("volume" if "volume" in frame else None)
    if not vol_col:
        return 0.0

    volume = pd.to_numeric(frame[vol_col], errors="coerce").dropna()
    if len(volume) < window:
        return 0.0

    latest_volume = float(volume.iloc[-1])
    median_volume = float(volume.tail(window).median())

    if median_volume <= 0:
        return 0.0

    return round(latest_volume / median_volume, 4)


def calculate_avg_dollar_volume(frame: pd.DataFrame, window: int = 20) -> float:
    """Calculate average dollar volume over the last N trading days.

    Dollar volume = Close * Volume.  This is a liquidity proxy.

    Args:
        frame: DataFrame with Close and Volume columns.
        window: Lookback window (default: 20).

    Returns:
        Average dollar volume as float.  Returns 0.0 if data is insufficient.
    """
    if frame is None or frame.empty:
        return 0.0

    close_col = "Close" if "Close" in frame else ("close" if "close" in frame else None)
    vol_col = "Volume" if "Volume" in frame else ("volume" if "volume" in frame else None)

    if not close_col or not vol_col:
        return 0.0

    close = pd.to_numeric(frame[close_col], errors="coerce")
    volume = pd.to_numeric(frame[vol_col], errors="coerce").fillna(0)

    dollar_volume = close * volume
    if len(dollar_volume) < window:
        return 0.0

    return round(float(dollar_volume.tail(window).mean()), 2)


def calculate_relative_momentum(
    stock_frame: pd.DataFrame,
    benchmark_frame: pd.DataFrame,
    window: int = 10,
) -> dict[str, Any]:
    """Calculate relative momentum: stock return minus benchmark return.

    This measures whether a stock is outperforming or underperforming the
    benchmark over the specified window.  A positive value means the stock
    is stronger than the benchmark.

    Args:
        stock_frame: DataFrame with stock OHLCV data.
        benchmark_frame: DataFrame with benchmark OHLCV data.
        window: Lookback window in trading days (default: 10).

    Returns:
        Dict with stock_return, benchmark_return, relative_return, and
        supporting metrics.  Returns None values if data is insufficient.
    """
    result: dict[str, Any] = {
        "stock_return": None,
        "benchmark_return": None,
        "relative_return": None,
        "stock_close": None,
        "stock_close_ago": None,
        "benchmark_close": None,
        "benchmark_close_ago": None,
        "window": window,
    }

    if stock_frame is None or stock_frame.empty or benchmark_frame is None or benchmark_frame.empty:
        return result

    stock_close_col = "Close" if "Close" in stock_frame else ("close" if "close" in stock_frame else None)
    bench_close_col = "Close" if "Close" in benchmark_frame else ("close" if "close" in benchmark_frame else None)

    if not stock_close_col or not bench_close_col:
        return result

    stock_close = pd.to_numeric(stock_frame[stock_close_col], errors="coerce").dropna()
    bench_close = pd.to_numeric(benchmark_frame[bench_close_col], errors="coerce").dropna()

    if len(stock_close) < window + 1 or len(bench_close) < window + 1:
        return result

    stock_latest = float(stock_close.iloc[-1])
    stock_ago = float(stock_close.iloc[-(window + 1)])
    bench_latest = float(bench_close.iloc[-1])
    bench_ago = float(bench_close.iloc[-(window + 1)])

    if stock_ago <= 0 or bench_ago <= 0:
        return result

    stock_return = stock_latest / stock_ago - 1.0
    bench_return = bench_latest / bench_ago - 1.0

    result.update({
        "stock_return": round(stock_return, 6),
        "benchmark_return": round(bench_return, 6),
        "relative_return": round(stock_return - bench_return, 6),
        "stock_close": round(stock_latest, 2),
        "stock_close_ago": round(stock_ago, 2),
        "benchmark_close": round(bench_latest, 2),
        "benchmark_close_ago": round(bench_ago, 2),
    })
    return result


def check_ma20_exit(frame: pd.DataFrame) -> dict[str, Any]:
    """Check MA20 stop-loss condition.

    This function determines whether the latest close is below MA20,
    which is used as a stop-loss trigger for the pre-earnings strategy.

    IMPORTANT: MA20 is a trigger condition, not an execution price.
    Actual exit execution happens at the next trading day's open.

    Args:
        frame: DataFrame with Close column.

    Returns:
        Dict with stop_triggered, current_price, ma20, distance_pct, and note.
    """
    result: dict[str, Any] = {
        "stop_triggered": False,
        "current_price": None,
        "ma20": None,
        "distance_pct": None,
        "note": "数据不足",
    }

    if frame is None or frame.empty:
        return result

    close_col = "Close" if "Close" in frame else ("close" if "close" in frame else None)
    if not close_col:
        return result

    close = pd.to_numeric(frame[close_col], errors="coerce").dropna()
    if len(close) < 20:
        return result

    ma20 = close.rolling(20).mean()
    current_price = float(close.iloc[-1])
    current_ma20 = float(ma20.iloc[-1])

    if pd.isna(current_ma20) or current_ma20 <= 0:
        return result

    distance_pct = (current_price / current_ma20 - 1.0) * 100.0
    stop_triggered = current_price < current_ma20

    result.update({
        "stop_triggered": stop_triggered,
        "current_price": round(current_price, 2),
        "ma20": round(current_ma20, 2),
        "distance_pct": round(distance_pct, 2),
        "note": "收盘价跌破MA20，触发止损条件" if stop_triggered else "收盘价在MA20上方，未触发止损",
    })
    return result


def calculate_technical_metrics(frame: pd.DataFrame, benchmark_frame: pd.DataFrame | None = None) -> dict[str, Any]:
    """Calculate all technical metrics for pre-earnings signal.

    This is a convenience function that calculates MA10, MA20, ATR14,
    volume ratio, average dollar volume, and relative momentum in one call.

    Args:
        frame: DataFrame with stock OHLCV data.
        benchmark_frame: Optional DataFrame with benchmark OHLCV data.

    Returns:
        Dict with all technical metrics.
    """
    result: dict[str, Any] = {
        "ma10": None,
        "ma20": None,
        "atr_14": None,
        "volume_ratio_20d": 0.0,
        "avg_dollar_volume_20d": 0.0,
        "relative_momentum": None,
        "ma20_exit_check": None,
    }

    if frame is None or frame.empty:
        return result

    # Calculate MAs
    ma10 = calculate_ma(frame, 10)
    ma20 = calculate_ma(frame, 20)

    if not ma10.empty and not pd.isna(ma10.iloc[-1]):
        result["ma10"] = round(float(ma10.iloc[-1]), 2)
    if not ma20.empty and not pd.isna(ma20.iloc[-1]):
        result["ma20"] = round(float(ma20.iloc[-1]), 2)

    # Calculate ATR
    atr = calculate_atr(frame, 14)
    if not atr.empty and not pd.isna(atr.iloc[-1]):
        result["atr_14"] = round(float(atr.iloc[-1]), 2)

    # Calculate volume ratio
    result["volume_ratio_20d"] = calculate_volume_ratio(frame, 20)

    # Calculate average dollar volume
    result["avg_dollar_volume_20d"] = calculate_avg_dollar_volume(frame, 20)

    # Calculate relative momentum if benchmark is provided
    if benchmark_frame is not None and not benchmark_frame.empty:
        result["relative_momentum"] = calculate_relative_momentum(frame, benchmark_frame, 10)

    # Calculate MA20 exit check
    result["ma20_exit_check"] = check_ma20_exit(frame)

    return result
