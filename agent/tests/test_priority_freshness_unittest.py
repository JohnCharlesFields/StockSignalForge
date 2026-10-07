from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

import market_calendar
import prediction_ledger_service
import priority_board_service


class PriorityFreshnessTests(unittest.TestCase):
    def test_persisted_slice_reports_its_actual_date(self) -> None:
        old_row = {
            "as_of_date": "2026-08-05",
            "created_at": "2026-08-06T01:20:00+00:00",
            "symbol": "NVDA",
            "current_price": 100.0,
            "calibrated_prob": 0.55,
            "horizon_days": 5,
            "payload": {"earnings": {"available": True}},
        }
        with (
            patch.object(market_calendar, "most_recent_session", return_value=date(2026, 9, 22)),
            patch.object(priority_board_service, "priority_candidate_slices_for_date", return_value=[old_row]),
            patch.object(priority_board_service, "_attach_sector_diffusion_labels"),
            patch.object(priority_board_service, "_calibration_provenance", return_value={}),
        ):
            board = priority_board_service._board_from_latest_slices(limit=1)

        self.assertEqual(board["generated_at"], old_row["created_at"])
        self.assertEqual(board["data_as_of"], "2026-08-05")
        self.assertTrue(board["data_stale"])

    def test_forward_ledger_uses_completed_market_session(self) -> None:
        prediction_rows = []
        slice_dates = []
        board = {"picks": [{"symbol": "NVDA", "current_price": 100.0, "calibrated_probability": 0.55}], "horizon_days": 5}

        def log_many(rows):
            prediction_rows.extend(rows)
            return len(rows)

        def log_slices(**kwargs):
            slice_dates.append(kwargs["as_of_date"])
            return len(kwargs["rows"])

        with (
            patch.object(market_calendar, "most_recent_session", return_value=date(2026, 9, 22)),
            patch.object(prediction_ledger_service, "cache_get", return_value=None),
            patch.object(prediction_ledger_service, "_fallback_board_from_latest_snapshot", return_value=board),
            patch.object(prediction_ledger_service, "predictions_log_many", side_effect=log_many),
            patch.object(prediction_ledger_service, "priority_candidate_slices_log", side_effect=log_slices),
            patch.object(prediction_ledger_service, "_bust_scorecard_cache"),
        ):
            result = prediction_ledger_service.log_predictions(top_n=1)

        self.assertEqual(result["as_of_date"], "2026-09-22")
        self.assertEqual(prediction_rows[0]["as_of_date"], "2026-09-22")
        self.assertEqual(slice_dates, ["2026-09-22"])


if __name__ == "__main__":
    unittest.main()
