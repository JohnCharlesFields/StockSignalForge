"""Dependency-light sort helpers for the three-layer research overview."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional


_EMPIRICAL_SOURCES = {"期权历史障碍检验", "单侧动量检验"}
_SORT_WEIGHTS = {
    "empirical_win": 0.34,
    "proxy_win": 0.12,
    "probability": 0.28,
    "risk_reward": 0.18,
    "execution": 0.08,
}


def win_rate_sort_metrics(
    opportunity: Optional[Dict[str, Any]],
    peer_earnings: Optional[Dict[str, Any]],
    hypothesis_test: Dict[str, Any],
    unified_probability: float,
    calibrated_probability: Optional[float] = None,
    portfolio_timing: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    option_evidence = (opportunity or {}).get("option_evidence") or {}
    historical = option_evidence.get("historical_barrier_win_rate")
    historical_source = "期权历史障碍检验"
    historical_is_proxy = False
    if historical is None and hypothesis_test.get("p_value") is not None:
        historical = 1.0 - float(hypothesis_test.get("p_value") or 0.5)
        historical_source = "单侧动量检验"
    if historical is None and peer_earnings:
        raw_probability = float(peer_earnings.get("research_probability", unified_probability) or unified_probability)
        similarity = float(peer_earnings.get("peer_similarity", 0.7) or 0.7)
        historical = 0.5 + (raw_probability - 0.5) * similarity
        historical_source = "同行接力历史代理"
        historical_is_proxy = True
    if historical is None:
        historical = None
        historical_source = "缺少历史验证"
        historical_is_proxy = True
    historical_win_rate = None if historical is None else max(0.0, min(1.0, float(historical or 0.0)))
    empirical_win_rate = historical_win_rate if historical_source in _EMPIRICAL_SOURCES else None
    proxy_win_rate = historical_win_rate if historical_is_proxy and historical_win_rate is not None else None
    historical_display_rate = historical_win_rate if historical_win_rate is not None else 0.5

    expected_value = float(option_evidence.get("expected_value", 0) or 0)
    expected_value_gross = float(option_evidence.get("expected_value_gross", expected_value) or 0)
    max_loss = float((opportunity or {}).get("max_loss", 0) or 0)
    if max_loss <= 0:
        max_loss = float(hypothesis_test.get("max_test_loss", 0) or 0)
    risk_reward = expected_value / max_loss if max_loss > 0 else 0.0
    if peer_earnings:
        expected_catch_up = max(0.0, float(peer_earnings.get("expected_catch_up_return", 0) or 0))
        invalidation_pct = max(0.01, float(hypothesis_test.get("invalidation_pct", 0.05) or 0.05))
        risk_reward = max(risk_reward, expected_catch_up / invalidation_pct)
    risk_reward_score = max(0.0, min(1.0, (risk_reward + 0.25) / 2.25))

    liquidity = float((opportunity or {}).get("liquidity_score", 0) or 0)
    if hypothesis_test.get("eligible"):
        execution_score = 0.55 + min(0.35, liquidity * 0.35)
    elif hypothesis_test.get("sizing_mode") == "paper_track":
        execution_score = 0.25
    else:
        execution_score = min(0.35, liquidity * 0.35)
    if peer_earnings and not opportunity:
        execution_score = max(execution_score, 0.35 if hypothesis_test.get("eligible") else 0.20)
    execution_score = max(0.0, min(1.0, execution_score))

    unified_probability = max(0.0, min(1.0, float(unified_probability or 0.0)))
    probability_for_ranking = unified_probability
    # R1 calibration hook. Shadow by default: the empirically calibrated
    # probability is surfaced for display but does NOT move the ranking unless
    # ENABLE_CALIBRATED_RANKING is explicitly turned on.
    calibrated = None
    if calibrated_probability is not None:
        calibrated = max(0.0, min(1.0, float(calibrated_probability)))
    calibrated_ranking_enabled = os.getenv("ENABLE_CALIBRATED_RANKING", "0").lower() not in {"0", "false", "no"}
    if calibrated_ranking_enabled and calibrated is not None:
        # Replace the asserted unified-probability term with the empirical one.
        probability_for_ranking = calibrated
    empirical_component = empirical_win_rate if empirical_win_rate is not None else 0.5
    proxy_component = proxy_win_rate if proxy_win_rate is not None else 0.5
    sort_score = (
        empirical_component * _SORT_WEIGHTS["empirical_win"]
        + proxy_component * _SORT_WEIGHTS["proxy_win"]
        + probability_for_ranking * _SORT_WEIGHTS["probability"]
        + risk_reward_score * _SORT_WEIGHTS["risk_reward"]
        + execution_score * _SORT_WEIGHTS["execution"]
    )
    exposure_multiplier = 1.0
    portfolio_regime = None
    portfolio_flags: list[str] = []
    # R3 gate. Default ON (it is live); set ENABLE_PORTFOLIO_TIMING=0 to run it
    # in shadow -- the regime/multiplier are still surfaced for display/A-B, but
    # the ranking is left untouched.
    portfolio_timing_enabled = os.getenv("ENABLE_PORTFOLIO_TIMING", "1").lower() not in {"0", "false", "no"}
    if portfolio_timing:
        exposure_multiplier = max(0.0, min(1.0, float(portfolio_timing.get("gross_exposure_multiplier", 1.0) or 0.0)))
        portfolio_regime = portfolio_timing.get("regime")
        portfolio_flags = list(portfolio_timing.get("risk_flags") or [])
        # Account-level R3 gate: when the market regime is defensive, cap the
        # ranking contribution of every single-name candidate consistently.
        if portfolio_timing_enabled:
            sort_score *= (0.35 + 0.65 * exposure_multiplier)
    return {
        "historical_win_rate": round(historical_display_rate, 4),
        "empirical_win_rate": round(empirical_win_rate, 4) if empirical_win_rate is not None else None,
        "proxy_win_rate": round(proxy_win_rate, 4) if proxy_win_rate is not None else None,
        "historical_is_proxy": bool(historical_is_proxy),
        "historical_source": historical_source,
        "unified_probability": round(unified_probability, 4),
        "calibrated_probability": round(calibrated, 4) if calibrated is not None else None,
        "calibrated_ranking_enabled": calibrated_ranking_enabled,
        "probability_for_ranking": round(probability_for_ranking, 4),
        "expected_value": round(expected_value, 4),
        "expected_value_gross": round(expected_value_gross, 4),
        "risk_reward": round(risk_reward, 4),
        "risk_reward_score": round(risk_reward_score, 4),
        "execution_score": round(execution_score, 4),
        "sort_weights": dict(_SORT_WEIGHTS),
        "sort_score": round(sort_score, 4),
        "portfolio_exposure_multiplier": round(exposure_multiplier, 4),
        "portfolio_timing_enabled": portfolio_timing_enabled,
        "portfolio_regime": portfolio_regime,
        "portfolio_risk_flags": portfolio_flags,
    }


def research_context_sort_key(item: Dict[str, Any]) -> tuple[Any, ...]:
    hypothesis = item.get("hypothesis_test") or {}
    evidence = item.get("research_evidence") or {}
    win_sort = evidence.get("win_sort") or {}
    opportunity = item.get("opportunity") or {}
    peer = item.get("peer_earnings") or {}
    pullback_priority = 5
    distance_to_breakout = 9.0
    pullback_sort_enabled = os.getenv("ENABLE_PULLBACK_RANKING_ADJUSTMENT", "1").lower() not in {"0", "false", "no"}
    if pullback_sort_enabled:
        pullback = opportunity.get("pullback_rejection") or {}
        confirmation = opportunity.get("pullback_confirmation") or (item.get("timing") or {}).get("pullback_confirmation") or {}
        confirmation_stage = str(confirmation.get("signal_stage") or "")
        rejection_stage = str(pullback.get("signal_stage") or "")
        priority_map = {
            "STRONG_CONFIRMED": 0,
            "CONFIRMED": 1,
            "WATCH": 2,
            "WAITING_CONFIRMATION": 2,
            "WEAK_CONFIRMED": 3,
            "INVALIDATED": 9,
        }
        pullback_priority = priority_map.get(confirmation_stage, priority_map.get(rejection_stage, 5))
        breakout = float(pullback.get("waiting_breakout_price", 0) or confirmation.get("waiting_breakout_price", 0) or 0)
        spot = float(opportunity.get("spot", 0) or 0)
        distance_to_breakout = abs(breakout - spot) / spot if spot > 0 and breakout > 0 else 9.0
    return (
        -float(win_sort.get("sort_score", 0) or 0),
        -float(win_sort.get("historical_win_rate", 0) or 0),
        -float(evidence.get("probability", 0) or 0),
        -float(win_sort.get("risk_reward_score", 0) or 0),
        -float(win_sort.get("execution_score", 0) or 0),
        pullback_priority,
        distance_to_breakout,
        -int(bool(peer)),
        -int(bool(hypothesis.get("eligible"))),
        -float(peer.get("relay_score", 0) or 0),
        -float(opportunity.get("opportunity_score", 0) or 0),
    )
