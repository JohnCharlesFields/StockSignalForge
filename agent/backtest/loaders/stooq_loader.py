"""Stooq-backed loader for free daily US equity OHLCV data.

Stooq is useful as a no-key fallback when Yahoo/yfinance is rate-limited.
It only provides daily bars through the simple CSV endpoint used here, so
intraday requests deliberately return no data and let the fallback chain move
on to another source.
"""

from __future__ import annotations

from io import StringIO
from typing import Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from backtest.loaders.base import validate_date_range
from backtest.loaders.registry import register

_OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]
_STOOQ_URL = "https://stooq.com/q/d/l/"
_SUPPORTED_INTERVALS = {"1D", "1d", "d", "daily"}


def _to_stooq_symbol(code: str) -> str:
    """Convert project symbols to Stooq symbols.

    Stooq uses lower-case ticker suffixes such as ``aapl.us``.
    Bare alphabetic tickers are treated as US equities for convenience.
    """
    normalized = code.strip().lower()
    if normalized.endswith(".us"):
        return normalized
    if "." not in normalized and normalized.isalpha():
        return f"{normalized}.us"
    return normalized


def _date_for_stooq(value: str) -> str:
    """Return Stooq's compact YYYYMMDD date string."""
    return pd.Timestamp(value).strftime("%Y%m%d")


def _read_stooq_csv(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Fetch one symbol from Stooq's CSV endpoint."""
    query = urlencode(
        {
            "s": symbol,
            "d1": _date_for_stooq(start_date),
            "d2": _date_for_stooq(end_date),
            "i": "d",
        }
    )
    request = Request(
        f"{_STOOQ_URL}?{query}",
        headers={"User-Agent": "Vibe-Trading/0.1 (+https://github.com/HKUDS/Vibe-Trading)"},
    )
    with urlopen(request, timeout=20) as response:
        text = response.read().decode("utf-8", errors="replace")
    return pd.read_csv(StringIO(text))


def _normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize Stooq CSV columns into the project's OHLCV schema."""
    if frame.empty or "No data" in frame.columns:
        return pd.DataFrame(columns=_OHLCV_COLUMNS)

    renamed = frame.rename(
        columns={
            "Date": "trade_date",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    )
    missing = [col for col in ["trade_date", *_OHLCV_COLUMNS] if col not in renamed.columns]
    if missing:
        return pd.DataFrame(columns=_OHLCV_COLUMNS)

    normalized = renamed.loc[:, ["trade_date", *_OHLCV_COLUMNS]].copy()
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"], errors="coerce")
    for column in _OHLCV_COLUMNS:
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    normalized["volume"] = normalized["volume"].fillna(0.0)
    normalized = normalized.dropna(subset=["trade_date", "open", "high", "low", "close"])
    if normalized.empty:
        return pd.DataFrame(columns=_OHLCV_COLUMNS)

    normalized = normalized.set_index("trade_date").sort_index()
    normalized.index.name = "trade_date"
    return normalized.loc[:, _OHLCV_COLUMNS]


@register
class DataLoader:
    """Fetch free daily US equity bars from Stooq."""

    name = "stooq"
    markets = {"us_equity"}
    requires_auth = False

    def is_available(self) -> bool:
        """No API key is required; availability is checked at fetch time."""
        return True

    def fetch(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
        fields: Optional[List[str]] = None,
        interval: str = "1D",
    ) -> Dict[str, pd.DataFrame]:
        """Fetch daily OHLCV history keyed by the original project symbols."""
        del fields
        if not codes:
            return {}
        validate_date_range(start_date, end_date)
        if str(interval or "1D").strip() not in _SUPPORTED_INTERVALS:
            print(f"[WARN] stooq only supports daily bars; got interval={interval!r}")
            return {}

        results: Dict[str, pd.DataFrame] = {}
        for code in codes:
            symbol = _to_stooq_symbol(code)
            try:
                raw = _read_stooq_csv(symbol, start_date, end_date)
                normalized = _normalize_frame(raw)
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                print(f"[WARN] stooq fetch failed for {symbol}: {exc}")
                continue
            except Exception as exc:
                print(f"[WARN] stooq returned unusable data for {symbol}: {exc}")
                continue

            if normalized.empty:
                print(f"[WARN] stooq returned no usable data for {symbol}")
                continue
            results[code] = normalized

        return results
