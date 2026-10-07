"""Cash-equity candidate scoring helpers shared by API and tests.

Keep this module ASCII-only so Windows terminals and container builds do not
turn Chinese UI labels into mojibake.
"""

from __future__ import annotations

from typing import Any, Dict, List


STOCK_SIGNAL_BULLISH_STRATEGIES = {"bull_call_spread", "bull_put_spread", "single_call"}

CORE_SIGNAL = "\u6838\u5fc3\u89c2\u5bdf"
WATCH_SIGNAL = "\u666e\u901a\u89c2\u5bdf"
EXCLUDE_SIGNAL = "\u6682\u4e0d\u7eb3\u5165"
STRICT_LABEL = "\u4e25\u683c\u5019\u9009"
BACKUP_LABEL = "\u5907\u9009\u89c2\u5bdf"
RISK_HIGH = "\u9ad8"
RISK_MEDIUM = "\u4e2d"
RISK_LOW = "\u4f4e"
UNMARKED_POOL = "\u672a\u6807\u6ce8"
BACKUP_TREND_FLAG = (
    "\u5907\u9009\u539f\u56e0\uff1a30\u65e5\u8d8b\u52bf\u672a\u8f6c\u6b63\uff0c"
    "\u4ec5\u4f5c\u89c2\u5bdf\uff0c\u4e0d\u4f5c\u4e3a\u8ba1\u5212\u5019\u9009\u3002"
)
BACKUP_OPTION_FLAG = (
    "\u5907\u9009\u539f\u56e0\uff1a\u671f\u6743\u8f85\u52a9\u7b56\u7565\u975e\u770b\u591a\uff0c"
    "\u9700\u7b49\u5f85\u5b9e\u80a1\u91cf\u4ef7\u786e\u8ba4\u3002"
)
OPTION_EVIDENCE_MISSING_FLAG = (
    "\u671f\u6743\u8bc1\u636e\u7f3a\u5931\uff1a\u672a\u80fd\u6784\u5efa\u771f\u5b9e\u53ef\u6267\u884c\u671f\u6743\u817f\uff0c"
    "\u672c\u6761\u4ec5\u6309\u5b9e\u80a1\u91cf\u4ef7\u4e0e\u98ce\u9669\u4fe1\u53f7\u89c2\u5bdf\u3002"
)
OPTION_ONLY_RISK_MARKERS = (
    "option",
    "quoted_price",
    "thin_option",
    "model_pop",
    "model_ev",
    "debit_strategy",
    "preferred_position_size",
)


def _equity_proxy_score(raw_score: float, trend: float, stress: float, confidence: float) -> tuple[float, bool]:
    """Return a usable stock-research score when option evidence is missing."""
    if raw_score > 0:
        return raw_score, False
    trend_base = min(max(trend + 0.10, 0.0) / 0.45, 1.0) * 3.2
    stress_base = min(max(stress, 0.0) / 10.0, 1.0) * 1.4
    confidence_base = min(max(confidence, 0.0) / 10.0, 1.0) * 0.8
    proxy = max(1.0, min(6.0, 1.0 + trend_base + stress_base + confidence_base))
    return round(proxy, 2), True


def _equity_liquidity_score(result: Dict[str, Any], option_liquidity: float) -> float:
    """Score cash-equity liquidity from recent dollar volume, falling back to options liquidity."""
    dollar_volumes: List[float] = []
    for row in (result.get("recent_ohlcv") or [])[-20:]:
        try:
            close = float((row or {}).get("Close", 0) or 0)
            volume = float((row or {}).get("Volume", 0) or 0)
        except (TypeError, ValueError):
            continue
        if close > 0 and volume > 0:
            dollar_volumes.append(close * volume)
    if not dollar_volumes:
        return round(min(max(option_liquidity, 0.0), 1.0), 4)
    avg_dollar_volume = sum(dollar_volumes) / len(dollar_volumes)
    thresholds = [
        (500_000_000, 1.00),
        (200_000_000, 0.90),
        (100_000_000, 0.80),
        (50_000_000, 0.68),
        (25_000_000, 0.55),
        (10_000_000, 0.42),
        (3_000_000, 0.25),
        (1, 0.12),
    ]
    for threshold, score in thresholds:
        if avg_dollar_volume >= threshold:
            return score
    return 0.0


