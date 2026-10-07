from overnight_alpha_service import benchmark_for_universes, validate_overnight_alpha_summary


def test_benchmark_prefers_more_specific_universe() -> None:
    assert benchmark_for_universes(["spx", "rut"]) == ("IWM", "rut")
    assert benchmark_for_universes(["ndx", "sox"]) == ("SOXX", "sox")
    assert benchmark_for_universes([]) == ("SPY", "default")


def test_summary_handles_missing_symbol_without_fake_win_rate() -> None:
    payload = validate_overnight_alpha_summary([{"symbol": ""}], period="1y", limit=1)
    assert payload["available"] is False
    assert payload["portfolio"]["sample_count"] == 0
    assert payload["portfolio"]["alpha_win_rate"] is None
