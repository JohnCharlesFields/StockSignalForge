from __future__ import annotations

import pandas as pd


def test_stooq_symbol_defaults_to_us_suffix() -> None:
    from backtest.loaders.stooq_loader import _to_stooq_symbol

    assert _to_stooq_symbol("AAPL") == "aapl.us"
    assert _to_stooq_symbol("MSFT.US") == "msft.us"


def test_stooq_loader_normalizes_daily_csv(monkeypatch) -> None:
    from backtest.loaders import stooq_loader

    raw = pd.DataFrame(
        {
            "Date": ["2026-05-20", "2026-05-21"],
            "Open": [100.0, 101.0],
            "High": [102.0, 103.0],
            "Low": [99.0, 100.0],
            "Close": [101.0, 102.0],
            "Volume": [1_000_000, 1_100_000],
        }
    )
    monkeypatch.setattr(stooq_loader, "_read_stooq_csv", lambda *args: raw)

    result = stooq_loader.DataLoader().fetch(["AAPL.US"], "2026-05-20", "2026-05-21")

    assert list(result) == ["AAPL.US"]
    frame = result["AAPL.US"]
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert frame.index.name == "trade_date"
    assert frame.loc[pd.Timestamp("2026-05-21"), "close"] == 102.0


def test_stooq_rejects_intraday_interval(monkeypatch) -> None:
    from backtest.loaders import stooq_loader

    called = False

    def fake_read(*args):
        nonlocal called
        called = True
        return pd.DataFrame()

    monkeypatch.setattr(stooq_loader, "_read_stooq_csv", fake_read)

    assert stooq_loader.DataLoader().fetch(["AAPL.US"], "2026-05-20", "2026-05-21", interval="1H") == {}
    assert called is False
