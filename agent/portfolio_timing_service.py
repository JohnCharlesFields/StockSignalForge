"""Portfolio-level market timing and gross-exposure gate (R3).

This service lifts the macro/VIX work from per-symbol evidence into an
account-level throttle.  It does not create buy/sell advice by itself; it gives
the research cockpit a gross-exposure multiplier that can cap all candidates
when the market backdrop is poor.

Inputs are deliberately limited to data the project already has:
* VIX regime from ``macro_panic_service``.
* SPY / QQQ daily OHLCV from ``market_data_service``.
* A small breadth proxy over liquid ETFs / mega-cap growth names.

Keep this file ASCII-only for Windows and container builds.
"""

from __future__ import annotations

import math
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable

import pandas as pd

from macro_panic_service import get_vix_regime
from market_data_service import aggregate_data_quality, get_daily_history


DEFAULT_BREADTH_SYMBOLS = (
    "SPY", "QQQ", "IWM", "DIA", "SMH", "XLK", "XLF", "XLY",
)

_CACHE_ROOT = Path(os.environ.get("VIBE_MARKET_DATA_CACHE_DIR", "/app/agent/data_cache/market_data"))
_GATE_CACHE_PATH = _CACHE_ROOT / "macro" / "portfolio_timing_gate.json"
_GATE_TTL_SECONDS = int(os.environ.get("PORTFOLIO_TIMING_GATE_TTL_SECONDS", "900") or 900)


