from __future__ import annotations

import asyncio
import threading
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import pandas as pd

import call_plan_service as plan
from scripts.backtest_leader_long_options import CostCappedHistorical


SESSION = date(2026, 10, 8)
START = date(2026, 10, 9)
CONFIG = plan.PlanConfig(capital=10000, risk_fraction=0.05)


def definitions():
    return pd.DataFrame([{"expiration": "2026-12-18", "strike_price": 100,
                          "instrument_class": "C", "raw_symbol": "TEST  261218C00100000", "unit_of_measure_qty": 100}])


def quotes(bid=5.0, ask=5.1):
    start, _ = plan.quote_window(SESSION)
    return pd.DataFrame({"symbol": ["TEST  261218C00100000"] * 5,
                         "bid_px_00": [bid] * 5, "ask_px_00": [ask] * 5,
                         "bid_sz_00": [10] * 5, "ask_sz_00": [12] * 5},
                        index=pd.date_range(start, periods=5, freq="min"))


class CallPlanTests(unittest.TestCase):
    def setUp(self):
        self.contract = plan.select_definitions(definitions(), 100, SESSION, START)[0]

    def test_selects_real_sixty_ninety_day_calls(self):
        self.assertEqual(self.contract["dte"], 70)
        bad = definitions()
        bad.loc[0, "raw_symbol"] = "TEST  261218C00999000"
        self.assertEqual(plan.select_definitions(bad, 100, SESSION, START), [])
        bad = definitions()
        bad.loc[0, "unit_of_measure_qty"] = 10
        self.assertEqual(plan.select_definitions(bad, 100, SESSION, START), [])
        bad = definitions()
        bad.loc[0, "expiration"] = "2026-10-30"
        bad.loc[0, "raw_symbol"] = "TEST  261030C00100000"
        self.assertEqual(plan.select_definitions(bad, 100, SESSION, START), [])

    def test_definition_duplicates_are_bounded(self):
        rows = pd.concat([definitions()] * 50, ignore_index=True)
        self.assertEqual(len(plan.select_definitions(rows, 100, SESSION, START)), 1)

    def test_dst_and_early_close(self):
        self.assertIn("19:55", plan.quote_window(date(2026, 10, 8))[0])
        self.assertIn("20:55", plan.quote_window(date(2026, 12, 1))[0])
        self.assertIn("17:55", plan.quote_window(date(2026, 11, 27))[0])

    def test_rejects_one_sided_crossed_and_future_quotes(self):
        for q in (quotes(bid=0), quotes(bid=6, ask=5), quotes(ask=float("nan"))):
            self.assertIsNone(plan.evaluate_quote(self.contract, q, 100, SESSION, CONFIG))
        q = quotes()
        q.index += pd.Timedelta(days=1)
        self.assertIsNone(plan.evaluate_quote(self.contract, q, 100, SESSION, CONFIG))

    def test_quote_quality_and_missing_size_are_not_faked(self):
        q = quotes()
        row = plan.evaluate_quote(self.contract, q, 100, SESSION, CONFIG)
        self.assertTrue(row["comparison_eligible"])
        self.assertGreater(row["delta"], 0.45)
        self.assertIsNone(row["volume"])
        self.assertIsNone(row["open_interest"])
        q = q.drop(columns=["bid_sz_00"])
        self.assertFalse(plan.evaluate_quote(self.contract, q, 100, SESSION, CONFIG)["comparison_eligible"])
        q = quotes().iloc[:1]
        self.assertIn("报价连续性不足", plan.evaluate_quote(self.contract, q, 100, SESSION, CONFIG)["risk_flags"])

    def test_budget_and_wide_spread_are_flags_not_green_recommendations(self):
        row = plan.evaluate_quote(self.contract, quotes(bid=4, ask=5.1), 100, SESSION, plan.PlanConfig())
        self.assertIn("价差过宽", row["risk_flags"])
        self.assertIn("计划止损风险超预算", row["risk_flags"])
        self.assertFalse(row["comparison_eligible"])
        self.assertEqual(row["max_loss_usd"], 510.65)

    def test_unknown_and_near_earnings(self):
        self.assertEqual(plan.event_window({}, START, CONFIG)["status"], "unknown")
        self.assertEqual(plan.event_window({"next_date": "2026-10-08"}, START, CONFIG)["status"], "unknown")
        self.assertEqual(plan.event_window({"next_date": "2026-10-12"}, START, CONFIG)["status"], "blocked")
        mid = plan.event_window({"next_date": "2026-10-19"}, START, CONFIG)
        self.assertEqual(mid["status"], "shortened")
        self.assertLess(mid["latest_exit_date"], "2026-10-19")
        self.assertEqual(plan.event_window({"next_date": "2026-12-01"}, START, CONFIG)["status"], "clear_known_earnings")

    def test_calendar_decay_iv_stress_costs_and_no_probability(self):
        row = plan.evaluate_quote(self.contract, quotes(), 100, SESSION, CONFIG)
        events = plan.event_window({"next_date": "2026-12-01"}, START, CONFIG)
        scenarios = plan.exit_scenarios(row, 100, 95, 103, START, events, CONFIG)
        same = [r for r in scenarios if r["target_name"] == "原地不涨"]
        self.assertLess(same[-1]["iv_flat"]["estimated_bid"], same[0]["iv_flat"]["estimated_bid"])
        self.assertTrue(all(s["iv_down"]["net_usd"] < s["iv_flat"]["net_usd"] < s["iv_up"]["net_usd"] for s in same))
        for s in scenarios:
            self.assertIsNone(s["expected_net_usd"])
            self.assertIsNone(s["hit_probability"])
            self.assertAlmostEqual(s["iv_flat"]["net_usd"], (s["iv_flat"]["estimated_bid"] - row["ask"]) * 100 - 1.3, delta=0.02)

    def test_earnings_limits_all_scenarios(self):
        row = plan.evaluate_quote(self.contract, quotes(), 100, SESSION, CONFIG)
        events = plan.event_window({"next_date": "2026-10-19"}, START, CONFIG)
        rows = plan.exit_scenarios(row, 100, 95, 103, START, events, CONFIG)
        self.assertTrue(rows)
        self.assertTrue(all(r["exit_date"] <= events["latest_exit_date"] for r in rows))

    def test_read_is_cache_only_and_interrupted_job_is_visible(self):
        with patch.object(plan, "cache_get", side_effect=[None, {"status": "running"}]), patch.object(plan, "get_daily_history") as history:
            out = plan.read_plan("TEST")
        self.assertFalse(out["available"])
        self.assertEqual(out["job"]["status"], "interrupted")
        history.assert_not_called()

    def test_stale_stock_blocks_before_any_option_download(self):
        frame = pd.DataFrame({c: [100] * 250 for c in ["Open", "High", "Low", "Close", "Volume"]}, index=pd.bdate_range(end="2026-10-07", periods=250))
        feed = Mock()
        with patch.object(plan, "get_daily_history", return_value=(frame, "fixture")), patch.object(plan, "get_next_earnings", return_value={}):
            result = plan.build_plan("TEST", CONFIG, 0, feed=feed, now=datetime(2026, 10, 9, 10, tzinfo=timezone.utc))
        self.assertEqual(result["status"], "blocked")
        feed.fetch.assert_not_called()

    def test_full_plan_uses_own_expiry_iv_and_trims_future_data(self):
        frame = pd.DataFrame({c: [100.] * 250 for c in ["Open", "High", "Low", "Close", "Volume"]}, index=pd.bdate_range(end="2026-10-08", periods=250))
        frame.loc[pd.Timestamp("2026-10-09")] = 9999
        feed = Mock(estimated_cost=0.0, requests=[])
        feed.fetch.side_effect = [definitions(), quotes()]
        with patch.object(plan, "get_daily_history", return_value=(frame, "fixture")), patch.object(plan, "cache_get", return_value=None), patch.object(plan, "priority_pool", return_value="fixture"), patch.object(plan, "get_next_earnings", return_value={"next_date": "2026-12-01"}), patch.object(plan, "consensus_feature_frame", return_value=pd.DataFrame({"net_consensus": [0.5]})) as features:
            result = plan.build_plan("TEST", CONFIG, 0, feed=feed, now=datetime(2026, 10, 9, 10, tzinfo=timezone.utc))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["spot"], 100)
        self.assertEqual(features.call_args.args[0].index[-1].date(), SESSION)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertIsNone(result["expected_net_usd"])

    def test_zero_cap_prevents_billable_call_and_failed_call_reserves_cost(self):
        client = Mock()
        client.metadata.get_cost.return_value = 0.02
        with TemporaryDirectory() as root:
            feed = CostCappedHistorical(client, Path(root), 0)
            with self.assertRaises(ValueError):
                feed.fetch(schema="definition", symbols=["TEST.OPT"], start="x", end="y", stype="parent")
            client.timeseries.get_range.assert_not_called()
            self.assertEqual(feed.requests[0]["status"], "cost_blocked")
            feed = CostCappedHistorical(client, Path(root), 0.03)
            client.timeseries.get_range.side_effect = RuntimeError("failed")
            with self.assertRaises(RuntimeError):
                feed.fetch(schema="definition", symbols=["TEST.OPT"], start="x", end="y", stype="parent")
            self.assertEqual(feed.estimated_cost, 0.02)
            self.assertEqual(feed.requests[0]["status"], "failed_cost_reserved")

    def test_tls_retry_is_bounded_and_reserves_each_attempt(self):
        import requests
        client = Mock()
        client.metadata.get_cost.return_value = 0.02
        client.timeseries.get_range.side_effect = [requests.exceptions.SSLError("test"), Mock(to_df=lambda: quotes())]
        with TemporaryDirectory() as root:
            feed = CostCappedHistorical(client, Path(root), 0.05)
            feed.fetch(schema="cbbo-1m", symbols=["TEST"], start="x", end="y", stype="raw_symbol")
            self.assertEqual(client.timeseries.get_range.call_count, 2)
            self.assertEqual(feed.estimated_cost, 0.04)
            self.assertEqual(feed.requests[0]["status"], "failed_cost_reserved")
        client.reset_mock()
        client.timeseries.get_range.side_effect = requests.exceptions.SSLError("test")
        with TemporaryDirectory() as root:
            feed = CostCappedHistorical(client, Path(root), 0.03)
            with self.assertRaises(ValueError):
                feed.fetch(schema="cbbo-1m", symbols=["TEST"], start="x", end="y", stype="raw_symbol")
            self.assertEqual(client.timeseries.get_range.call_count, 1)
            self.assertEqual(feed.estimated_cost, 0.02)

    def test_automatic_iv_enrichment_does_not_incur_default_opra_fees(self):
        import databento
        import market_data_service as market
        client = Mock()
        client.metadata.get_cost.return_value = 0.01
        with TemporaryDirectory() as root, patch.dict("os.environ", {"DATABENTO_API_KEY": "fixture", "DATABENTO_OPRA_SNAPSHOT_MAX_COST_USD": "0"}), patch.object(market, "_CACHE_ROOT", Path(root)), patch.object(market, "_fresh", return_value=False), patch.object(market, "_close_on_or_before", return_value=100), patch.object(databento, "Historical", return_value=client):
            result = market.get_databento_options_snapshot("TEST", 100)
            self.assertEqual(result["reason"], "cost_cap")
            client.timeseries.get_range.assert_not_called()

    def test_worker_deduplicates_preserves_snapshot_and_releases_slots(self):
        saved = {"call_plan:v1:TEST": {"status": "completed", "symbol": "TEST"}}
        slots = threading.BoundedSemaphore(1)
        with patch.object(plan, "cache_get", side_effect=saved.get), patch.object(plan, "cache_set", side_effect=lambda k, v, **kw: saved.update({k: v})), patch.object(plan.threading, "Thread") as thread, patch.object(plan, "build_plan", return_value={"status": "unavailable", "reason": "blocked", "estimated_cost_usd": 0}):
            self.assertTrue(plan.start_plan("TEST", slots)["started"])
            self.assertFalse(plan.start_plan("TEST", slots)["started"])
            thread.call_args.kwargs["target"]()
            self.assertEqual(saved["call_plan:v1:TEST"]["status"], "completed")
            self.assertEqual(plan.read_plan("TEST")["job"]["status"], "unavailable")
            self.assertTrue(slots.acquire(blocking=False))
            slots.release()
            self.assertFalse(plan.start_plan("TEST", slots)["started"])

    def test_api_read_only_and_new_greeks_align_with_quote_spot(self):
        import api_server as api
        with patch.object(plan, "read_plan", return_value={"available": False, "job": {"status": "idle"}}) as reader:
            self.assertFalse(asyncio.run(api.single_stock_call_plan("NVDA"))["available"])
            reader.assert_called_once_with("NVDA")
        with patch.object(api, "cache_get", return_value=None), patch.object(api, "cache_set"), patch.object(api, "get_massive_options_snapshot", return_value={"available": False}), patch.object(api, "get_databento_options_snapshot", return_value={"available": True, "underlying_price_for_iv": 100, "atm_iv": 0.5, "call_iv": 0.5, "put_iv": 0.5, "dte": 22, "call_strike": 100, "put_strike": 100}), patch.object(api, "_single_bs_greeks", return_value={"delta": 0.5}) as greeks:
            api._single_option_iv("NVDA", 120, 0.4)
            self.assertEqual(greeks.call_args_list[0].kwargs["spot"], 100)
            self.assertEqual(greeks.call_args_list[1].kwargs["spot"], 100)

    def test_blocked_refresh_retains_historical_candidates_but_marks_not_current(self):
        saved = {"call_plan:v1:TEST": {"status": "partial", "symbol": "TEST", "candidates": [{"contract": "old"}]}}
        slots = threading.BoundedSemaphore(1)
        with patch.object(plan, "cache_get", side_effect=saved.get), patch.object(plan, "cache_set", side_effect=lambda k, v, **kw: saved.update({k: v})), patch.object(plan.threading, "Thread") as thread, patch.object(plan, "build_plan", return_value={"status": "blocked", "reason": "stale", "estimated_cost_usd": 0}):
            plan.start_plan("TEST", slots)
            thread.call_args.kwargs["target"]()
            self.assertEqual(saved["call_plan:v1:TEST"]["candidates"][0]["contract"], "old")
            self.assertTrue(plan.read_plan("TEST")["current_request_blocked"])


if __name__ == "__main__":
    unittest.main()
