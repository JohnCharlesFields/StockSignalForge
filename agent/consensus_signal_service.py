"""Bull/bear consensus proxy built from replayable OHLCV features.

The live intuition is: price direction follows the side with stronger marginal
consensus; volatility controls how far it can move.  This module deliberately
uses only trailing OHLCV features so it can be backtested without leaking future
data or using today's option snapshot as a historical IV proxy.
"""

from __future__ import annotations

import math
from typing import Any, Dict

import numpy as np
import pandas as pd

from distribution_risk_service import detect_distribution_risk


def _clip(value: Any, lo: float = 0.0, hi: float = 1.0, default: float = 0.0) -> float:
    try:
        x = float(value)
        if not math.isfinite(x):
            return default
        return max(lo, min(hi, x))
    except (TypeError, ValueError):
        return default


def consensus_feature_frame(frame: pd.DataFrame, benchmark: pd.DataFrame | None = None) -> pd.DataFrame:
    """Return a same-index feature frame with bull/bear/net/launch scores.

    Scores are in 0..1 except ``net_consensus`` (-1..1).  All rolling inputs use
    bars up to and including the current bar only.
    """
    if frame is None or frame.empty or "Close" not in frame:
        return pd.DataFrame()
    data = frame.copy()
    open_ = pd.to_numeric(data.get("Open"), errors="coerce")
    high = pd.to_numeric(data.get("High"), errors="coerce")
    low = pd.to_numeric(data.get("Low"), errors="coerce")
    close = pd.to_numeric(data.get("Close"), errors="coerce")
    volume = pd.to_numeric(data.get("Volume"), errors="coerce").fillna(0.0)
    out = pd.DataFrame(index=data.index)

    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    ma50 = close.rolling(50).mean()
    ma200 = close.rolling(200).mean()
    ret5 = close.pct_change(5)
    ret20 = close.pct_change(20)
    ret60 = close.pct_change(60)
    vol20 = volume.rolling(20).median().replace(0.0, np.nan)
    vol_ratio = volume / vol20
    rng = (high - low).replace(0.0, np.nan)
    close_location = ((close - low) / rng).clip(0.0, 1.0)
    upper_shadow = (high - pd.concat([open_, close], axis=1).max(axis=1)).clip(lower=0.0)
    lower_shadow = (pd.concat([open_, close], axis=1).min(axis=1) - low).clip(lower=0.0)
    upper_pct = (upper_shadow / rng).clip(0.0, 1.0)
    lower_pct = (lower_shadow / rng).clip(0.0, 1.0)
    day_ret = close.pct_change()
    intraday = close / open_ - 1.0

    # Accumulation / distribution proxies from executed daily volume.
    up_vol = volume.where(day_ret > 0, 0.0)
    dn_vol = volume.where(day_ret < 0, 0.0)
    udvr10 = up_vol.rolling(10).sum() / dn_vol.rolling(10).sum().replace(0.0, np.nan)
    sign = np.sign(day_ret).fillna(0.0)
    obv = (sign * volume).fillna(0.0).cumsum()
    obv_slope = (obv - obv.shift(20)) / (20.0 * volume.rolling(20).mean().replace(0.0, np.nan))
    money_flow_multiplier = ((close - low) - (high - close)) / rng
    cmf20 = (money_flow_multiplier * volume).rolling(20).sum() / volume.rolling(20).sum().replace(0.0, np.nan)

    prev20_high = high.shift(1).rolling(20).max()
    breakout = close > prev20_high
    failed_breakout = (high > prev20_high * 1.003) & (close < prev20_high)

    log_ret = np.log(close / close.shift(1))
    hv20 = log_ret.rolling(20).std() * math.sqrt(252.0)
    hv_rise = (hv20 / hv20.shift(10) - 1.0).replace([np.inf, -np.inf], np.nan)
    atr_pct = (pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1).rolling(14).mean() / close).replace([np.inf, -np.inf], np.nan)

    rs20 = pd.Series(0.0, index=data.index)
    if benchmark is not None and not benchmark.empty and "Close" in benchmark:
        bclose = pd.to_numeric(benchmark.get("Close"), errors="coerce")
        common = close.index.intersection(bclose.index)
        br20 = bclose.loc[common].pct_change(20)
        rs20.loc[common] = (ret20.loc[common] - br20).fillna(0.0)

    trend_bull = (
        (close > ma20).astype(float) * 0.25
        + (close > ma50).astype(float) * 0.25
        + (ma20 > ma20.shift(10)).astype(float) * 0.20
        + (ma50 > ma50.shift(20)).astype(float) * 0.15
        + (close > ma200).astype(float) * 0.15
    )
    trend_bear = (
        (close < ma20).astype(float) * 0.25
        + (close < ma50).astype(float) * 0.25
        + (ma20 < ma20.shift(10)).astype(float) * 0.20
        + (ma50 < ma50.shift(20)).astype(float) * 0.15
        + (close < ma200).astype(float) * 0.15
    )
    price_bull = (
        close_location.fillna(0.5) * 0.30
        + lower_pct.fillna(0.0).clip(0.0, 0.6) / 0.6 * 0.18
        + breakout.astype(float) * 0.22
        + ((ret5.fillna(0.0) + 0.04) / 0.08).clip(0.0, 1.0) * 0.15
        + ((rs20.fillna(0.0) + 0.05) / 0.10).clip(0.0, 1.0) * 0.15
    )
    price_bear = (
        (1.0 - close_location.fillna(0.5)) * 0.28
        + upper_pct.fillna(0.0).clip(0.0, 0.6) / 0.6 * 0.22
        + failed_breakout.astype(float) * 0.22
        + ((-ret5.fillna(0.0) + 0.04) / 0.08).clip(0.0, 1.0) * 0.14
        + ((-rs20.fillna(0.0) + 0.05) / 0.10).clip(0.0, 1.0) * 0.14
    )
    volume_bull = (
        ((udvr10.fillna(1.0) - 0.8) / 1.0).clip(0.0, 1.0) * 0.35
        + ((obv_slope.fillna(0.0) + 0.2) / 0.7).clip(0.0, 1.0) * 0.30
        + ((cmf20.fillna(0.0) + 0.1) / 0.3).clip(0.0, 1.0) * 0.25
        + ((vol_ratio.fillna(1.0) - 1.0) / 2.0).clip(0.0, 1.0) * breakout.astype(float) * 0.10
    )
    volume_bear = (
        ((1.2 - udvr10.fillna(1.0)) / 1.0).clip(0.0, 1.0) * 0.30
        + ((-obv_slope.fillna(0.0) + 0.2) / 0.7).clip(0.0, 1.0) * 0.25
        + ((-cmf20.fillna(0.0) + 0.1) / 0.3).clip(0.0, 1.0) * 0.25
        + ((vol_ratio.fillna(1.0) - 1.0) / 2.0).clip(0.0, 1.0) * (failed_breakout | (close_location < 0.45)).astype(float) * 0.20
    )

    # Row-wise distribution risk, only for recent lookback; acceptable for replay
    # because each call sees history up to that bar.
    dist_score = pd.Series(0.0, index=data.index)
    for i in range(25, len(data)):
        risk = detect_distribution_risk(data.iloc[: i + 1])
        if risk.get("triggered"):
            dist_score.iloc[i] = 1.0 if risk.get("level") == "high" else 0.65

    bull = (0.34 * trend_bull + 0.36 * price_bull + 0.30 * volume_bull).clip(0.0, 1.0)
    bear = (0.30 * trend_bear + 0.32 * price_bear + 0.28 * volume_bear + 0.10 * dist_score).clip(0.0, 1.0)
    net = (bull - bear).clip(-1.0, 1.0)
    # Historical IV proxy: realized vol expansion + ATR percent. The live layer
    # may replace/add actual IV when a historical IV source is available.
    volatility_impulse = (
        ((hv_rise.fillna(0.0) + 0.15) / 0.5).clip(0.0, 1.0) * 0.55
        + ((atr_pct.fillna(0.0) - 0.015) / 0.08).clip(0.0, 1.0) * 0.45
    )
    launch = ((net.clip(lower=0.0) * 0.72) + (net.clip(lower=0.0) * volatility_impulse * 0.28)).clip(0.0, 1.0)

    out["bull_consensus"] = bull
    out["bear_consensus"] = bear
    out["net_consensus"] = net
    out["consensus_direction_score"] = ((net + 1.0) / 2.0).clip(0.0, 1.0)
    out["volatility_impulse"] = volatility_impulse
    out["consensus_launch_score"] = launch
    out["hv20"] = hv20
    out["hv_rise"] = hv_rise
    out["atr_pct"] = atr_pct
    out["volume_ratio"] = vol_ratio
    out["close_location"] = close_location
    out["upper_shadow_pct"] = upper_pct
    out["cmf20"] = cmf20
    out["udvr10"] = udvr10
    out["rs20"] = rs20
    return out.replace([np.inf, -np.inf], np.nan)


