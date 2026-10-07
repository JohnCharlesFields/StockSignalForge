"""Overnight alpha validation for the research cockpit.

The cockpit strategy is defined as a research hypothesis:
buy near the signal-date close and evaluate the next regular-session open.
This module validates whether that overnight move beats the symbol's relevant
benchmark beta, rather than merely rising with the market.
"""

from __future__ import annotations

import hashlib
import math
import os
from statistics import mean, median, stdev
from typing import Any, Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

from market_data_service import get_daily_history, is_daily_data_current
from app_database import overnight_alpha_get, overnight_alpha_set


BENCHMARK_MAP = {
    "spx": "SPY",
    "w5000": "SPY",
    "rua": "SPY",
    "djia": "DIA",
    "ixic": "QQQ",
    "ndx": "QQQ",
    "sox": "SOXX",
    "rut": "IWM",
    "sml": "IWM",
    "mid": "MDY",
    "aiq": "AIQ",
    "komp": "KOMP",
    "pave": "PAVE",
    "ura": "URA",
    "ppa": "PPA",
    "dtcr": "DTCR",
    "spmo": "SPMO",
    "watchlist": "QQQ",
}

BENCHMARK_PRIORITY = [
    "sox",
    "aiq",
    "komp",
    "pave",
    "ura",
    "ppa",
    "dtcr",
    "spmo",
    "watchlist",
    "ndx",
    "ixic",
    "rut",
    "sml",
    "mid",
    "djia",
    "spx",
    "rua",
    "w5000",
]
_ALPHA_CACHE_TTL_SECONDS = int(os.environ.get("OVERNIGHT_ALPHA_CACHE_TTL_SECONDS", str(12 * 3600)))


def benchmark_for_universes(source_universes: Iterable[str] | None) -> tuple[str, str]:
    ids = [str(item or "").strip().lower() for item in (source_universes or []) if str(item or "").strip()]
    for universe in BENCHMARK_PRIORITY:
        if universe in ids and universe in BENCHMARK_MAP:
            return BENCHMARK_MAP[universe], universe
    if ids:
        first = ids[0]
        return BENCHMARK_MAP.get(first, "SPY"), first
    return "SPY", "default"


def _aligned_frames(symbol: str, benchmark: str, period: str) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, str]]:
    stock, stock_source = get_daily_history(symbol, period=period)
    bench, bench_source = get_daily_history(benchmark, period=period)
    if stock.empty or bench.empty:
        return pd.DataFrame(), pd.DataFrame(), {"stock": stock_source, "benchmark": bench_source}
    common = stock.index.intersection(bench.index).sort_values()
    return stock.loc[common].copy(), bench.loc[common].copy(), {"stock": stock_source, "benchmark": bench_source}


def _signal_profile(row: Dict[str, Any]) -> str:
    return "|".join([
        str(row.get("pullback_rejection_status") or "").upper(),
        str(row.get("pullback_confirmation_status") or "").upper(),
    ])


def _cache_key(symbol: str, benchmark: str, benchmark_source: str, profile: str, period: str, min_edge: float) -> str:
    raw = f"{symbol}|{benchmark}|{benchmark_source}|{profile}|{period}|{min_edge:.5f}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _beta(stock: pd.DataFrame, bench: pd.DataFrame, lookback: int = 120) -> float:
    stock_ret = stock["Close"].astype(float).pct_change().dropna()
    bench_ret = bench["Close"].astype(float).pct_change().dropna()
    common = stock_ret.index.intersection(bench_ret.index)
    if len(common) < 30:
        return 1.0
    s = stock_ret.loc[common].tail(lookback)
    b = bench_ret.loc[common].tail(lookback)
    variance = float(np.var(b))
    if not math.isfinite(variance) or variance <= 1e-12:
        return 1.0
    beta = float(np.cov(s, b)[0, 1] / variance)
    if not math.isfinite(beta):
        return 1.0
    return max(-1.0, min(3.0, beta))


