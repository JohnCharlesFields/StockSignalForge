from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_screener():
    root = Path(__file__).resolve().parents[1]
    path = root / "scripts" / "screening_framework_v2_optimized.py"
    spec = importlib.util.spec_from_file_location("screening_framework_v2_optimized_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _NoYFinanceStock:
    @property
    def options(self):
        raise AssertionError("yfinance options should not be used when CBOE expiries are available")

    def option_chain(self, expiry):
        raise AssertionError("yfinance option_chain should not be used when CBOE chain is available")


def test_cached_options_and_chain_prefer_cboe(monkeypatch):
    screener = _load_screener()
    monkeypatch.setitem(screener.CONFIG, "cache_enabled", False)
    monkeypatch.setitem(screener.CONFIG, "cboe_option_chain_enabled", True)

    def fake_chain(ticker, expiry=None, ttl_seconds=None):
        return {
            "available": True,
            "expiries": ["2026-06-26"],
            "calls": [
                {
                    "contractSymbol": "AAPL260626C00200000",
                    "strike": 200.0,
                    "bid": 5.1,
                    "ask": 5.4,
                    "volume": 120,
                    "openInterest": 2400,
                    "impliedVolatility": 0.31,
                }
            ],
            "puts": [
                {
                    "contractSymbol": "AAPL260626P00195000",
                    "strike": 195.0,
                    "bid": 4.0,
                    "ask": 4.3,
                    "volume": 90,
                    "openInterest": 1800,
                    "impliedVolatility": 0.29,
                }
            ],
        }

    monkeypatch.setattr(screener, "get_cboe_option_chain", fake_chain)

    expiries, expiry_source = screener.cached_options(_NoYFinanceStock(), "AAPL")
    calls, puts, chain_source = screener.cached_option_chain(_NoYFinanceStock(), "AAPL", "2026-06-26")

    assert expiries == ("2026-06-26",)
    assert expiry_source == "live:cboe_options"
    assert chain_source == "live:cboe_option_chain"
    assert float(calls.iloc[0]["strike"]) == 200.0
    assert float(puts.iloc[0]["strike"]) == 195.0


def test_option_metrics_keep_net_of_commission_ev():
    screener = _load_screener()
    result = {}

    screener.credit_metrics(result, credit=1.0, width=5.0, breakeven=99.0, pop=0.70, trade_cost=2.6)
    assert result["expected_value_gross"] == 70.0 - 120.0
    assert result["expected_value"] == result["expected_value_gross"] - result["trade_cost"]
    assert result["trade_cost"] == 2.6
    assert result["roc"] == round((100.0 - 2.6) / 400.0, 4)

    screener.debit_metrics(result, debit=2.0, width=5.0, breakeven=102.0, pop=0.45, trade_cost=2.6)
    assert result["expected_value_gross"] == round(0.45 * 300.0 - 0.55 * 200.0, 2)
    assert result["expected_value"] == result["expected_value_gross"] - result["trade_cost"]
    assert result["trade_cost"] == 2.6
