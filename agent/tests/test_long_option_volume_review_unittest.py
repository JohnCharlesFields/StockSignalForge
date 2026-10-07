from __future__ import annotations

import json
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

AGENT = Path(__file__).resolve().parents[1]
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

import long_option_review_service as review
import option_volume_leaders_service as volume
from scripts.validate_long_option_alpha_panic import _cluster_delta, _split


class OptionVolumeLeadersTest(unittest.TestCase):
    def test_complete_year_and_stock_type_required(self):
        raw = ("Trade Month,Options Class,Underlying,Product Type,Exchange,Volume\n"
               "2026/08,SPY,SPY,S,CBOE,10000\n"
               "2026/08,NVDA,NVDA,S,CBOE,9000\n"
               "2026/08,INTC,INTC,S,CBOE,8000\n"
               "2026/08,MU,MU,S,BATS,7000\n"
               "2026/08,SPX,SPX,I,CBOE,100000\n")
        with patch.object(volume, "_monthly_report", return_value=raw) as fetch:
            with patch.object(volume, "_listing_directory", return_value={
                "SPY": {"etf": "Y", "name": "SPDR ETF"},
                "INTC": {"etf": "N", "name": "Intel Common Stock"},
                "MU": {"etf": "N", "name": "Micron Common Stock"},
            }):
                result = volume.rank_non_seven_stocks(date(2026, 9, 24), limit=10)
        self.assertEqual(fetch.call_count, 12)
        self.assertEqual([row["symbol"] for row in result["leaders"]], ["INTC", "MU"])
        self.assertEqual(result["leaders"][0]["contracts_12m"], 96000)
        self.assertEqual(result["window_start"], "2025-09")
        self.assertEqual(result["window_end"], "2026-08")

    def test_missing_month_does_not_publish_partial_ranking(self):
        with patch.object(volume, "_monthly_report", side_effect=ValueError("missing")):
            result = volume.rank_non_seven_stocks(date(2026, 9, 24))
        self.assertFalse(result["available"])
        self.assertEqual(result["leaders"], [])


class LongOptionReviewTest(unittest.TestCase):
    def setUp(self):
        self.snapshot = {"available": True, "stale": False, "snapshot": {"rows": [
            {"symbol": "AAPL", "status": "signal", "side": "C", "spot": 100,
             "stock_data_as_of": "2026-09-23", "net_consensus": 0.3, "technical_context": {"breakout_price": 103}},
        ]}}

    def test_no_direction_never_calls_model(self):
        self.snapshot["snapshot"]["rows"][0]["status"] = "no_direction"
        with patch.object(review, "read_snapshot", return_value=self.snapshot):
            with patch("src.providers.chat.ChatLLM") as model:
                result = review.review_symbol("AAPL")
        self.assertFalse(result["available"])
        model.assert_not_called()

    def test_opposite_model_view_is_watch_not_put(self):
        response = MagicMock(content=json.dumps({"model_view": "PUT", "summary": "证据相互矛盾",
                                                "supporting_points": [], "objections": ["趋势不稳"]}))
        with patch.object(review, "read_snapshot", return_value=self.snapshot):
            with patch.object(Path, "exists", return_value=False), patch.object(Path, "mkdir"):
                with patch.object(Path, "write_text"), patch.object(Path, "replace"):
                    with patch("src.providers.chat.ChatLLM") as model:
                        model.return_value.chat.return_value = response
                        result = review.review_symbol("AAPL")
        self.assertEqual(result["model_view"], "PUT")
        self.assertEqual(result["final_view"], "WAIT")
        self.assertTrue(result["conflict"])
        model.return_value.chat.assert_called_once()

    def test_model_cannot_invent_wave_when_detector_found_none(self):
        self.snapshot["snapshot"]["rows"][0]["technical_context"]["wave_structure"] = {"status": "no_pattern"}
        response = MagicMock(content=json.dumps({"model_view": "CALL", "wave_note": "当前已经进入第三浪"}))
        with patch.object(review, "read_snapshot", return_value=self.snapshot):
            with patch.object(Path, "exists", return_value=False), patch.object(Path, "mkdir"):
                with patch.object(Path, "write_text"), patch.object(Path, "replace"):
                    with patch("src.providers.chat.ChatLLM") as model:
                        model.return_value.chat.return_value = response
                        result = review.review_symbol("AAPL")
        self.assertIn("未识别", result["wave_note"])
        self.assertNotIn("第三浪", result["wave_note"])

    def test_time_splits_leave_embargo(self):
        self.assertEqual(_split(date(2023, 12, 15)), "train")
        self.assertIsNone(_split(date(2023, 12, 20)))
        self.assertEqual(_split(date(2024, 1, 2)), "validation")
        self.assertIsNone(_split(date(2024, 12, 20)))
        self.assertEqual(_split(date(2025, 1, 2)), "test")

    def test_one_panic_event_has_no_confidence_interval(self):
        rows = [{"symbol": "AAPL", "signal_date": "2025-01-03", "panic_high": True, "ret_5": -0.08},
                {"symbol": "NVDA", "signal_date": "2025-02-03", "panic_high": False, "ret_5": 0.01}]
        result = _cluster_delta(rows, "panic_high", 5, n_boot=100)
        self.assertEqual(result["n"], 1)
        self.assertIsNone(result["ci95_delta"])


if __name__ == "__main__":
    unittest.main()
