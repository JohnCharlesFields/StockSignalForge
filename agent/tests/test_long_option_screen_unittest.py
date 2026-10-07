from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd

AGENT = Path(__file__).resolve().parents[1]
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

import long_option_screen_service as screen


def daily(last_day: str, count: int = 240) -> pd.DataFrame:
    days = pd.bdate_range(end=last_day, periods=count)
    return pd.DataFrame({"Close": [100.0] * count}, index=days)


class LongOptionScreenTest(unittest.TestCase):
    def setUp(self):
        self.session = date(2026, 9, 23)
        self.signal = {
            "status": "signal", "side": "C", "spot": 100.0,
            "stock_data_as_of": "2026-09-23", "net_consensus": 0.3,
            "signal_score": 0.3, "hv20": 0.4,
        }

    def test_snapshot_read_does_not_fetch_market_data(self):
        path = AGENT / "tests" / "nonexistent_long_option_snapshot_test.json"
        with patch.object(screen, "SNAPSHOT_PATH", path):
            with patch.object(screen, "get_daily_history", side_effect=AssertionError("external read")):
                self.assertFalse(screen.read_snapshot()["available"])

    def test_start_does_not_require_databento_key(self):
        with patch.dict(os.environ, {"DATABENTO_API_KEY": ""}):
            with patch.object(screen.threading.Thread, "start"):
                with patch.object(screen, "_job", {"status": "idle"}):
                    self.assertEqual(screen.start_screen()["status"], "running")

    def test_current_cached_daily_does_not_refetch(self):
        with patch.object(screen, "get_daily_history", return_value=(daily("2026-09-23"), "cache")) as fetch:
            result = screen._daily_for_session("TEST", self.session)
        self.assertEqual(len(result), 240)
        fetch.assert_called_once_with("TEST", period="2y", allow_yfinance_fallback=False)

    def test_stale_cached_daily_tries_existing_refresh(self):
        with patch.object(screen, "get_daily_history", side_effect=[
            (daily("2026-09-22"), "cache"), (daily("2026-09-23"), "massive"),
        ]) as fetch:
            result = screen._daily_for_session("TEST", self.session)
        self.assertEqual(result.index[-1].date(), self.session)
        self.assertEqual(fetch.call_count, 2)
        self.assertTrue(fetch.call_args.kwargs["allow_yfinance_fallback"])

    def test_signal_uses_underlying_and_keeps_spot(self):
        for net, expected_side, expected_status in [
            (0.3, "C", "signal"), (-0.3, "P", "signal"), (0.04, None, "no_direction"),
        ]:
            features = pd.DataFrame([{
                "net_consensus": net, "bull_consensus": 0.6, "bear_consensus": 0.3,
                "hv20": 0.4, "rs20": 0.08,
            }], index=[pd.Timestamp("2026-09-23")])
            with self.subTest(net=net):
                with patch.object(screen, "_daily_for_session", return_value=daily("2026-09-23")):
                    with patch.object(screen, "consensus_feature_frame", return_value=features):
                        result = screen._signal("TEST", daily("2026-09-23"), self.session)
                self.assertEqual(result["status"], expected_status)
                self.assertEqual(result["side"], expected_side)
                self.assertEqual(result["spot"], 100.0)
                self.assertEqual(result["relative_strength_20d"], 0.08)

    def test_missing_consensus_preserves_spot(self):
        features = pd.DataFrame([{"net_consensus": None}], index=[pd.Timestamp("2026-09-23")])
        with patch.object(screen, "_daily_for_session", return_value=daily("2026-09-23")):
            with patch.object(screen, "consensus_feature_frame", return_value=features):
                result = screen._signal("TEST", pd.DataFrame(), self.session)
        self.assertEqual(result["status"], "missing_signal")
        self.assertEqual(result["spot"], 100.0)

    def test_no_direction_and_stale_stock_data(self):
        with patch.object(screen, "_signal", return_value={"status": "no_direction", "side": None}):
            neutral = screen._screen_one("TEST", self.session, pd.DataFrame())
        self.assertEqual(neutral["status"], "no_direction")
        with patch.object(screen, "_signal", return_value={**self.signal, "stock_data_as_of": "2026-09-22"}):
            stale = screen._screen_one("TEST", self.session, pd.DataFrame())
        self.assertEqual(stale["status"], "stale_stock_data")
        self.assertIsNone(stale["side"])
        self.assertEqual(stale["spot"], 100.0)

    def test_screen_snapshot_has_no_option_quotes_or_option_requests(self):
        universe = {"eligible_symbols": ["TEST"], "complete_recent_groups": 0, "group_count": 84}
        with patch.object(screen, "SNAPSHOT_PATH", Path("unused-latest.json")):
            with patch.object(screen, "build_leader_snapshot", return_value=universe):
                with patch.object(screen, "rank_non_seven_stocks", return_value={"leaders": [], "available": False}):
                    with patch.object(screen, "_daily_for_session", return_value=daily("2026-09-23")):
                        with patch.object(screen, "_screen_one", return_value={"symbol": "TEST", **self.signal}):
                            with patch.object(screen, "get_latest_us_market_close_utc", return_value=pd.Timestamp("2026-09-23 21:00", tz="UTC")):
                                with patch.object(Path, "mkdir"), patch.object(Path, "replace"):
                                    with patch.object(Path, "write_text") as write:
                                        with patch("long_option_shadow_service.record_snapshot", return_value={"inserted": 0}), patch("long_option_shadow_service.resolve_pending", return_value={"resolved": 0}):
                                            screen._run_screen()
        self.assertEqual(screen.job_status()["status"], "completed", screen.job_status().get("error"))
        snapshot = json.loads(write.call_args.args[0])
        self.assertEqual(snapshot["source"], "equity:daily_ohlcv")
        self.assertEqual(snapshot["option_data_request_count"], 0)
        self.assertNotIn("contract", snapshot["rows"][0])
        self.assertNotIn("premium_usd", snapshot["rows"][0])

    def test_fibonacci_context_uses_only_supplied_history(self):
        days = pd.bdate_range(end="2026-09-23", periods=60)
        frame = pd.DataFrame({"High": range(101, 161), "Low": range(99, 159),
                              "Close": range(100, 160)}, index=days)
        context = screen._technical_context(frame)
        self.assertEqual(context["range_high"], 160)
        self.assertEqual(context["range_low"], 99)
        self.assertEqual(context["wave_status"], "no_pattern")
        self.assertEqual(context["wave_structure"]["reason"], "no_standard_structure" if context["wave_structure"]["recent_pivots"] else "no_confirmed_pivots")
        self.assertIn("50", context["fib_retracement"])


if __name__ == "__main__":
    unittest.main()