def _read_gate_cache() -> Dict[str, Any] | None:
    try:
        if _GATE_CACHE_PATH.exists() and time.time() - _GATE_CACHE_PATH.stat().st_mtime < _GATE_TTL_SECONDS:
            payload = json.loads(_GATE_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                payload["cache_hit"] = True
                return payload
    except (OSError, json.JSONDecodeError):
        return None
    return None


def _write_gate_cache(payload: Dict[str, Any]) -> None:
    try:
        _GATE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = _GATE_CACHE_PATH.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(_GATE_CACHE_PATH)
    except OSError:
        pass


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _trend_state(symbol: str, period: str = "1y") -> Dict[str, Any]:
    frame, source = get_daily_history(symbol, period=period, allow_yfinance_fallback=True)
    close = pd.to_numeric(frame.get("Close"), errors="coerce").dropna() if not frame.empty else pd.Series(dtype=float)
    if len(close) < 60:
        return {
            "symbol": symbol,
            "available": False,
            "source": source,
            "reason": "insufficient_history",
        }
    ma50 = float(close.rolling(50).mean().iloc[-1])
    ma200_window = min(200, len(close))
    ma200 = float(close.rolling(ma200_window).mean().iloc[-1])
    last = float(close.iloc[-1])
    distance_ma200 = last / ma200 - 1.0 if ma200 > 0 else 0.0
    distance_ma50 = last / ma50 - 1.0 if ma50 > 0 else 0.0
    ret20 = float(close.iloc[-1] / close.iloc[-min(21, len(close))] - 1.0)
    trend_ok = bool(last >= ma200 and ma50 >= ma200 * 0.995)
    return {
        "symbol": symbol,
        "available": True,
        "source": source,
        "last_close": round(last, 4),
        "ma50": round(ma50, 4),
        "ma200": round(ma200, 4),
        "distance_ma50": round(distance_ma50, 4),
        "distance_ma200": round(distance_ma200, 4),
        "ret20": round(ret20, 4),
        "trend_ok": trend_ok,
    }


def _breadth_proxy(
    symbols: Iterable[str] = DEFAULT_BREADTH_SYMBOLS,
    precomputed: Dict[str, Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    rows: list[Dict[str, Any]] = []
    sources: dict[str, str] = {}
    precomputed = precomputed or {}
    for symbol in symbols:
        state = precomputed.get(symbol) or _trend_state(symbol, period="1y")
        if not state.get("available"):
            continue
        rows.append(state)
        sources[str(symbol)] = str(state.get("source") or "")
    if not rows:
        return {
            "available": False,
            "breadth_above_ma50": None,
            "breadth_above_ma200": None,
            "sample_size": 0,
            "data_quality": aggregate_data_quality(sources),
        }
    above50 = sum(1 for row in rows if _finite(row.get("distance_ma50")) >= 0)
    above200 = sum(1 for row in rows if _finite(row.get("distance_ma200")) >= 0)
    return {
        "available": True,
        "breadth_above_ma50": round(above50 / len(rows), 4),
        "breadth_above_ma200": round(above200 / len(rows), 4),
        "sample_size": len(rows),
        "members": rows,
        "data_quality": aggregate_data_quality(sources),
    }


def _exposure_from_conditions(
    *,
    vix_mode: str,
    spy_ok: bool,
    qqq_ok: bool,
    breadth50: float | None,
    breadth200: float | None,
) -> tuple[float, str, list[str]]:
    flags: list[str] = []
    trend_count = int(spy_ok) + int(qqq_ok)
    breadth50 = 0.5 if breadth50 is None else breadth50
    breadth200 = 0.5 if breadth200 is None else breadth200

    if vix_mode == "crisis_guard":
        flags.append("vix_crisis_guard")
        return 0.0, "cash_guard", flags
    if trend_count == 0 and breadth200 < 0.35:
        flags.extend(["index_below_ma200", "breadth_weak"])
        return 0.25, "risk_off", flags
    if vix_mode in {"panic_reclaim", "panic_watch"}:
        flags.append("vix_panic_zone")
        if trend_count >= 1 and breadth50 >= 0.45:
            return 0.50, "panic_reclaim_research", flags
        return 0.25, "panic_defensive", flags
    if vix_mode == "defensive":
        flags.append("vix_defensive")
        return (0.75 if trend_count >= 1 and breadth200 >= 0.45 else 0.50), "defensive", flags
    if trend_count == 2 and breadth50 >= 0.55 and breadth200 >= 0.50:
        return 1.0, "risk_on", flags
    if trend_count >= 1 and breadth200 >= 0.45:
        return 0.75, "selective_risk_on", flags
    flags.append("mixed_market")
    return 0.50, "neutral_selective", flags


def portfolio_timing_gate(force_refresh: bool = False) -> Dict[str, Any]:
    """Return an account-level gross exposure gate for research workflows."""
    if not force_refresh:
        cached = _read_gate_cache()
        if cached is not None:
            return cached
    macro = get_vix_regime(force_refresh=force_refresh)
    spy = _trend_state("SPY")
    qqq = _trend_state("QQQ")
    breadth = _breadth_proxy(precomputed={"SPY": spy, "QQQ": qqq})
    exposure, regime, flags = _exposure_from_conditions(
        vix_mode=str(macro.get("mode") or "normal"),
        spy_ok=bool(spy.get("trend_ok")),
        qqq_ok=bool(qqq.get("trend_ok")),
        breadth50=breadth.get("breadth_above_ma50") if breadth.get("available") else None,
        breadth200=breadth.get("breadth_above_ma200") if breadth.get("available") else None,
    )
    mode_cn = {
        "cash_guard": "现金防守",
        "risk_off": "风险关闭",
        "panic_reclaim_research": "恐慌承接研究",
        "panic_defensive": "恐慌防守",
        "defensive": "防守观察",
        "risk_on": "风险开启",
        "selective_risk_on": "选择性风险开启",
        "neutral_selective": "中性精选",
    }.get(regime, "中性精选")
    reason_parts = [
        f"VIX mode={macro.get('mode') or 'unknown'}",
        f"SPY trend={'ok' if spy.get('trend_ok') else 'weak'}",
        f"QQQ trend={'ok' if qqq.get('trend_ok') else 'weak'}",
    ]
    if breadth.get("available"):
        reason_parts.append(f"breadth50={breadth.get('breadth_above_ma50'):.1%}")
        reason_parts.append(f"breadth200={breadth.get('breadth_above_ma200'):.1%}")
    payload = {
        "available": True,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "cache_hit": False,
        "regime": regime,
        "regime_cn": mode_cn,
        "gross_exposure_multiplier": round(exposure, 4),
        "max_gross_exposure_pct": round(exposure * 100.0, 2),
        "new_position_scale": round(exposure, 4),
        "risk_flags": flags,
        "reason": "; ".join(reason_parts),
        "macro_regime": macro,
        "index_trend": {"SPY": spy, "QQQ": qqq},
        "breadth": breadth,
        "method_note": (
            "Portfolio gate uses VIX regime + SPY/QQQ 50/200-day trend + a "
            "small breadth proxy. It caps account-level research exposure; it "
            "does not override per-symbol risk checks or create trade advice."
        ),
    }
    _write_gate_cache(payload)
    return payload
