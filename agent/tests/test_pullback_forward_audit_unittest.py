import unittest
from datetime import date, datetime

from scripts.audit_pullback_forward_strategy import ET, next_open, shift_session, summary


class ForwardAuditTests(unittest.TestCase):
    def test_after_close_uses_next_session_open(self):
        recorded = datetime(2026, 9, 23, 18, 0, tzinfo=ET)
        self.assertEqual(next_open(recorded, date(2026, 9, 23)), date(2026, 9, 24))

    def test_intraday_record_does_not_get_previous_open(self):
        recorded = datetime(2026, 9, 24, 12, 0, tzinfo=ET)
        self.assertEqual(next_open(recorded, date(2026, 9, 24)), date(2026, 9, 25))

    def test_holiday_and_weekend_not_holding_sessions(self):
        self.assertEqual(shift_session(date(2026, 7, 2), 1), date(2026, 7, 6))

    def test_overlapping_five_day_holds_with_two_dates_have_no_ci(self):
        rows = [{"day": day, "symbol": str(n), "net": .01} for day in ("2026-09-22", "2026-09-23") for n in range(100)]
        result = summary(rows, block_length=5)
        self.assertIsNone(result["ci95"])
        self.assertEqual(result["inference"], "insufficient_date_blocks")
        self.assertEqual(result["n"], 200)

    def test_negative_ci_does_not_become_zero_crossing(self):
        rows = [{"day": str(day), "symbol": "A", "net": -.02} for day in range(20)]
        result = summary(rows, block_length=5)
        self.assertLess(result["ci95"][1], 0)


if __name__ == '__main__':
    unittest.main()
