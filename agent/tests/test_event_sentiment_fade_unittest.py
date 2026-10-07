import importlib.util
import sqlite3
import unittest
from pathlib import Path

import pandas as pd


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backtest_event_sentiment_fade.py"
SPEC = importlib.util.spec_from_file_location("backtest_event_sentiment_fade", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class EventSentimentFadeTest(unittest.TestCase):
    def test_premarket_afterhours_and_intraday_alignment(self):
        self.assertEqual(MODULE.session_day("2026-09-23T12:00:00Z"), "2026-09-23")
        self.assertEqual(MODULE.session_day("2026-09-23T21:00:00Z"), "2026-09-24")
        self.assertIsNone(MODULE.session_day("2026-09-23T15:00:00Z"))
        self.assertEqual(MODULE.session_day("2026-09-20T15:00:00Z"), "2026-09-20")
        sessions = pd.Series([1, 2], index=pd.to_datetime(["2026-09-18", "2026-09-21"]))
        self.assertEqual(MODULE.next_trading_session("2026-09-20", sessions), "2026-09-21")

    def test_prior_pe_never_uses_future_snapshot(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE home_dashboard_snapshots "
            "(generated_at TEXT, status TEXT, payload_json TEXT)"
        )
        conn.execute(
            "INSERT INTO home_dashboard_snapshots VALUES (?,?,?)",
            ("2026-09-20T00:00:00Z", "completed", '{"rows":[{"symbol":"META","pe":25}]}'),
        )
        conn.execute(
            "INSERT INTO home_dashboard_snapshots VALUES (?,?,?)",
            ("2026-09-25T00:00:00Z", "completed", '{"rows":[{"symbol":"META","pe":99}]}'),
        )
        snapshots = MODULE.prior_valuation_snapshots(conn)
        conn.close()
        self.assertEqual(MODULE.prior_pe(snapshots, "META", "2026-09-23T12:00:00Z"), 25)
        self.assertIsNone(MODULE.prior_pe(snapshots, "META", "2026-10-10T12:00:00Z"))

    def test_event_day_move_is_not_forward_return(self):
        dates = pd.bdate_range("2026-06-01", periods=45)
        prices = pd.Series([100.0 + (i % 2) for i in range(24)] + [100.0] + [105.0] * 20, index=dates)
        spy = pd.Series([100.0] * 45, index=dates)
        row = MODULE.study_event("TEST", dates[25].date().isoformat(), prices, spy)
        self.assertTrue(row["rally"])
        self.assertAlmostEqual(row["initial_excess_spy"], 0.05)
        self.assertAlmostEqual(row["excess_1d"], 0.0)
        self.assertAlmostEqual(row["excess_10d"], 0.0)

    def test_pe_reference_is_target_only_and_never_uses_future_multiple(self):
        dates = ["2026-09-01", "2026-09-08", "2026-09-15", "2026-09-25"]
        multiples = [20.0, 25.0, 30.0, 100.0]
        prices = [100.0, 125.0, 150.0, 500.0]
        snapshots = {
            "TEST": [
                (pd.Timestamp(day, tz="UTC"), pe, price, day)
                for day, pe, price in zip(dates, multiples, prices)
            ]
        }
        reference = MODULE.valuation_reference(snapshots, "TEST", "2026-09-20T12:00:00Z", 200.0)
        self.assertEqual(reference["pe_reference_status"], "unverified_pe_basis_static_earnings_scenario")
        self.assertEqual(reference["pe_reference_snapshot_days"], 3)
        self.assertAlmostEqual(reference["pe_reference_multiple"], 25.0)
        self.assertAlmostEqual(reference["pe_reference_price_provisional"], 125.0)
        self.assertAlmostEqual(reference["pe_reference_gap_pct_provisional"], -0.375)

    def test_peer_reference_uses_same_sublane_date_and_pe_basis(self):
        target = {
            "symbol": "TEST", "current_price": 100, "price_as_of": "2026-09-18",
            "pe_type": "trailing", "trailing_pe": 20,
            "peer_mapping": {
                "group_id": "chips", "target_sublane": "ai_chips",
                "direct_peers": ["A", "B", "C", "D", "E", "F"],
                "peer_sublanes": {name: "ai_chips" for name in "ABCDEF"},
            },
        }
        rows = {"TEST": target}
        for name, pe in zip("ABC", [10, 12, 14]):
            rows[name] = {
                "symbol": name, "price_as_of": "2026-09-18", "pe_type": "trailing", "trailing_pe": pe,
            }
        rows["D"] = {"price_as_of": "2026-09-17", "pe_type": "trailing", "trailing_pe": 99}
        rows["E"] = {"price_as_of": "2026-09-18", "pe_type": "forward", "forward_pe": 99}
        rows["F"] = {"price_as_of": "2026-09-18", "pe_type": "trailing", "trailing_pe": -5}
        snapshots = [
            (pd.Timestamp("2026-09-18T21:00:00Z"), rows),
            (pd.Timestamp("2026-09-25T21:00:00Z"), {"TEST": {**target, "current_price": 999}}),
        ]
        result = MODULE.peer_valuation_reference(snapshots, "TEST", "2026-09-20T12:00:00Z", 120)
        self.assertEqual(result["peer_reference_status"], "static_earnings_peer_scenario")
        self.assertEqual(result["peer_reference_symbols"], ["A", "B", "C"])
        self.assertEqual(result["peer_reference_count"], 3)
        self.assertEqual(result["peer_reference_pe_type"], "trailing")
        self.assertAlmostEqual(result["peer_reference_pe_median"], 12)
        self.assertAlmostEqual(result["peer_reference_price_median"], 60)
        self.assertAlmostEqual(result["peer_reference_gap_pct"], -0.5)

    def test_peer_reference_requires_three_comparable_peers(self):
        target = {
            "symbol": "TEST", "current_price": 100, "price_as_of": "2026-09-18",
            "pe_type": "trailing", "trailing_pe": 20,
            "peer_mapping": {
                "group_id": "chips", "target_sublane": "ai_chips",
                "direct_peers": ["A", "B", "C"],
                "peer_sublanes": {"A": "ai_chips", "B": "ai_chips", "C": "storage"},
            },
        }
        rows = {"TEST": target}
        for name in "ABC":
            rows[name] = {"price_as_of": "2026-09-18", "pe_type": "trailing", "trailing_pe": 15}
        snapshots = [(pd.Timestamp("2026-09-18T21:00:00Z"), rows)]
        result = MODULE.peer_valuation_reference(snapshots, "TEST", "2026-09-20T12:00:00Z", 120)
        self.assertEqual(result["peer_reference_status"], "insufficient_same_sublane_peers")
        self.assertEqual(result["peer_reference_count"], 2)
        self.assertIsNone(result["peer_reference_price_median"])
        target["pe_type"] = None
        result = MODULE.peer_valuation_reference(snapshots, "TEST", "2026-09-20T12:00:00Z", 120)
        self.assertEqual(result["peer_reference_status"], "unverified_target_pe_basis")

    def test_cluster_contrast_requires_enough_shared_symbols(self):
        events = [{"symbol": "A", "excess_5d": -0.05}]
        controls = [{"symbol": "A", "excess_5d": -0.01}]
        result = MODULE.clustered_contrast(events, controls, 5)
        self.assertEqual(result["paired_symbol_count"], 1)
        self.assertIsNone(result["ci95"])


if __name__ == "__main__":
    unittest.main()
