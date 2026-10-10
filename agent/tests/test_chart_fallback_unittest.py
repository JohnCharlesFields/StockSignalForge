from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import requests

import api_server
import market_data_service


def _rate_limit_error() -> requests.HTTPError:
    error = requests.HTTPError("429 for url with apiKey=SECRET")
    error.response = SimpleNamespace(status_code=429)
    return error


def _daily_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {"Open": [100.0, 101.0], "High": [103.0, 104.0], "Low": [99.0, 100.0],
         "Close": [102.0, 103.0], "Volume": [1000, 1200]},
        index=pd.to_datetime(["2026-09-21", "2026-09-22"]),
    )


class ChartFallbackTests(unittest.TestCase):
    def test_daily_chart_uses_existing_history_without_leaking_key(self) -> None:
        with (
            patch.object(api_server, "cache_get", return_value=None),
            patch.object(api_server, "cache_set"),
            patch.object(market_data_service, "_massive_get", side_effect=_rate_limit_error()),
            patch.object(api_server, "get_daily_history", side_effect=[(pd.DataFrame(), "cache:empty"), (_daily_frame(), "twelvedata:incremental")]) as history,
        ):
            result = api_server._single_candles("MRK", "daily")

        self.assertTrue(result["available"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["as_of_date"], "2026-09-22")
        self.assertEqual(result["source"], "twelvedata:incremental")
        self.assertEqual(result["reason"], "massive_http_429")
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertTrue(any(call.kwargs.get("skip_massive") for call in history.call_args_list))

    def test_daily_existing_cache_does_not_call_massive(self) -> None:
        with (
            patch.object(api_server, "cache_get", return_value=None),
            patch.object(api_server, "cache_set"),
            patch.object(market_data_service, "_massive_get", side_effect=AssertionError("cache-first cannot fetch")) as provider,
            patch.object(api_server, "get_daily_history", return_value=(_daily_frame(), "cache:ohlcv")),
        ):
            result = api_server._single_candles("MRK", "daily")
        self.assertTrue(result["available"])
        self.assertIsNone(result["reason"])
        provider.assert_not_called()

    def test_intraday_failure_is_sanitized_and_not_misrepresented_as_daily(self) -> None:
        with (
            patch.object(api_server, "cache_get", return_value=None),
            patch.object(api_server, "cache_set"),
            patch.object(market_data_service, "_massive_get", side_effect=_rate_limit_error()),
            patch.object(api_server, "get_daily_history", side_effect=AssertionError("daily fallback must not run")),
        ):
            result = api_server._single_candles("MRK", "1h")

        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "massive_http_429")
        self.assertIn("仅接 Massive", result["note"])
        self.assertNotIn("SECRET", json.dumps(result))

    def test_daily_reader_can_skip_massive_when_chart_falls_back(self) -> None:
        with (
            patch.object(market_data_service, "_read_daily_cache", return_value=pd.DataFrame()),
            patch.object(market_data_service, "_massive_aggs", side_effect=AssertionError("Massive must be skipped")),
            patch.object(market_data_service, "_tiingo_daily", return_value=pd.DataFrame()),
            patch.object(market_data_service, "_twelve_daily", return_value=_daily_frame()),
            patch.object(market_data_service, "_write_daily_cache"),
        ):
            frame, source = market_data_service.get_daily_history("MRK", period="1y", skip_massive=True)

        self.assertEqual(len(frame), 2)
        self.assertEqual(source, "twelvedata:incremental")


if __name__ == "__main__":
    unittest.main()
