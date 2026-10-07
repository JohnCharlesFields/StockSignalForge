from __future__ import annotations

import pandas as pd


def _bars(close: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [close],
            "high": [close],
            "low": [close],
            "close": [close],
            "volume": [1000],
        },
        index=pd.DatetimeIndex([pd.Timestamp("2026-05-20")], name="trade_date"),
    )


def test_us_equity_fallback_chain_includes_stooq() -> None:
    from backtest.loaders.registry import FALLBACK_CHAINS, _ensure_registered, LOADER_REGISTRY

    _ensure_registered()

    assert FALLBACK_CHAINS["us_equity"][:4] == ["twelvedata", "yfinance", "stooq", "akshare"]
    assert "twelvedata" in LOADER_REGISTRY
    assert "stooq" in LOADER_REGISTRY


def test_runtime_fallback_fills_only_missing_symbols(monkeypatch) -> None:
    from backtest import runner

    class FakeYfinance:
        name = "yfinance"

        def fetch(self, codes, start_date, end_date, fields=None, interval="1D"):
            return {"AAPL.US": _bars(101.0)}

    class FakeStooq:
        name = "stooq"

        def is_available(self):
            return True

        def fetch(self, codes, start_date, end_date, fields=None, interval="1D"):
            assert codes == ["MSFT.US"]
            return {"MSFT.US": _bars(202.0)}

    monkeypatch.setattr(runner, "_get_loader", lambda source: FakeYfinance)
    monkeypatch.setitem(runner.LOADER_REGISTRY, "stooq", FakeStooq)
    monkeypatch.setitem(runner.FALLBACK_CHAINS, "us_equity", ["yfinance", "stooq"])

    data_map, used_sources = runner._fetch_with_runtime_fallback(
        primary_source="yfinance",
        codes=["AAPL.US", "MSFT.US"],
        config={"start_date": "2026-05-20", "end_date": "2026-05-21"},
        interval="1D",
        market="us_equity",
    )

    assert sorted(data_map) == ["AAPL.US", "MSFT.US"]
    assert used_sources == ["yfinance", "stooq"]