def _event_mask(stock: pd.DataFrame, row: Dict[str, Any]) -> pd.Series:
    close = stock["Close"].astype(float)
    open_ = stock["Open"].astype(float)
    high = stock["High"].astype(float)
    low = stock["Low"].astype(float)
    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    body = (close - open_).abs().clip(lower=1e-9)
    candle_range = (high - low).clip(lower=1e-9)
    lower_shadow = (pd.concat([open_, close], axis=1).min(axis=1) - low).clip(lower=0.0)
    lower_ratio = lower_shadow / body
    lower_range = lower_shadow / candle_range
    close_location = (close - low) / candle_range
    trend_ok = (close >= ma20) | (ma5 >= ma20)
    rejection = (lower_ratio >= 1.2) & (lower_range >= 0.25) & (close_location >= 0.52) & trend_ok
    confirmation = (close > high.shift(1)) & (close > ma5) & trend_ok
    weak_momentum = (close > ma5) & (close.pct_change(5) > 0) & trend_ok

    rejection_status = str(row.get("pullback_rejection_status") or "").upper()
    confirmation_status = str(row.get("pullback_confirmation_status") or "").upper()
    if "STRONG_CONFIRMED" in confirmation_status or confirmation_status == "CONFIRMED":
        return confirmation.fillna(False)
    if "CONFIRMED" in confirmation_status:
        return (confirmation | weak_momentum).fillna(False)
    if "WATCH" in rejection_status:
        return rejection.fillna(False)
    return weak_momentum.fillna(False)


def _overnight_events(stock: pd.DataFrame, bench: pd.DataFrame, row: Dict[str, Any], beta: float) -> list[dict[str, Any]]:
    mask = _event_mask(stock, row)
    events: list[dict[str, Any]] = []
    for idx in range(25, len(stock) - 1):
        if not bool(mask.iloc[idx]):
            continue
        entry = float(stock["Close"].iloc[idx])
        exit_ = float(stock["Open"].iloc[idx + 1])
        bench_entry = float(bench["Close"].iloc[idx])
        bench_exit = float(bench["Open"].iloc[idx + 1])
        if min(entry, exit_, bench_entry, bench_exit) <= 0:
            continue
        stock_overnight = exit_ / entry - 1.0
        bench_overnight = bench_exit / bench_entry - 1.0
        alpha = stock_overnight - bench_overnight
        beta_alpha = stock_overnight - beta * bench_overnight
        events.append({
            "date": stock.index[idx].strftime("%Y-%m-%d"),
            "stock_overnight_return": stock_overnight,
            "benchmark_overnight_return": bench_overnight,
            "alpha_return": alpha,
            "beta_adjusted_alpha": beta_alpha,
        })
    return events


