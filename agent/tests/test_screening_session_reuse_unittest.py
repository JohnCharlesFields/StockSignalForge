from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from scripts import screening_framework_v2_optimized as screening


class ScreeningSessionReuseTests(unittest.TestCase):
    def test_result_reuse_requires_same_session_and_close(self) -> None:
        path = Path("AAPL.json")
        result = {"ticker": "AAPL", "spot": 100.0, "price_as_of": "2026-09-23"}
        with (
            patch.object(screening, "_screen_result_cache_path", return_value=path),
            patch.object(screening, "_current_cached_close", return_value=("2026-09-23", 100.0)),
            patch.object(Path, "write_text") as write,
            patch.object(Path, "replace"),
        ):
            screening._write_session_screen_result("AAPL", result)
        contents = write.call_args.args[0]
        with (
            patch.object(screening, "_screen_result_cache_path", return_value=path),
            patch.object(screening, "_current_cached_close", return_value=("2026-09-23", 100.0)),
            patch.object(Path, "read_text", return_value=contents),
        ):
            self.assertEqual(screening._read_session_screen_result("AAPL"), result)
        with (
            patch.object(screening, "_screen_result_cache_path", return_value=path),
            patch.object(screening, "_current_cached_close", return_value=("2026-09-24", 102.0)),
            patch.object(Path, "read_text", return_value=contents),
        ):
            self.assertIsNone(screening._read_session_screen_result("AAPL"))

    def test_recent_ohlcv_uses_real_dates_not_row_numbers(self) -> None:
        days = pd.date_range("2026-08-03", periods=35, freq="B")
        history = pd.DataFrame({
            "Date": days, "Open": range(35), "High": range(1, 36),
            "Low": range(35), "Close": range(1, 36), "Volume": [100] * 35,
        })
        chain = pd.DataFrame({
            "strike": [30], "bid": [1.0], "ask": [1.2], "lastPrice": [1.1],
            "impliedVolatility": [0.3], "openInterest": [100], "volume": [10],
        })
        with (
            patch.object(screening, "cached_history", return_value=(history, "cache")),
            patch.object(screening, "cached_options", return_value=(("2026-11-20",), "cache")),
            patch.object(screening, "cached_option_chain", return_value=(chain.copy(), chain.copy(), "cache")),
            patch.object(screening, "compute_atm_iv", return_value=0.3),
            patch.object(screening, "gamma_exposure_summary", return_value={}),
            patch.object(screening, "cached_fundamentals", return_value=({}, "cache")),
            patch.object(screening, "detect_pullback_setup", return_value=({}, {})),
        ):
            data = screening.fetch_ticker("AAPL", "2026-11-20")
        self.assertEqual(data["price_as_of"], days[-1].date().isoformat())
        self.assertEqual(data["recent_ohlcv"][-1]["Date"], days[-1].date().isoformat())


if __name__ == "__main__":
    unittest.main()