def _is_option_only_risk(flag: str) -> bool:
    text = flag.lower()
    return any(marker in text for marker in OPTION_ONLY_RISK_MARKERS)


def stock_signal_item(
    result: Dict[str, Any],
    memberships: Dict[str, List[str]],
    run_id: str,
) -> Dict[str, Any]:
    ticker = str(result.get("ticker") or "")
    spot = float(result.get("spot", 0) or 0)
    trend = float(result.get("trend_30d", 0) or 0)
    raw_score = float(result.get("research_score", result.get("final_score", 0)) or 0)
    option_liquidity = float(result.get("liquidity_score", 0) or 0)
    liquidity = _equity_liquidity_score(result, option_liquidity)
    risk_flags = [str(x) for x in (result.get("risk_flags") or [])]
    rejection_reasons = [str(x) for x in (result.get("rejection_reasons") or [])]
    confidence = float(result.get("confidence_score", 0) or 0)
    stress = float(result.get("stress_score", 0) or 0)
    score, score_is_proxy = _equity_proxy_score(raw_score, trend, stress, confidence)
    if score_is_proxy:
        risk_flags.append(OPTION_EVIDENCE_MISSING_FLAG)

    trend_component = min(max(trend, 0.0) / 0.30, 1.0) * 38.0
    model_component = min(max(score, 0.0) / 10.0, 1.0) * 32.0
    resilience_component = min(max(stress, 0.0) / 10.0, 1.0) * 18.0
    option_bonus = min(max(liquidity, 0.0), 1.0) * 8.0 + min(max(confidence, 0.0) / 10.0, 1.0) * 4.0
    overheat_penalty = min(20.0, max(0.0, trend - 0.35) * 30.0 + (8.0 if trend > 0.35 else 0.0))
    risk_penalty = min(10.0, len(risk_flags) * 2.0)
    opportunity_score = round(
        max(0.0, trend_component + model_component + resilience_component + option_bonus - overheat_penalty - risk_penalty),
        1,
    )

    if opportunity_score >= 70:
        signal = CORE_SIGNAL
        signal_tone = "strong"
        opportunity_tier = "core"
    elif opportunity_score >= 45:
        signal = WATCH_SIGNAL
        signal_tone = "watch"
        opportunity_tier = "watch"
    else:
        signal = EXCLUDE_SIGNAL
        signal_tone = "neutral"
        opportunity_tier = "exclude"

    equity_risk_flags = [flag for flag in risk_flags if not _is_option_only_risk(flag)]
    gamma_high = str(result.get("gamma_risk_level", "")).lower() == "high"
    if abs(trend) >= 0.45 or liquidity < 0.15 or gamma_high or len(equity_risk_flags) >= 2:
        risk_level = RISK_HIGH
    elif abs(trend) >= 0.25 or liquidity < 0.35 or equity_risk_flags:
        risk_level = RISK_MEDIUM
    else:
        risk_level = RISK_LOW

    source_pools = result.get("universe_memberships") or memberships.get(ticker) or [UNMARKED_POOL]
    return {
        "ticker": ticker,
        "signal": signal,
        "signal_tone": signal_tone,
        "spot": spot,
        "price_as_of": result.get("price_as_of"),
        "market_cap": result.get("market_cap"),
        "trailing_pe": result.get("trailing_pe"),
        "forward_pe": result.get("forward_pe"),
        "shares_outstanding": result.get("shares_outstanding"),
        "entry_zone_low": round(spot * 0.98, 2),
        "entry_zone_high": round(spot * 1.02, 2),
        "trend_30d": trend,
        "trend_class": result.get("trend_class", ""),
        "opportunity_score": opportunity_score,
        "opportunity_tier": opportunity_tier,
        "overheat_penalty": round(overheat_penalty, 1),
        "risk_penalty": round(risk_penalty, 1),
        "research_score": score,
        "raw_research_score": raw_score,
        "research_score_is_proxy": score_is_proxy,
        "confidence_score": confidence,
        "stress_score": stress,
        "research_tier": result.get("research_tier", ""),
        "liquidity_score": liquidity,
        "equity_liquidity_score": liquidity,
        "option_liquidity_score": option_liquidity,
        "liquidity_grade": result.get("liquidity_grade", ""),
        "iv_hv_ratio": float(result.get("iv_hv_ratio", 0) or 0),
        "gamma_exposure": result.get("gamma_exposure") or {},
        "gamma_risk_level": result.get("gamma_risk_level", ""),
        "data_sources": result.get("cache_sources") or {},
        "theme": result.get("theme", ""),
        "source_pools": source_pools,
        "risk_level": risk_level,
        "risk_flags": risk_flags,
        "rejection_reasons": rejection_reasons,
        "watch_levels": result.get("watch_levels") or {},
        "recent_closes": result.get("recent_closes") or [],
        "recent_ohlcv": result.get("recent_ohlcv") or [],
        "pullback_rejection": result.get("pullback_rejection") or {},
        "pullback_confirmation": result.get("pullback_confirmation") or {},
        "option_evidence": {
            "strategy": result.get("primary_strategy", ""),
            "strategy_desc": result.get("strategy_desc", ""),
            "status": result.get("option_evidence_status", ""),
            "market_status": result.get("option_market_status", ""),
            "message": result.get("option_evidence_message", ""),
            "chain_diagnostics": result.get("option_chain_diagnostics") or {},
            "has_listed_options": result.get("option_market_status") == "listed_options",
            "tradable_structure": bool(result.get("tradable")),
            "pop": float(result.get("pop", 0) or 0),
            "model_pop": float(result.get("model_pop", result.get("pop", 0)) or 0),
            "historical_barrier_win_rate": (
                float(result["historical_barrier_win_rate"])
                if result.get("historical_barrier_win_rate") is not None
                else None
            ),
            "conservative_pop_range": result.get("conservative_pop_range") or {},
            "expected_value": float(result.get("expected_value", 0) or 0),
            "quote_quality": result.get("quote_quality", ""),
            "liquidity_score": option_liquidity,
            "available": bool(result.get("tradable")),
        },
        "report_pdf_url": f"/runs/{run_id}/report.pdf?download=1",
    }


