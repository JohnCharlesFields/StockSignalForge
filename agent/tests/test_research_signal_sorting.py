from research_signal_sorting import research_context_sort_key, win_rate_sort_metrics


def _item(symbol: str, evidence: dict, hypothesis: dict | None = None) -> dict:
    return {
        "symbol": symbol,
        "research_evidence": evidence,
        "hypothesis_test": hypothesis or {},
        "opportunity": {},
        "peer_earnings": {},
    }


def test_win_sort_prefers_composite_sort_score_before_subscores() -> None:
    high_history = _item(
        "HIST",
        {"probability": 0.58, "win_sort": {"historical_win_rate": 0.78, "risk_reward_score": 0.2, "execution_score": 0.2, "sort_score": 0.6}},
    )
    high_probability = _item(
        "PROB",
        {"probability": 0.72, "win_sort": {"historical_win_rate": 0.55, "risk_reward_score": 1.0, "execution_score": 1.0, "sort_score": 0.7}},
    )

    assert sorted([high_probability, high_history], key=research_context_sort_key)[0]["symbol"] == "PROB"


def test_win_sort_metrics_keeps_peer_proxy_separate_from_empirical_history() -> None:
    metrics = win_rate_sort_metrics(
        opportunity=None,
        peer_earnings={"research_probability": 0.8, "peer_similarity": 0.75, "expected_catch_up_return": 0.12},
        hypothesis_test={"invalidation_pct": 0.06, "eligible": True},
        unified_probability=0.68,
    )

    assert metrics["historical_source"] == "同行接力历史代理"
    assert metrics["historical_is_proxy"] is True
    assert metrics["empirical_win_rate"] is None
    assert metrics["proxy_win_rate"] == 0.725
    assert metrics["historical_win_rate"] == 0.725
    assert metrics["risk_reward"] == 2.0
    assert metrics["execution_score"] >= 0.35


def test_win_sort_metrics_does_not_turn_unified_probability_into_history() -> None:
    metrics = win_rate_sort_metrics(
        opportunity=None,
        peer_earnings=None,
        hypothesis_test={},
        unified_probability=0.82,
    )

    assert metrics["historical_source"] == "缺少历史验证"
    assert metrics["empirical_win_rate"] is None
    assert metrics["proxy_win_rate"] is None
    assert metrics["historical_is_proxy"] is True
    assert metrics["historical_win_rate"] == 0.5
    assert metrics["unified_probability"] == 0.82


def test_pullback_priority_layers_before_same_score_items() -> None:
    confirmed = _item(
        "CONF",
        {"probability": 0.55, "win_sort": {"sort_score": 0.5, "historical_win_rate": 0.6, "risk_reward_score": 0.2, "execution_score": 0.2}},
    )
    confirmed["opportunity"] = {
        "spot": 10,
        "pullback_confirmation": {"signal_stage": "CONFIRMED", "waiting_breakout_price": 10.2},
    }
    watch = _item(
        "WATCH",
        {"probability": 0.55, "win_sort": {"sort_score": 0.5, "historical_win_rate": 0.6, "risk_reward_score": 0.2, "execution_score": 0.2}},
    )
    watch["opportunity"] = {
        "spot": 10,
        "pullback_rejection": {"signal_stage": "WATCH", "waiting_breakout_price": 10.05},
    }
    invalid = _item(
        "BAD",
        {"probability": 0.55, "win_sort": {"sort_score": 0.5, "historical_win_rate": 0.6, "risk_reward_score": 0.2, "execution_score": 0.2}},
    )
    invalid["opportunity"] = {
        "spot": 10,
        "pullback_confirmation": {"signal_stage": "INVALIDATED", "waiting_breakout_price": 10.01},
    }

    ordered = [item["symbol"] for item in sorted([invalid, watch, confirmed], key=research_context_sort_key)]
    assert ordered == ["CONF", "WATCH", "BAD"]


def test_pullback_ranking_adjustment_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_PULLBACK_RANKING_ADJUSTMENT", "0")
    confirmed = _item(
        "LOW",
        {"probability": 0.55, "win_sort": {"historical_win_rate": 0.55, "risk_reward_score": 0.2, "execution_score": 0.2}},
    )
    confirmed["opportunity"] = {
        "spot": 10,
        "pullback_confirmation": {"signal_stage": "CONFIRMED", "waiting_breakout_price": 10.2},
    }
    stronger_legacy_score = _item(
        "HIGH",
        {"probability": 0.9, "win_sort": {"historical_win_rate": 0.9, "risk_reward_score": 0.9, "execution_score": 0.9}},
    )

    ordered = [item["symbol"] for item in sorted([confirmed, stronger_legacy_score], key=research_context_sort_key)]
    assert ordered == ["HIGH", "LOW"]


def test_portfolio_timing_gate_can_be_disabled(monkeypatch) -> None:
    timing = {"gross_exposure_multiplier": 0.25, "regime": "risk_off", "risk_flags": ["index_below_ma200"]}
    base_kwargs = dict(opportunity=None, peer_earnings=None, hypothesis_test={}, unified_probability=0.6)

    monkeypatch.setenv("ENABLE_PORTFOLIO_TIMING", "1")
    on = win_rate_sort_metrics(**base_kwargs, portfolio_timing=timing)
    monkeypatch.setenv("ENABLE_PORTFOLIO_TIMING", "0")
    off = win_rate_sort_metrics(**base_kwargs, portfolio_timing=timing)

    # When disabled the defensive regime must NOT dampen the sort score,
    # but the regime/multiplier are still surfaced for display.
    assert off["sort_score"] > on["sort_score"]
    assert off["portfolio_timing_enabled"] is False
    assert off["portfolio_exposure_multiplier"] == 0.25
    assert on["portfolio_timing_enabled"] is True