def _summary_stats(events: list[dict[str, Any]], min_edge: float) -> dict[str, Any]:
    if not events:
        return {
            "sample_count": 0,
            "absolute_win_rate": None,
            "alpha_win_rate": None,
            "strong_alpha_win_rate": None,
            "mean_alpha": None,
            "median_alpha": None,
            "mean_beta_adjusted_alpha": None,
            "profit_factor": None,
            "max_loss": None,
            "max_consecutive_alpha_losses": 0,
        }
    stock_returns = [float(item["stock_overnight_return"]) for item in events]
    alpha_values = [float(item["beta_adjusted_alpha"]) for item in events]
    wins = [value for value in alpha_values if value > 0]
    losses = [value for value in alpha_values if value <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    max_streak = streak = 0
    for value in alpha_values:
        if value <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    return {
        "sample_count": len(events),
        "absolute_win_rate": round(sum(value > 0 for value in stock_returns) / len(events), 4),
        "alpha_win_rate": round(sum(value > 0 for value in alpha_values) / len(events), 4),
        "strong_alpha_win_rate": round(sum(value > min_edge for value in alpha_values) / len(events), 4),
        "mean_alpha": round(mean(float(item["alpha_return"]) for item in events), 6),
        "median_alpha": round(median(float(item["alpha_return"]) for item in events), 6),
        "mean_beta_adjusted_alpha": round(mean(alpha_values), 6),
        "median_beta_adjusted_alpha": round(median(alpha_values), 6),
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else None,
        "max_loss": round(min(alpha_values), 6),
        "max_consecutive_alpha_losses": max_streak,
    }


def validate_symbol_overnight_alpha(
    row: Dict[str, Any],
    *,
    period: str = "2y",
    min_edge: float = 0.001,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """Compute (or retrieve) overnight alpha evidence for a single symbol.

    Parameters
    ----------
    force_refresh : bool
        When True, skip the overnight-alpha cache entirely and recompute
        from fresh OHLCV data.  This is the right knob for interactive
        per-symbol "研判" (analyse) requests where the operator expects the
        very latest close to be reflected.
    """
    symbol = str(row.get("symbol") or row.get("ticker") or "").strip().upper()
    source_universes = row.get("source_universe_ids") or row.get("source_pools") or []
    benchmark, benchmark_source = benchmark_for_universes(source_universes)
    if not symbol:
        return {"symbol": "", "available": False, "reason": "missing_symbol"}
    profile = _signal_profile(row)
    cache_key = _cache_key(symbol, benchmark, benchmark_source, profile, period, min_edge)

    # ── cache-hit path (with freshness guard) ──────────────────────────
    if not force_refresh:
        cached = overnight_alpha_get(cache_key)
        if cached:
            # Only trust the overnight-alpha cache when the underlying OHLCV
            # data still covers the latest US market close.  If the market has
            # closed since the cache was written the cached alpha may be stale.
            stock_frame, _stock_source = get_daily_history(symbol, period=period)
            if stock_frame is not None and is_daily_data_current(stock_frame):
                cached["cache_hit"] = True
                return cached
            # Data is stale — fall through to recompute.

    # ── recompute path ─────────────────────────────────────────────────
    stock, bench, sources = _aligned_frames(symbol, benchmark, period)
    if stock.empty or bench.empty or len(stock) < 40:
        payload = {
            "symbol": symbol,
            "available": False,
            "reason": "insufficient_price_history",
            "benchmark": benchmark,
            "benchmark_source_universe": benchmark_source,
            "signal_profile": profile,
            "data_sources": sources,
        }
        overnight_alpha_set(
            cache_key,
            symbol=symbol,
            benchmark=benchmark,
            universe_key=benchmark_source,
            signal_profile=profile,
            period=period,
            min_edge=min_edge,
            payload=payload,
            ttl_seconds=max(900, min(_ALPHA_CACHE_TTL_SECONDS, 3600)),
        )
        return payload
    beta = _beta(stock, bench)
    events = _overnight_events(stock, bench, row, beta)
    stats = _summary_stats(events, min_edge)
    latest = events[-1] if events else None
    payload = {
        "symbol": symbol,
        "available": bool(events),
        "reason": "ok" if events else "no_matching_historical_events",
        "benchmark": benchmark,
        "benchmark_source_universe": benchmark_source,
        "signal_profile": profile,
        "period": period,
        "beta": round(beta, 4),
        "min_edge": min_edge,
        "stats": stats,
        "latest_event": latest,
        "data_sources": sources,
        "cache_hit": False,
    }
    overnight_alpha_set(
        cache_key,
        symbol=symbol,
        benchmark=benchmark,
        universe_key=benchmark_source,
        signal_profile=profile,
        period=period,
        min_edge=min_edge,
        payload=payload,
        ttl_seconds=_ALPHA_CACHE_TTL_SECONDS,
    )
    return payload


def validate_overnight_alpha_summary(
    rows: List[Dict[str, Any]],
    *,
    period: str = "2y",
    min_edge: float = 0.001,
    limit: int = 12,
) -> Dict[str, Any]:
    selected = [row for row in rows if str(row.get("symbol") or row.get("ticker") or "").strip()][:max(1, limit)]
    results = [validate_symbol_overnight_alpha(row, period=period, min_edge=min_edge) for row in selected]
    available = [item for item in results if item.get("available") and (item.get("stats") or {}).get("sample_count")]
    if not available:
        portfolio_stats = {"sample_count": 0, "alpha_win_rate": None, "mean_beta_adjusted_alpha": None}
    else:
        sample_count = sum(int((item.get("stats") or {}).get("sample_count") or 0) for item in available)
        weighted_alpha_win = sum(
            float((item.get("stats") or {}).get("alpha_win_rate") or 0) * int((item.get("stats") or {}).get("sample_count") or 0)
            for item in available
        ) / max(1, sample_count)
        weighted_strong = sum(
            float((item.get("stats") or {}).get("strong_alpha_win_rate") or 0) * int((item.get("stats") or {}).get("sample_count") or 0)
            for item in available
        ) / max(1, sample_count)
        weighted_alpha = sum(
            float((item.get("stats") or {}).get("mean_beta_adjusted_alpha") or 0) * int((item.get("stats") or {}).get("sample_count") or 0)
            for item in available
        ) / max(1, sample_count)
        portfolio_stats = {
            "sample_count": sample_count,
            "symbol_count": len(available),
            "alpha_win_rate": round(weighted_alpha_win, 4),
            "strong_alpha_win_rate": round(weighted_strong, 4),
            "mean_beta_adjusted_alpha": round(weighted_alpha, 6),
        }
    return {
        "available": bool(available),
        "period": period,
        "min_edge": min_edge,
        "method": "close_to_next_open_beta_adjusted_alpha",
        "note": "Alpha win rate means close-to-next-open beta-adjusted alpha > 0; it is not absolute win rate or live-trading certainty.",
        "portfolio": portfolio_stats,
        "results": results,
        "cache": {
            "hit_count": sum(1 for item in results if item.get("cache_hit")),
            "miss_count": sum(1 for item in results if not item.get("cache_hit")),
        },
    }
