"""Twelve Data free-tier loader for cached US-equity daily OHLCV."""

from __future__ import annotations

import os
from typing import Dict, List, Optional

import pandas as pd

from backtest.loaders.base import validate_date_range
from backtest.loaders.registry import register
from market_data_service import get_daily_history


def _symbol(code: str) -> str:
    upper = str(code).strip().upper()
    return upper[:-3] if upper.endswith(".US") else upper


@register
class DataLoader:
    """Fetch US daily bars through the shared Twelve Data cache."""

    name = "twelvedata"
    markets = {"us_equity"}
    requires_auth = True

    def is_available(self) -> bool:
        return bool(os.environ.get("TWELVE_DATA_API_KEY", "").strip())

    def fetch(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
        fields: Optional[List[str]] = None,
        interval: str = "1D",
    ) -> Dict[str, pd.DataFrame]:
        del fields
        validate_date_range(start_date, end_date)
        if str(interval or "1D").upper() != "1D":
            return {}
        output: Dict[str, pd.DataFrame] = {}
        for code in codes:
            frame, _source = get_daily_history(
                _symbol(code),
                start=start_date,
                end=end_date,
                allow_yfinance_fallback=False,
            )
            if frame.empty:
                continue
            normalized = frame.rename(columns={
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            })
            normalized.index.name = "trade_date"
            output[code] = normalized[["open", "high", "low", "close", "volume"]]
        return output
