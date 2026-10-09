from __future__ import annotations

import pandas as pd
import pytest
from pathlib import Path
from uuid import uuid4


def _bars(close: float) -> pd.DataFrame:
    return pd.DataFrame(
        {"Open": [close], "High": [close], "Low": [close], "Close": [close], "Volume": [1000]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-06-01")], name="Date"),
    )


def test_download_daily_history_uses_raw_massive_only_for_missing(monkeypatch) -> None:
    import market_data_service as service

    monkeypatch.setattr(
        service,
        "get_daily_history",
        lambda symbol, period="6mo", allow_yfinance_fallback=False: (
            (_bars(101.0), "twelvedata:incremental") if symbol == "AAPL" else (pd.DataFrame(), "cache:ohlcv")
        ),
    )
    calls = []
    def massive(symbol, start, adjusted=False):
        calls.append((symbol, adjusted))
        return _bars(202.0)
    monkeypatch.setattr(service, "_massive_aggs", massive)
    monkeypatch.setattr(service.yf, "download", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy Yahoo bulk fallback must stay disabled")))
    monkeypatch.setattr(service, "_write_daily_cache", lambda *args, **kwargs: None)

    frame, sources = service.download_daily_history(["AAPL", "MSFT"])

    assert not frame.empty
    assert sources == {"AAPL": "twelvedata:incremental", "MSFT": "massive:bulk-fallback"}
    assert calls == [("MSFT", False)]


def test_external_data_scope_blocks_bulk_yahoo_for_missing(monkeypatch) -> None:
    import market_data_service as service

    monkeypatch.setattr(
        service,
        "get_daily_history",
        lambda symbol, period="6mo", allow_yfinance_fallback=False: (
            (_bars(101.0), "cache:fresh") if symbol == "AAPL" else (pd.DataFrame(), "cache:ohlcv")
        ),
    )
    monkeypatch.setattr(
        service.yf,
        "download",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("page reads must not call yfinance bulk")),
    )

    with service.external_data_scope(False):
        frame, sources = service.download_daily_history(["AAPL", "MSFT"])

    assert not frame.empty
    assert sources == {"AAPL": "cache:fresh"}


def test_external_data_scope_forces_daily_history_cache_only(monkeypatch) -> None:
    import market_data_service as service

    calls = []
    monkeypatch.setattr(service, "_CACHE_ROOT", Path("agent/runs") / f"market_data_test_{uuid4().hex}")
    monkeypatch.setattr(service, "_read_daily_cache", lambda symbol: pd.DataFrame())
    monkeypatch.setattr(service, "_fresh", lambda *args, **kwargs: False)
    monkeypatch.setattr(service, "_massive_aggs", lambda *args, **kwargs: calls.append("massive") or pd.DataFrame())
    monkeypatch.setattr(service, "_tiingo_daily", lambda *args, **kwargs: calls.append("tiingo") or pd.DataFrame())
    monkeypatch.setattr(service, "_twelve_daily", lambda *args, **kwargs: calls.append("twelve") or pd.DataFrame())
    monkeypatch.setattr(
        service.yf,
        "download",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("page reads must not call yfinance")),
    )

    with service.external_data_scope(False):
        frame, source = service.get_daily_history("AAPL", period="6mo", allow_yfinance_fallback=True)

    assert frame.empty
    assert source == "cache:ohlcv"
    assert calls == []


def test_company_fundamentals_calculates_revenue_yoy(monkeypatch) -> None:
    import market_data_service as service

    def fake_get(endpoint: str, symbol: str, **params):
        if endpoint == "profile":
            return [{"marketCap": 123, "price": 45, "industry": "Software", "sector": "Technology"}]
        return [{"revenue": 120}, {"revenue": 115}, {"revenue": 110}, {"revenue": 105}, {"revenue": 100}]

    monkeypatch.setattr(service, "_fmp_get", fake_get)

    result = service.get_company_fundamentals("TEST")

    assert result["market_cap"] == 123
    assert result["revenue_yoy"] == pytest.approx(0.2)


def test_short_fresh_cache_does_not_mask_longer_requested_history(monkeypatch) -> None:
    import market_data_service as service

    calls = []
    tmp_path = Path("agent/runs") / f"market_data_test_{uuid4().hex}"
    monkeypatch.setattr(service, "_CACHE_ROOT", tmp_path)
    monkeypatch.setattr(service, "_read_daily_cache", lambda symbol: _bars(101.0))
    monkeypatch.setattr(service, "_fresh", lambda *args, **kwargs: True)
    monkeypatch.setattr(service, "_write_daily_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "_massive_aggs", lambda symbol, start, adjusted=False: pd.DataFrame())
    monkeypatch.setattr(service, "_tiingo_daily", lambda symbol, start: pd.DataFrame())
    monkeypatch.setattr(service, "_twelve_daily", lambda symbol, start: calls.append(start) or _bars(102.0))

    service.get_daily_history("AAPL", start="2026-01-01", allow_yfinance_fallback=True)

    assert calls
    assert calls[0].date().isoformat() == "2026-01-01"


def test_massive_option_snapshot_preserves_missing_activity_fields(monkeypatch) -> None:
    import market_data_service as service
    # This tests missing activity, not expired-contract rejection.
    expiry = (pd.Timestamp.now().normalize() + pd.Timedelta(days=60)).date().isoformat()

    monkeypatch.setattr(
        service,
        "_massive_get",
        lambda *args, **kwargs: {
            "results": [
                {
                    "details": {
                        "ticker": "O:TEST260717C00100000",
                        "contract_type": "call",
                        "expiration_date": expiry,
                        "strike_price": 100,
                    },
                    "implied_volatility": 0.5,
                    "greeks": {"delta": 0.5},
                    "day": {},
                },
                {
                    "details": {
                        "ticker": "O:TEST260717P00100000",
                        "contract_type": "put",
                        "expiration_date": expiry,
                        "strike_price": 100,
                    },
                    "implied_volatility": 0.6,
                    "greeks": {"delta": -0.5},
                    "day": {},
                },
            ]
        },
    )

    result = service.get_massive_options_snapshot("TEST", current_price=100)

    assert result["available"] is True
    assert result["atm_iv"] == pytest.approx(0.55)
    assert result["call_open_interest"] is None
    assert result["put_open_interest"] is None
    assert result["call_put_oi_ratio"] is None
    assert result["activity_available"] is False


def test_implied_vol_from_mid_round_trips_black_scholes_price() -> None:
    import market_data_service as service

    price = service._bs_option_price(spot=100.0, strike=100.0, dte=30, sigma=0.42, option_type="call")
    iv = service._implied_vol_from_mid(spot=100.0, strike=100.0, dte=30, mid=price, option_type="call")

    assert iv == pytest.approx(0.42, abs=0.002)
