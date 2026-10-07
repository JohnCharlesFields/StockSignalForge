from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd


AGENT = Path(__file__).resolve().parents[1]
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))
MODULE_PATH = AGENT / "scripts" / "backtest_leader_long_options.py"
SPEC = importlib.util.spec_from_file_location("backtest_leader_long_options", MODULE_PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class LeaderOptionsTest(unittest.TestCase):
    def test_incomplete_market_cap_group_is_not_declared_leader(self) -> None:
        with patch.object(module, "PEER_GROUPS", [
            {"id": "test", "label": "测试赛道", "symbols": ["AAA", "BBB"]},
        ]), patch.object(module, "_cached_cap", side_effect=lambda symbol, root: (
            {"market_cap": 1_000_000_000, "source": "test", "captured_at": datetime.now(timezone.utc).isoformat()}
            if symbol == "AAA" else {}
        )):
            snapshot = module.build_leader_snapshot(AGENT)
            group = snapshot["groups"][0]
            self.assertEqual(group["leader"], "AAA")
            self.assertEqual(group["ranking_status"], "provisional_incomplete_or_stale")
            self.assertEqual(group["missing_cap"], ["BBB"])
            self.assertNotIn("AAA", snapshot["eligible_symbols"])
            self.assertIn("NVDA", snapshot["eligible_symbols"])

    def test_choose_actual_listed_contract_nearest_sixty_days(self) -> None:
        entry = date(2026, 8, 5)
        defs = pd.DataFrame([
            {"expiration": "2026-09-18", "strike_price": 100, "instrument_class": "C", "raw_symbol": "SHORT"},
            {"expiration": "2026-10-02", "strike_price": 100, "instrument_class": "C", "raw_symbol": "GOOD"},
            {"expiration": "2026-10-02", "strike_price": 110, "instrument_class": "C", "raw_symbol": "FAR"},
            {"expiration": "2026-10-02", "strike_price": 100, "instrument_class": "P", "raw_symbol": "PUT"},
        ])
        result = module.choose_contract(defs, "C", 101, entry)
        self.assertIsNotNone(result)
        self.assertEqual(result["contract"], "GOOD")
        self.assertEqual(result["strike"], 100)

    def test_path_uses_ask_entry_and_bid_first_touch(self) -> None:
        days = pd.bdate_range("2026-08-05", periods=10, tz="America/New_York")
        stamps = [day + pd.Timedelta(hours=9, minutes=40) for day in days]
        frame = pd.DataFrame({
            "bid_px_00": [0.95, 1.55] + [0.50] * 8,
            "ask_px_00": [1.00, 1.60] + [0.55] * 8,
        }, index=pd.DatetimeIndex(stamps).tz_convert("UTC"))
        result = module.settle_path(frame, date(2026, 8, 5))
        self.assertEqual(result["exit_reason"], "target_first")
        self.assertEqual(result["entry_ask"], 1.0)
        self.assertEqual(result["exit_bid"], 1.55)
        self.assertEqual(result["net_dollars"], 53.70)

    def test_wide_spread_and_missing_sessions_do_not_count_as_wins(self) -> None:
        days = pd.bdate_range("2026-08-05", periods=9, tz="America/New_York")
        stamps = [day + pd.Timedelta(hours=9, minutes=40) for day in days]
        frame = pd.DataFrame({
            "bid_px_00": [0.80] + [1.55] * 8,
            "ask_px_00": [1.00] + [1.60] * 8,
        }, index=pd.DatetimeIndex(stamps).tz_convert("UTC"))
        self.assertEqual(module.settle_path(frame, date(2026, 8, 5))["status"], "wide_entry_spread")
        frame.iloc[0, 0] = 0.95
        frame.iloc[1:, 0] = 0.95
        frame.iloc[1:, 1] = 1.00
        self.assertEqual(module.settle_path(frame, date(2026, 8, 5))["status"], "insufficient_quote_sessions")

    def test_early_exit_does_not_require_later_quotes(self) -> None:
        days = pd.bdate_range("2026-08-05", periods=2, tz="America/New_York")
        stamps = [day + pd.Timedelta(hours=9, minutes=40) for day in days]
        frame = pd.DataFrame({
            "bid_px_00": [0.95, 1.55], "ask_px_00": [1.00, 1.60],
        }, index=pd.DatetimeIndex(stamps).tz_convert("UTC"))
        self.assertEqual(module.settle_path(frame, date(2026, 8, 5))["exit_reason"], "target_first")

    def test_missing_session_before_target_cannot_be_counted_as_win(self) -> None:
        dates = [pd.Timestamp("2026-08-05 09:40", tz="America/New_York"),
                 pd.Timestamp("2026-08-07 09:40", tz="America/New_York")]
        frame = pd.DataFrame({
            "bid_px_00": [0.95, 1.55], "ask_px_00": [1.00, 1.60],
        }, index=pd.DatetimeIndex(dates).tz_convert("UTC"))
        result = module.settle_path(frame, date(2026, 8, 5))
        self.assertEqual(result["status"], "insufficient_quote_sessions")
        self.assertEqual(result["missing_sessions"], ["2026-08-06"])

    def test_cash_and_stop_budget_are_reported_separately(self) -> None:
        audit = module.risk_audit({"entry_ask": 17.0}, 2000.0, 0.015, 0.65)
        self.assertEqual(audit["premium_cash_usd"], 1700.65)
        self.assertEqual(audit["planned_stop_risk_usd"], 426.30)
        self.assertFalse(audit["within_cash_and_planned_stop_budget"])

    def test_time_exit_requires_near_close_quote(self) -> None:
        days = pd.bdate_range("2026-08-05", periods=10, tz="America/New_York")
        stamps = [day + pd.Timedelta(hours=9, minutes=40) for day in days]
        frame = pd.DataFrame({
            "bid_px_00": [0.95] * 10, "ask_px_00": [1.00] * 10,
        }, index=pd.DatetimeIndex(stamps).tz_convert("UTC"))
        result = module.settle_path(frame, date(2026, 8, 5))
        self.assertEqual(result["status"], "missing_close_quote")

    def test_cost_limit_is_checked_before_download(self) -> None:
        class Metadata:
            def get_cost(self, **kwargs):
                return 0.10

        class TimeSeries:
            def get_range(self, **kwargs):
                raise AssertionError("billable download should not happen")

        class Client:
            metadata = Metadata()
            timeseries = TimeSeries()

        with patch.object(module, "_cache_file", return_value=Path("nonexistent-test-opra-cache")):
            feed = module.CostCappedHistorical(Client(), AGENT, 0.05)
            with self.assertRaisesRegex(ValueError, "cost_cap"):
                feed.fetch(schema="definition", symbols=["NVDA.OPT"], start="2026-08-05", end="2026-08-06", stype="parent")
            self.assertEqual(feed.estimated_cost, 0.0)

    def test_event_selection_uses_only_prior_crossing(self) -> None:
        index = pd.bdate_range("2025-09-01", periods=230)
        stock = pd.DataFrame({"Close": [100.0] * 230}, index=index)
        net = [0.0] * 230
        net[210] = 0.13
        net[211] = 0.14
        net[213] = -0.13
        features = pd.DataFrame({"net_consensus": net}, index=index)
        with patch.object(module, "consensus_feature_frame", return_value=features):
            result = module.select_consensus_events(
                ["NVDA"], index[205].date(), index[215].date(), max_events=2,
                histories={"NVDA": stock, "SPY": stock},
            )
        self.assertEqual([row["side"] for row in result], ["C"])
        self.assertEqual(result[0]["signal_date"], index[210].date().isoformat())


if __name__ == "__main__":
    unittest.main()
