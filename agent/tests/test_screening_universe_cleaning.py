from scripts.screening_framework_v2_optimized import is_equity_holding_ticker


def test_provider_synthetic_holdings_do_not_enter_equity_universe() -> None:
    assert is_equity_holding_ticker("NVDA")
    assert is_equity_holding_ticker("BRK-B")
    assert not is_equity_holding_ticker("WFFUT")
    assert not is_equity_holding_ticker("XTSLA")
    assert not is_equity_holding_ticker("USD")