def stock_signal_candidate_pool(
    pool_results: List[Dict[str, Any]],
    memberships: Dict[str, List[str]],
    run_id: str,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Build strict candidates first, then fill the view with backup ideas."""
    strict: List[Dict[str, Any]] = []
    backups: List[Dict[str, Any]] = []

    for result in pool_results:
        item = stock_signal_item(result, memberships, run_id)
        if item["spot"] <= 0:
            continue
        is_strict = (
            str(result.get("primary_strategy")) in STOCK_SIGNAL_BULLISH_STRATEGIES
            and item["trend_30d"] > 0
            and item["opportunity_tier"] != "exclude"
        )
        if is_strict:
            item["candidate_type"] = "strict"
            item["candidate_label"] = STRICT_LABEL
            strict.append(item)
            continue

        item = dict(item)
        item["candidate_type"] = "backup"
        item["candidate_label"] = BACKUP_LABEL
        item["signal"] = BACKUP_LABEL
        item["signal_tone"] = "watch" if item["opportunity_score"] >= 25 else "neutral"
        item["opportunity_tier"] = "backup"
        item["risk_flags"] = list(item.get("risk_flags") or [])
        if item["trend_30d"] <= 0:
            item["risk_flags"].append(BACKUP_TREND_FLAG)
        if str(result.get("primary_strategy")) not in STOCK_SIGNAL_BULLISH_STRATEGIES:
            item["risk_flags"].append(BACKUP_OPTION_FLAG)
        backups.append(item)

    strict.sort(key=lambda item: (-item["opportunity_score"], -item["trend_30d"], item["ticker"]))
    backups.sort(key=lambda item: (-item["opportunity_score"], -abs(item["trend_30d"]), item["ticker"]))
    selected = strict[:limit]
    if len(selected) < limit:
        selected_tickers = {item["ticker"] for item in selected}
        selected.extend(item for item in backups if item["ticker"] not in selected_tickers)
    return selected[:limit]
