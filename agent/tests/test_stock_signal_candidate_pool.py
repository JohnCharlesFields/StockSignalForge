from stock_signal_candidate_service import stock_signal_candidate_pool


def _row(ticker: str, trend: float, strategy: str = "bear_call_spread", score: float = 5.0) -> dict:
    return {
        "ticker": ticker,
        "spot": 10.0,
        "trend_30d": trend,
        "research_score": score,
        "final_score": score,
        "stress_score": 4.0,
        "confidence_score": 4.0,
        "liquidity_score": 0.2,
        "primary_strategy": strategy,
        "risk_flags": [],
        "rejection_reasons": [],
        "recent_closes": [10.0, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3],
    }


def test_candidate_pool_keeps_backup_rows_when_strict_filter_would_be_empty() -> None:
    rows = [_row(f"T{i}", trend=-0.05 - i * 0.01) for i in range(12)]

    candidates = stock_signal_candidate_pool(rows, {}, "run_x", limit=10)

    assert len(candidates) == 10
    assert all(item["candidate_type"] == "backup" for item in candidates)
    assert all(item["signal"] == "备选观察" for item in candidates)
    assert any("30日趋势未转正" in flag for flag in candidates[0]["risk_flags"])


def test_candidate_pool_ranks_strict_candidates_before_backups() -> None:
    rows = [
        _row("WEAK", trend=-0.12, strategy="bear_call_spread", score=8.0),
        _row("GOOD", trend=0.18, strategy="bull_call_spread", score=8.0),
    ]

    candidates = stock_signal_candidate_pool(rows, {}, "run_x", limit=8)

    assert candidates[0]["ticker"] == "GOOD"
    assert candidates[0]["candidate_type"] == "strict"
    assert any(item["ticker"] == "WEAK" and item["candidate_type"] == "backup" for item in candidates)


def test_candidate_pool_keeps_equity_proxy_when_option_legs_are_missing() -> None:
    rows = [
        {
            **_row("NOPTS", trend=0.22, strategy="bull_put_spread", score=0.0),
            "tradable": False,
            "rejection_reasons": ["could not construct real tradable legs"],
        }
    ]

    candidates = stock_signal_candidate_pool(rows, {}, "run_x", limit=5)

    assert len(candidates) == 1
    assert candidates[0]["ticker"] == "NOPTS"
    assert candidates[0]["research_score"] > 0
    assert candidates[0]["research_score_is_proxy"] is True
    assert candidates[0]["option_evidence"]["available"] is False
