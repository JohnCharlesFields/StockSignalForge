import copy
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import api_server as api
import market_data_service as market
import priority_board_service as board
from scripts import backfill_daily_gap as gap
from scripts import screening_framework_v2_optimized as screen
from tests.test_daily_batch_reliability_unittest import ImmediateThread


class CurrentSessionBatchTests(unittest.TestCase):
    def test_unpublished_session_never_estimates_or_downloads(self):
        client = Mock()
        client.metadata.get_dataset_range.return_value = {"schema": {"ohlcv-1d": {"end": "2026-10-08T04:00:00Z"}}}
        with patch.object(gap, "_cache_files", return_value=[Path("NVDA.csv")]), patch("databento.Historical", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "not published"):
                gap.backfill("2026-10-08", "2026-10-09", True, 1)
        client.metadata.get_cost.assert_not_called()
        client.timeseries.get_range.assert_not_called()

    def test_availability_failure_does_not_reserve_budget(self):
        records = {}
        reply = subprocess.CompletedProcess([], 1, "", json.dumps({"phase": "availability", "download_started": False,
            "repair_error": "daily_data_not_published", "available_end": "2026-10-08T04:00:00Z"}))
        with patch.dict(api.os.environ, {"DATABENTO_API_KEY": "test", "VIBE_DAILY_PRICE_REPAIR_MAX_COST_USD": "1"}), \
                patch.object(api, "cache_get", side_effect=lambda k: records.get(k)), \
                patch.object(api, "cache_set", side_effect=lambda k, v: records.update({k: v})), \
                patch.object(api.subprocess, "run", return_value=reply) as run:
            result = api._daily_databento_gap_repair("2026-10-08")
        self.assertEqual(result["status"], "waiting_data")
        self.assertFalse(result["budget_reservation_retained"])
        self.assertEqual(records["daily_three_layer_auto:price_repair_budget"]["committed_usd"], 0)
        self.assertEqual(run.call_count, 1)
        self.assertNotIn("--execute", run.call_args.args[0])

    def test_unknown_old_reservation_is_not_cleared(self):
        budget = {"date": api._auto_scan_today_key(), "committed_usd": 1}
        with patch.dict(api.os.environ, {"DATABENTO_API_KEY": "test", "VIBE_DAILY_PRICE_REPAIR_MAX_COST_USD": "1"}), \
                patch.object(api, "cache_get", return_value=budget), patch.object(api, "cache_set") as write, \
                patch.object(api.subprocess, "run") as run:
            self.assertIn("exhausted", api._daily_databento_gap_repair("2026-10-08")["error"])
        write.assert_not_called()
        run.assert_not_called()

    def test_unknown_download_retains_estimate_not_entire_daily_cap(self):
        records = {}
        def request(command, **kwargs):
            if "--execute" not in command:
                return subprocess.CompletedProcess([], 0, json.dumps({"estimated_cost_usd": 0.002}), "")
            self.assertAlmostEqual(records["daily_three_layer_auto:price_repair_budget"]["committed_usd"], .002)
            return subprocess.CompletedProcess([], 1, "", json.dumps({"phase": "download", "download_started": True}))
        with patch.dict(api.os.environ, {"DATABENTO_API_KEY": "test", "VIBE_DAILY_PRICE_REPAIR_MAX_COST_USD": "1"}), \
                patch.object(api, "cache_get", side_effect=lambda k: records.get(k)), \
                patch.object(api, "cache_set", side_effect=lambda k, v: records.update({k: v})), \
                patch.object(api.subprocess, "run", side_effect=request):
            result = api._daily_databento_gap_repair("2026-10-08")
        self.assertTrue(result["budget_reservation_retained"])
        self.assertAlmostEqual(records["daily_three_layer_auto:price_repair_budget"]["committed_usd"], .002)

    def test_current_candidate_after_old_top_rows_is_not_lost(self):
        rows = [{"ticker": f"OLD{i}", "price_as_of": "2026-10-07"} for i in range(50)]
        rows.append({"ticker": "NVDA", "price_as_of": "2026-10-08"})
        with patch.object(api, "_stock_signal_find_report", return_value=(Path("test.json"), {"results": rows}, False)), \
                patch.object(api, "_stock_signal_filter_results", return_value=rows), \
                patch.object(api, "_stock_signal_candidate_pool", side_effect=lambda items, *args: items[:1]), \
                patch.object(api, "_daily_report_price_coverage", return_value={"current_symbols": ["NVDA"]}):
            found = api._stock_signal_observation_pool("ndx", 1, "2026-10-08")
        self.assertEqual(found[0]["ticker"], "NVDA")

    def test_partial_report_preserves_membership_and_original_dates(self):
        old = {"universe": {"name": "ndx", "memberships": {"AAPL": ["NDX"], "NVDA": ["NDX"]}},
               "results": [{"ticker": "AAPL", "price_as_of": "2026-10-07"}, {"ticker": "NVDA", "price_as_of": "2026-10-07"}]}
        fresh = {"results": [{"ticker": "NVDA", "price_as_of": "2026-10-08"}], "universe": {"name": "ndx"}}
        result = api._merge_daily_subset_report(old, fresh)
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][0]["price_as_of"], "2026-10-07")
        self.assertEqual(result["universe"], old["universe"])

    def test_stock_only_fallback_uses_existing_pullback_function(self):
        dates = pd.bdate_range(end="2026-10-08", periods=80)
        frame = pd.DataFrame({"Open": 100., "High": 102., "Low": 99., "Close": 101., "Volume": 1000000}, index=dates)
        with patch.object(api, "get_daily_history", return_value=(frame, "cache:ohlcv")):
            rows = api._watchlist_observation_pool(10, symbols=["AAPL"], source_label="spx")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["price_as_of"], "2026-10-08")
        self.assertEqual(rows[0]["source_pools"], ["spx"])
        self.assertFalse(rows[0]["option_evidence"]["available"])
        self.assertIn("pullback_rejection", rows[0])

    def test_report_lookup_does_not_parse_unrelated_history(self):
        paths = [Path("20261009_010000_ab_cd/options_screening_results_sox_261009.json"),
                 Path("20261008_010000_ab_cd/options_screening_results_ndx_261008.json"),
                 Path("20261007_010000_ab_cd/options_screening_results_ndx_261007.json")]
        with patch.object(api, "_stock_signal_report_candidates", return_value=paths), \
                patch.object(api, "_load_json_file", return_value={"universe": {"name": "ndx"}, "results": [{"ticker": "AAPL"}]}) as load:
            self.assertEqual(api._stock_signal_find_report("ndx")[0], paths[1])
        load.assert_called_once_with(paths[1])

    def test_empty_pools_end_without_scanning_or_finalizing(self):
        records = {}
        with patch.object(api, "_AUTO_SCAN_JOB", {}), patch.object(api, "_start_daily_news_update"), \
                patch.object(api, "_auto_scan_universe_ids", return_value=["ndx", "watchlist"]), \
                patch.object(api, "_daily_sync_prices", return_value="2026-10-08"), \
                patch.object(api, "_daily_report_price_coverage", return_value={"current_by_universe": {}}), \
                patch.object(api, "cache_get", side_effect=lambda k: copy.deepcopy(records.get(k))), \
                patch.object(api, "cache_set", side_effect=lambda k, v: records.update({k: copy.deepcopy(v)})), \
                patch.object(api, "_run_research_signal_hub_job") as scan, patch.object(api, "_daily_post_scan_finalize") as finalize:
            api._run_daily_three_layer_auto_scan(reason="test")
        scan.assert_not_called()
        finalize.assert_not_called()
        self.assertEqual(records[api._AUTO_SCAN_STATE_KEY]["status"], "waiting_data")
        self.assertEqual(records[api._AUTO_SCAN_STATE_KEY]["skipped_universe_count"], 2)

    def test_only_current_pool_runs_and_finalize_reuses_preflight(self):
        records, calls = {}, []
        def scan(job_id, payload):
            calls.append((payload.universe, api._research_signal_hub_jobs[job_id]["daily_current_symbols"]))
            api._research_signal_hub_jobs[job_id].update(status="completed", result={"decision_count": 1})
        with patch.object(api, "_AUTO_SCAN_JOB", {}), patch.object(api, "_DAILY_ACTIVE_WORKERS", {}), \
                patch.object(api, "_start_daily_news_update"), patch.object(api, "_auto_scan_universe_ids", return_value=["ndx", "spx"]), \
                patch.object(api, "_daily_sync_prices", return_value="2026-10-08") as sync, \
                patch.object(api, "_daily_report_price_coverage", return_value={"current_by_universe": {"ndx": ["NVDA"]}}), \
                patch.object(api, "cache_get", side_effect=lambda k: copy.deepcopy(records.get(k))), \
                patch.object(api, "cache_set", side_effect=lambda k, v: records.update({k: copy.deepcopy(v)})), \
                patch.object(api, "_run_research_signal_hub_job", side_effect=scan), \
                patch.object(api, "_daily_post_scan_finalize", return_value={"snapshot_id": "new", "snapshot_row_count": 1}) as finalize, \
                patch.object(api, "_daily_post_scan_optional_enrichment"), patch.object(api.threading, "Thread", ImmediateThread):
            api._run_daily_three_layer_auto_scan(reason="test")
        self.assertEqual(calls, [("ndx", ["NVDA"])])
        sync.assert_called_once()
        self.assertEqual(finalize.call_args.kwargs["preflight"]["session"], "2026-10-08")
        self.assertEqual(records[api._AUTO_SCAN_STATE_KEY]["status"], "partial")

    def test_daily_option_and_fundamental_miss_never_requests_live(self):
        stock = Mock()
        with tempfile.TemporaryDirectory() as temp, patch.dict(screen.CONFIG, {"daily_price_cache_only": True, "cache_enabled": True, "cache_dir": temp}), \
                patch.object(screen, "_cboe_expiries") as cboe, patch.object(screen, "fetch_fundamentals_live") as fundamentals:
            self.assertEqual(screen.cached_options(stock, "NVDA")[0], ())
            self.assertTrue(screen.cached_option_chain(stock, "NVDA", "2026-11-20")[0].empty)
            self.assertIsNone(screen.cached_fundamentals(stock, "NVDA")[0]["market_cap"])
        cboe.assert_not_called()
        fundamentals.assert_not_called()
        stock.option_chain.assert_not_called()

    def test_waiting_publication_has_separate_bounded_retry_counter(self):
        now = datetime.now(api._auto_scan_timezone())
        record = {"date": api._auto_scan_today_key(now), "status": "waiting_data", "phase": "price_preflight",
                  "finished_at": (now - timedelta(minutes=31)).isoformat(), "price_repair": {"status": "waiting_data"}}
        with patch.dict(api.os.environ, {"VIBE_DAILY_DATA_WAIT_RETRY_LIMIT": "8"}), \
                patch.object(api, "cache_get", return_value={"date": record["date"], "count": 2, "data_wait_count": 2}):
            self.assertTrue(api._daily_auto_retry_due(now, record))
            self.assertFalse(api._daily_auto_retry_due(now, {**record, "finished_at": now.isoformat()}))
        with patch.object(api, "cache_get", return_value={"date": record["date"], "data_wait_count": 8}):
            self.assertFalse(api._daily_auto_retry_due(now, record))

    def test_track_workers_inherit_cache_only_scope(self):
        calls = []
        def track(symbol):
            calls.append(market.external_data_allowed())
            return {"industry": "cached"}
        with market.external_data_scope(False), patch.object(board, "cache_get", return_value=None), \
                patch.object(board, "_symbol_track", side_effect=track):
            board.prewarm_tracks(["AAPL", "NVDA"])
        self.assertEqual(calls, [False, False])

    def test_cache_only_track_miss_never_calls_yfinance(self):
        with market.external_data_scope(False), patch.object(board, "cache_get", return_value=None), \
                patch.object(market, "get_ticker_reference", return_value={}), patch("yfinance.Ticker") as ticker:
            self.assertIsNone(board._symbol_track("AAPL")["industry"])
        ticker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
