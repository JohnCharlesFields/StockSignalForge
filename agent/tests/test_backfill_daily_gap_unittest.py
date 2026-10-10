from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import market_data_service as market
from scripts import backfill_daily_gap as job


class _FakeRange:
    def __init__(self, frame: pd.DataFrame):
        self.frame = frame

    def to_df(self) -> pd.DataFrame:
        return self.frame


class _FakeHistorical:
    def __init__(self, frame: pd.DataFrame, cost: float = 0.01):
        self.metadata = self
        self.timeseries = self
        self.frame = frame
        self.cost = cost
        self.downloads = 0

    def get_cost(self, **_kwargs) -> float:
        return self.cost

    def get_dataset_range(self, **_kwargs) -> dict:
        return {"schema": {"ohlcv-1d": {"end": "2026-10-10T00:00:00Z"}}}

    def get_range(self, **_kwargs) -> _FakeRange:
        self.downloads += 1
        return _FakeRange(self.frame)


class DailyGapBackfillTests(unittest.TestCase):
    def test_class_share_alias_uses_provider_dot(self) -> None:
        self.assertEqual(job._provider_symbol("BF-B"), "BF.B")
        self.assertEqual(job._provider_symbol("NVDA"), "NVDA")

    def test_only_missing_dates_are_added_and_rerun_is_idempotent(self) -> None:
        dates = pd.to_datetime(["2026-08-04", "2026-08-05", "2026-08-06"], utc=True)
        source = pd.DataFrame({
            "symbol": ["NVDA"] * 3,
            "open": [100.0, 101.0, 102.0],
            "high": [102.0, 103.0, 104.0],
            "low": [99.0, 100.0, 101.0],
            "close": [101.0, 102.0, 103.0],
            "volume": [1000, 1200, 1300],
        }, index=dates)
        existing = pd.DataFrame({
            "Open": [100.0, 102.0], "High": [102.0, 104.0],
            "Low": [99.0, 101.0], "Close": [101.5, 103.0],
            "Volume": [1000, 1300],
        }, index=pd.to_datetime(["2026-08-04", "2026-08-06"]))
        client = _FakeHistorical(source)
        with tempfile.TemporaryDirectory() as temp, patch.object(market, "_CACHE_ROOT", Path(temp)), patch("databento.Historical", return_value=client):
            market._write_daily_cache("NVDA", existing)
            first = job.backfill("2026-08-04", "2026-08-07", execute=True, max_cost_usd=0.25)
            result = market._read_daily_cache("NVDA")
            self.assertEqual(first["records_added"], 1)
            self.assertEqual(len(result), 3)
            self.assertEqual(result.loc[pd.Timestamp("2026-08-04"), "Close"], 101.5)
            self.assertEqual(result.loc[pd.Timestamp("2026-08-05"), "Close"], 102.0)
            second = job.backfill("2026-08-04", "2026-08-07", execute=True, max_cost_usd=0.25)
            self.assertEqual(second["records_added"], 0)

    def test_cost_cap_refuses_download(self) -> None:
        source = pd.DataFrame()
        client = _FakeHistorical(source, cost=2.0)
        with tempfile.TemporaryDirectory() as temp, patch.object(market, "_CACHE_ROOT", Path(temp)), patch("databento.Historical", return_value=client):
            market._write_daily_cache("NVDA", pd.DataFrame({
                "Open": [100.0], "High": [101.0], "Low": [99.0],
                "Close": [100.0], "Volume": [1000],
            }, index=pd.to_datetime(["2026-08-04"])))
            with self.assertRaisesRegex(RuntimeError, "exceeds cap"):
                job.backfill("2026-08-04", "2026-08-07", execute=True, max_cost_usd=0.25)
            self.assertEqual(client.downloads, 0)


if __name__ == "__main__":
    unittest.main()