def latest_consensus_snapshot(frame: pd.DataFrame, benchmark: pd.DataFrame | None = None) -> Dict[str, Any]:
    features = consensus_feature_frame(frame, benchmark)
    if features.empty:
        return {"available": False}
    row = features.dropna(subset=["consensus_direction_score"]).tail(1)
    if row.empty:
        return {"available": False}
    item = row.iloc[-1]
    bull = _clip(item.get("bull_consensus"))
    bear = _clip(item.get("bear_consensus"))
    net = _clip(item.get("net_consensus"), -1.0, 1.0)
    direction = "bullish" if net > 0.12 else ("bearish" if net < -0.12 else "neutral")
    return {
        "available": True,
        "bull_consensus_score": round(bull, 4),
        "bear_consensus_score": round(bear, 4),
        "net_consensus_score": round(net, 4),
        "direction": direction,
        "consensus_direction_score": round(float(item.get("consensus_direction_score")), 4),
        "volatility_impulse": round(_clip(item.get("volatility_impulse")), 4),
        "launch_potential_score": round(_clip(item.get("consensus_launch_score")), 4),
        "hv20": round(float(item.get("hv20")), 4) if pd.notna(item.get("hv20")) else None,
        "hv_rise": round(float(item.get("hv_rise")), 4) if pd.notna(item.get("hv_rise")) else None,
        "atr_pct": round(float(item.get("atr_pct")), 4) if pd.notna(item.get("atr_pct")) else None,
        "method": "OHLCV bull/bear consensus proxy + historical volatility impulse",
    }
