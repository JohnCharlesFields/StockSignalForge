from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

AGENT = Path(__file__).resolve().parents[1]
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

from wave_structure_service import detect_wave_structure


def price_path(anchors: list[tuple[int, float]], end: int | None = None) -> pd.DataFrame:
    length = end if end is not None else anchors[-1][0] + 1
    days = pd.bdate_range("2026-01-02", periods=length)
    prices = []
    for index in range(length):
        for left, right in zip(anchors, anchors[1:]):
            if left[0] <= index <= right[0]:
                fraction = (index - left[0]) / (right[0] - left[0])
                prices.append(left[1] + fraction * (right[1] - left[1]))
                break
    return pd.DataFrame({"High": [value + 0.2 for value in prices],
                         "Low": [value - 0.2 for value in prices],
                         "Close": prices}, index=days)


IMPULSE = [(0, 105), (3, 100), (10, 120), (17, 110), (24, 140),
           (31, 125), (38, 150), (45, 130)]
CORRECTION = IMPULSE[:-1] + [(45, 130), (52, 140), (59, 115), (66, 120)]


class WaveStructureTest(unittest.TestCase):
    def test_confirmed_up_impulse_has_auditable_points(self):
        result = detect_wave_structure(price_path(IMPULSE, 42))
        self.assertEqual(result["status"], "impulse_candidate")
        self.assertEqual(result["stage"], "wave5_complete")
        self.assertEqual(result["direction"], "up")
        self.assertEqual([point["label"] for point in result["points"]], list("012345"))
        self.assertTrue(all(point["confirmed_on"] <= "2026-12-31" for point in result["points"]))
        self.assertFalse(result["is_predictive_probability"])

    def test_last_pivot_needs_three_future_closes_to_confirm(self):
        before = detect_wave_structure(price_path(IMPULSE, 41))
        after = detect_wave_structure(price_path(IMPULSE, 42))
        self.assertEqual(before["status"], "impulse_forming")
        self.assertEqual(before["stage"], "wave5_pending")
        self.assertEqual(after["status"], "impulse_candidate")
        self.assertTrue(all(point["confirmed_on"] <= price_path(IMPULSE, 42).index[-1].date().isoformat()
                            for point in after["points"]))

    def test_abc_needs_prior_impulse_and_c_beyond_a(self):
        result = detect_wave_structure(price_path(CORRECTION, 63))
        self.assertEqual(result["status"], "abc_candidate")
        self.assertEqual([point["label"] for point in result["points"]], list("012345ABC"))
        self.assertIn("c_beyond_a", result["rule_checks"])

    def test_wave_four_overlap_rejects_standard_impulse(self):
        overlap = [(bar, 115 if bar == 31 else value) for bar, value in IMPULSE]
        result = detect_wave_structure(price_path(overlap, 42))
        self.assertNotEqual(result["status"], "impulse_candidate")

    def test_one_day_final_swing_is_not_counted_as_a_daily_wave(self):
        noisy = [(0, 105), (3, 100), (10, 120), (17, 110), (24, 140),
                 (31, 125), (32, 150), (45, 130)]
        result = detect_wave_structure(price_path(noisy, 42))
        self.assertNotEqual(result["status"], "impulse_candidate")

    def test_flat_or_missing_data_is_not_fabricated_into_waves(self):
        days = pd.bdate_range("2026-01-02", periods=60)
        flat = pd.DataFrame({"High": 100.0, "Low": 100.0, "Close": 100.0}, index=days)
        self.assertEqual(detect_wave_structure(flat)["status"], "no_pattern")
        flat.loc[days[30], "High"] = float("nan")
        self.assertEqual(detect_wave_structure(flat)["status"], "unverified")

    def test_downward_impulse_uses_same_structural_constraints(self):
        inverse = [(bar, 200 - value) for bar, value in IMPULSE]
        result = detect_wave_structure(price_path(inverse, 42))
        self.assertEqual(result["status"], "impulse_candidate")
        self.assertEqual(result["direction"], "down")


if __name__ == "__main__":
    unittest.main()
