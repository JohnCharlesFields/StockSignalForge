"""Macro panic regime service for VIX-aware research workflows.

The service deliberately distinguishes real CBOE VIX data from fallback
proxies.  It is a research context input, not a trading signal by itself.
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from market_data_service import external_data_allowed, get_daily_history
from fincept_adapters import get_cboe_vix_latest


_CACHE_ROOT = Path(os.environ.get("VIBE_MARKET_DATA_CACHE_DIR", "/app/agent/data_cache/market_data"))
_CACHE_PATH = _CACHE_ROOT / "macro" / "vix_regime.json"
_TTL_SECONDS = int(os.environ.get("VIX_REGIME_CACHE_TTL_SECONDS", "900") or 900)


def _read_cache() -> dict[str, Any] | None:
    try:
        if _CACHE_PATH.exists() and time.time() - _CACHE_PATH.stat().st_mtime < _TTL_SECONDS:
            payload = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                payload["cache_hit"] = True
                return payload
    except (OSError, json.JSONDecodeError):
        return None
    return None


def _write_cache(payload: dict[str, Any]) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = _CACHE_PATH.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(_CACHE_PATH)
    except OSError:
        pass


def _last_close(frame: pd.DataFrame) -> float | None:
    if frame.empty or "Close" not in frame:
        return None
    value = pd.to_numeric(frame["Close"], errors="coerce").dropna()
    if value.empty:
        return None
    return float(value.iloc[-1])


def _realized_vol_proxy(symbols: list[str] | None = None) -> dict[str, Any]:
    symbols = symbols or ["SPY", "QQQ"]
    frames: dict[str, pd.DataFrame] = {}
    sources: dict[str, str] = {}
    for symbol in symbols:
        frame, source = get_daily_history(symbol, period="4mo", allow_yfinance_fallback=True)
        if not frame.empty:
            frames[symbol] = frame
            sources[symbol] = source
    if not frames:
        return {
            "available": False,
            "reason": "SPY/QQQ price history unavailable; cannot compute realized-vol proxy.",
            "sources": sources,
        }
    proxy_values: list[float] = []
    details: dict[str, Any] = {}
    for symbol, frame in frames.items():
        close = pd.to_numeric(frame["Close"], errors="coerce").dropna()
        if len(close) < 25:
            continue
        returns = close.pct_change().dropna()
        vol20 = float(returns.rolling(20).std().iloc[-1] * math.sqrt(252) * 100)
        vol5 = float(returns.rolling(5).std().iloc[-1] * math.sqrt(252) * 100)
        high60 = float(close.rolling(min(60, len(close))).max().iloc[-1])
        last = float(close.iloc[-1])
        drawdown = max(0.0, (high60 - last) / high60 * 100.0) if high60 > 0 else 0.0
        value = 0.55 * vol20 + 0.30 * vol5 + 0.80 * drawdown
        proxy_values.append(value)
        details[symbol] = {
            "last_close": round(last, 4),
            "realized_vol_20d": round(vol20, 2),
            "realized_vol_5d": round(vol5, 2),
            "drawdown_60d_pct": round(drawdown, 2),
            "proxy_value": round(value, 2),
            "source": sources.get(symbol),
        }
    if not proxy_values:
        return {
            "available": False,
            "reason": "Not enough SPY/QQQ bars to compute realized-vol proxy.",
            "sources": sources,
        }
    return {
        "available": True,
        "value": round(max(proxy_values), 2),
        "details": details,
        "sources": sources,
    }


def _vix_etf_adjustment() -> dict[str, Any]:
    adjustments: list[float] = []
    details: dict[str, Any] = {}
    for symbol in ["VIXY", "VXX"]:
        frame, source = get_daily_history(symbol, period="2mo", allow_yfinance_fallback=True)
        close = pd.to_numeric(frame.get("Close"), errors="coerce").dropna() if not frame.empty else pd.Series(dtype=float)
        if len(close) < 6:
            continue
        ret5 = float(close.iloc[-1] / close.iloc[-6] - 1.0)
        adjustment = max(-3.0, min(8.0, ret5 * 20.0))
        adjustments.append(adjustment)
        details[symbol] = {
            "five_day_return": round(ret5, 4),
            "adjustment": round(adjustment, 2),
            "source": source,
        }
    if not adjustments:
        return {"available": False, "adjustment": 0.0, "details": details}
    return {"available": True, "adjustment": round(max(adjustments), 2), "details": details}


def _classify(value: float | None, source_type: str) -> dict[str, Any]:
    if value is None:
        return {
            "level": "unknown",
            "label_cn": "宏观波动未知",
            "mode": "normal",
            "mode_cn": "正常研究模式",
            "panic_score": 0.0,
            "systemic_crisis": False,
            "systemic_risk_flags": ["vix_unavailable"],
        }
    flags: list[str] = []
    systemic = False
    if value < 15:
        level, label, mode, score = "low_vol", "低波动/偏贪婪", "risk_control", 0.05
    elif value < 20:
        level, label, mode, score = "normal", "正常波动", "normal", 0.15
    elif value < 30:
        level, label, mode, score = "risk_on_watch", "风险升温", "defensive", 0.35
    elif value < 35:
        level, label, mode, score = "panic_watch", "恐慌预备区", "panic_watch", 0.55
    elif value < 45:
        level, label, mode, score = "extreme_panic", "极端恐慌", "panic_reclaim", 0.80
    else:
        level, label, mode, score = "crisis", "危机级别", "crisis_guard", 0.95
        systemic = True
        flags.append("vix_above_45")
    if source_type != "real_vix":
        flags.append("proxy_not_official_vix")
    return {
        "level": level,
        "label_cn": label,
        "mode": mode,
        "mode_cn": {
            "risk_control": "低波动防追高",
            "normal": "正常研究模式",
            "defensive": "风险升温防守",
            "panic_watch": "恐慌承接观察",
            "panic_reclaim": "恐慌承接模式",
            "crisis_guard": "危机过滤模式",
        }.get(mode, "正常研究模式"),
        "panic_score": score,
        "systemic_crisis": systemic,
        "systemic_risk_flags": flags,
    }


def get_vix_regime(force_refresh: bool = False) -> dict[str, Any]:
    """Return current VIX regime with cache and proxy fallback."""
    if not force_refresh:
        cached = _read_cache()
        if cached is not None:
            return cached

    warnings: list[str] = []
    source = "unavailable"
    source_type = "unavailable"
    value: float | None = None
    real_source = None
    data_as_of_date: str | None = None

    if external_data_allowed():
        cboe_vix = get_cboe_vix_latest("VIX")
        if cboe_vix.get("available") and cboe_vix.get("value"):
            value = round(float(cboe_vix["value"]), 2)
            source = str(cboe_vix.get("source") or "cboe:VIX_History.csv")
            source_type = "real_vix"
            real_source = source
            data_as_of_date = str(cboe_vix.get("as_of_date") or "") or None
        else:
            warnings.append(f"CBOE VIX unavailable: {cboe_vix.get('reason') or 'unknown'}")
    else:
        warnings.append("page_read_cache_only: skip CBOE VIX fetch")

    if value is None:
        real_frame, real_source = get_daily_history("^VIX", period="1y", allow_yfinance_fallback=True)
        real_value = _last_close(real_frame)
        if real_value is not None and real_value > 0:
            value = round(real_value, 2)
            source = real_source
            source_type = "real_vix"
            data_as_of_date = real_frame.index.max().date().isoformat()

    if value is None:
        from gildata_shadow_service import cached_vix
        from market_calendar import most_recent_session

        shadow_vix = cached_vix(most_recent_session().isoformat())
        if shadow_vix:
            value = round(float(shadow_vix["value"]), 2)
            source = "gildata:VIX_daily:cached"
            source_type = "real_vix"
            real_source = source
            data_as_of_date = str(shadow_vix["as_of"])
            warnings.append("聚源仅提供已核验交易日的日收盘 VIX，非实时行情。")

    if value is None:
        warnings.append("真实 ^VIX 暂不可用，已降级为 SPY/QQQ 实现波动率代理。")
        proxy = _realized_vol_proxy()
        etf_proxy = _vix_etf_adjustment()
        if proxy.get("available"):
            value = round(float(proxy["value"]) + float(etf_proxy.get("adjustment", 0.0) or 0.0), 2)
            source = "realized_vol_proxy"
            source_type = "proxy"
        else:
            warnings.append(str(proxy.get("reason") or "VIX proxy unavailable."))
            proxy = {"available": False}
            etf_proxy = {"available": False, "adjustment": 0.0}

    classification = _classify(value, source_type)
    payload = {
        "available": value is not None,
        "value": value,
        "display_name": "VIX" if source_type == "real_vix" else "VIX Proxy",
        "source": source,
        "source_type": source_type,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "data_as_of_date": data_as_of_date,
        "cache_hit": False,
        "warnings": warnings,
        "method_note": (
            "CBOE VIX CSV 优先；随后使用 ^VIX 行情或可选的聚源日值缓存；仍不可用时使用 SPY/QQQ 实现波动率等构建 VIX Proxy。"
        ),
        **classification,
    }
    if source_type != "real_vix":
        payload["proxy_detail"] = {
            "realized_vol_proxy": proxy if "proxy" in locals() else {},
            "vix_etf_adjustment": etf_proxy if "etf_proxy" in locals() else {},
        }
    _write_cache(payload)
    return payload


def panic_evidence_for_opportunity(opportunity: dict[str, Any] | None, regime: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a per-symbol macro panic evidence component."""
    regime = regime or get_vix_regime()
    if not regime.get("available"):
        return {}
    mode = str(regime.get("mode") or "")
    value = float(regime.get("value") or 0.0)
    opportunity = opportunity or {}
    opportunity_score = float(opportunity.get("opportunity_score") or 0.0)
    risk_level = str(opportunity.get("risk_level") or "")
    liquidity_score = float(opportunity.get("liquidity_score") or 0.0)
    gex = opportunity.get("gamma_exposure") or {}
    probability = 0.50
    label = "宏观环境中性"
    reason = f"{regime.get('display_name')} {value:.2f}，{regime.get('label_cn')}。"
    if mode == "panic_reclaim":
        quality_bonus = max(0.0, min(0.08, (opportunity_score - 45.0) / 100.0))
        liquidity_bonus = max(0.0, min(0.03, liquidity_score * 0.03))
        risk_penalty = 0.04 if risk_level == "高" else 0.0
        gex_penalty = 0.04 if str(gex.get("risk_level") or "").lower() == "high" else 0.0
        probability = 0.54 + quality_bonus + liquidity_bonus - risk_penalty - gex_penalty
        label = "宏观恐慌承接"
        reason += " 极端恐慌下，只对质量、流动性和风险过滤后的候选增加承接权重。"
    elif mode == "panic_watch":
        probability = 0.52 + max(0.0, min(0.04, (opportunity_score - 50.0) / 120.0))
        label = "恐慌承接观察"
    elif mode == "crisis_guard":
        probability = 0.40
        label = "系统性危机过滤"
        reason += " VIX 已进入危机级别，默认降低承接优先级。"
    elif mode == "defensive":
        probability = 0.47
        label = "风险升温防守"
    elif mode == "risk_control":
        probability = 0.48
        label = "低波动防追高"
    else:
        probability = 0.50
    probability = max(0.30, min(0.68, probability))
    return {
        "id": "macro_vix_regime",
        "label": label,
        "probability": probability,
        "source_probability": probability,
        "reliability": 0.72 if regime.get("source_type") == "real_vix" else 0.52,
        "weight": 0.12 if mode in {"panic_reclaim", "panic_watch", "crisis_guard"} else 0.06,
        "regime": regime,
        "reason": reason,
    }
