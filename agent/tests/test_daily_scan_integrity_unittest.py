from __future__ import annotations

import unittest
from contextlib import nullcontext
from datetime import date
from unittest.mock import patch

import pandas as pd
import requests

import api_server
import market_calendar
import market_data_service
import priority_board_service
import prediction_ledger_service
from scripts import ingest_grouped_daily


class DailyScanIntegrityTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(api_server, "_start_daily_news_update")
        patcher.start()
        self.addCleanup(patcher.stop)
        enrichment = patch.object(api_server, "_daily_post_scan_optional_enrichment")
        enrichment.start()
        self.addCleanup(enrichment.stop)

    def test_candidate_ledger_is_written_before_optional_enrichment(self) -> None:
        calls = []
        session = date(2026, 9, 22)

        def log_predictions(*args, **kwargs):
            calls.append(("ledger", kwargs.get("as_of_date")))
            return {"slice_rows_written": 1, "candidates": 1}

        with (
            patch.object(market_calendar, "most_recent_session", return_value=session),
            patch.object(api_server, "cache_get", return_value=None),
            patch.object(api_server, "cache_set", side_effect=lambda *args: calls.append(("marker", args[1]))),
            patch.object(api_server, "external_data_scope", return_value=nullcontext()),
            patch.object(ingest_grouped_daily, "ingest_recent_grouped_daily", return_value={
                "ok": True, "latest_data_date": session.isoformat(), "symbols_written": 1,
                "latest_session_symbols": 1,
            }),
            patch.object(api_server, "_build_home_dashboard_snapshot_payload", return_value={"rows": [{"symbol": "NVDA"}]}),
            patch.object(api_server, "save_home_dashboard_snapshot", side_effect=lambda *args, **kwargs: calls.append(("snapshot", None)) or "snap-1"),
            patch.object(priority_board_service, "compute_priority_board", side_effect=lambda *args, **kwargs: calls.append(("board", None)) or {"picks": [{"symbol": "NVDA"}]}),
            patch.object(prediction_ledger_service, "log_predictions", side_effect=log_predictions),
            patch.object(api_server, "_prewarm_track_leaders", side_effect=AssertionError("optional prewarm blocked persistence")),
        ):
            result = api_server._daily_post_scan_finalize(["ndx"])

        self.assertEqual([name for name, _ in calls], ["marker", "snapshot", "board", "ledger"])
        self.assertEqual(calls[-1][1], session.isoformat())
        self.assertEqual(result["prediction_ledger"]["slice_rows_written"], 1)

    def test_incomplete_price_ingest_does_not_advance_marker(self) -> None:
        def save(key, value):
            self.assertNotEqual(key, "market_calendar:last_synced_session", "sync marker must not advance")
        for ingest in (
            {"ok": True, "latest_data_date": "2026-09-19", "symbols_written": 1, "latest_session_symbols": 1},
            {"ok": True, "latest_data_date": "2026-09-22", "symbols_written": 1, "latest_session_symbols": 0},
        ):
            with (
                self.subTest(ingest=ingest),
                patch.object(market_calendar, "most_recent_session", return_value=date(2026, 9, 22)),
                patch.object(api_server, "cache_get", return_value=None),
                patch.object(api_server, "cache_set", side_effect=save),
                patch.object(api_server, "_daily_report_price_coverage", return_value={"current": 0, "total": 10, "ratio": 0, "missing_reports": []}),
                patch.object(ingest_grouped_daily, "ingest_recent_grouped_daily", return_value=ingest),
            ):
                with self.assertRaisesRegex(RuntimeError, "旧榜单保留"):
                    api_server._daily_post_scan_finalize(["ndx"])

    def test_existing_price_sync_still_repairs_missing_ledger(self) -> None:
        session = "2026-09-22"
        with (
            patch.object(market_calendar, "most_recent_session", return_value=date.fromisoformat(session)),
            patch.object(api_server, "cache_get", return_value=session),
            patch.object(api_server, "external_data_scope", return_value=nullcontext()),
            patch.object(ingest_grouped_daily, "ingest_recent_grouped_daily", side_effect=AssertionError("no new price sync")),
            patch.object(api_server, "_build_home_dashboard_snapshot_payload", return_value={"rows": [{"symbol": "NVDA"}]}),
            patch.object(api_server, "save_home_dashboard_snapshot", return_value="snap-2"),
            patch.object(priority_board_service, "compute_priority_board", return_value={"picks": [{"symbol": "NVDA"}]}),
            patch.object(prediction_ledger_service, "log_predictions", return_value={"slice_rows_written": 1}) as ledger,
        ):
            result = api_server._daily_post_scan_finalize(["ndx"])

        ledger.assert_called_once_with(30, as_of_date=session)
        self.assertFalse(result["market_data_fresh"])

    def test_current_cache_skips_rate_limited_grouped_request(self) -> None:
        session = "2026-09-23"
        with (
            patch.object(market_calendar, "most_recent_session", return_value=date.fromisoformat(session)),
            patch.object(api_server, "cache_get", return_value="2026-09-22"),
            patch.object(api_server, "cache_set") as cache_set,
            patch.object(api_server, "_daily_report_price_coverage", return_value={
                "total": 100, "current": 99, "ratio": 0.99,
                "stale_symbols": ["MISSING"], "missing_reports": [],
            }),
            patch.object(api_server, "external_data_scope", return_value=nullcontext()),
            patch.object(ingest_grouped_daily, "ingest_recent_grouped_daily", side_effect=AssertionError("Massive must not be called")),
            patch.object(api_server, "_build_home_dashboard_snapshot_payload", return_value={
                "rows": [{"symbol": "NVDA"}], "excluded_stale_count": 1, "price_coverage": 0.99,
            }),
            patch.object(api_server, "save_home_dashboard_snapshot", return_value="snap-cache"),
            patch.object(priority_board_service, "compute_priority_board", return_value={"picks": [{"symbol": "NVDA"}]}),
            patch.object(prediction_ledger_service, "log_predictions", return_value={"slice_rows_written": 1}),
        ):
            result = api_server._daily_post_scan_finalize(["ndx"])
        cache_set.assert_called_once_with("market_calendar:last_synced_session", session)
        self.assertEqual(result["excluded_stale_count"], 1)
        self.assertEqual(api_server._daily_finalize_outcome(result, False, 0)[0], "partial")

    def test_finalize_outcome_never_calls_timeout_completed(self) -> None:
        saved = {"snapshot_id": "snap-1", "snapshot_row_count": 2}
        self.assertEqual(api_server._daily_finalize_outcome(saved, False, 0)[0], "completed")
        self.assertEqual(api_server._daily_finalize_outcome(saved, False, 1)[0], "partial")
        self.assertEqual(api_server._daily_finalize_outcome(saved, True, 0)[0], "failed")
        self.assertEqual(api_server._daily_finalize_outcome({}, False, 0)[0], "failed")

    def test_legacy_timeout_record_is_not_reported_completed(self) -> None:
        with patch.object(api_server, "cache_get", return_value={
            "date": "2026-09-23", "status": "completed", "finalize_timed_out": True,
        }):
            record = api_server._auto_scan_last_record()
        self.assertEqual(record["status"], "failed")
        self.assertIn("timed out", record["error"])

    def test_grouped_daily_rate_limit_is_not_a_holiday(self) -> None:
        response = requests.Response()
        response.status_code = 429
        error = requests.HTTPError("rate limited", response=response)
        with patch.object(market_data_service, "_massive_get", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
                market_data_service.massive_grouped_daily("2026-09-23")

    def test_target_session_ingest_uses_one_date_and_unions_pools(self) -> None:
        bars = {
            symbol: {"open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 100}
            for symbol in ("AAPL", "NVDA")
        }
        def resolve(universe):
            return {"spx": ["AAPL"], "ndx": ["AAPL", "NVDA"]}[universe], "snapshot", {}

        with (
            patch.object(ingest_grouped_daily, "resolve_universe", side_effect=resolve),
            patch.object(ingest_grouped_daily.mds, "massive_grouped_daily", return_value=bars) as grouped,
            patch.object(ingest_grouped_daily.mds, "_read_daily_cache", return_value=pd.DataFrame()),
            patch.object(ingest_grouped_daily.mds, "_write_daily_cache") as write,
        ):
            result = ingest_grouped_daily.ingest_recent_grouped_daily(
                "spx", target_session="2026-09-23", universe_ids=["ndx"], sleep=0,
            )
        grouped.assert_called_once_with("2026-09-23")
        self.assertEqual(result["calls"], 1)
        self.assertEqual(result["symbols_in_universe"], 2)
        self.assertEqual(write.call_count, 2)

    def test_target_session_ingest_stops_on_rate_limit(self) -> None:
        with (
            patch.object(ingest_grouped_daily, "resolve_universe", return_value=(["AAPL"], "snapshot", {})),
            patch.object(ingest_grouped_daily.mds, "massive_grouped_daily", side_effect=RuntimeError("HTTP 429")) as grouped,
        ):
            result = ingest_grouped_daily.ingest_recent_grouped_daily(
                "spx", target_session="2026-09-23", sleep=0,
            )
        grouped.assert_called_once_with("2026-09-23")
        self.assertFalse(result["ok"])
        self.assertIn("429", result["error"])

    def test_snapshot_does_not_publish_stale_price_as_current(self) -> None:
        with (
            patch.object(api_server, "_ranked_research_universes", return_value=[{"universe": "ndx", "label": "Nasdaq-100"}]),
            patch.object(api_server, "portfolio_timing_gate", return_value={}),
            patch.object(api_server, "summarize_peer_earnings_history", return_value={}),
            patch.object(api_server, "data_source_status", return_value={}),
            patch.object(api_server, "_research_signal_contexts_for_universe", return_value=[{"symbol": "NVDA"}]),
            patch.object(api_server, "_research_context_sort_key", return_value=(0,)),
            patch.object(api_server, "_compact_research_context", return_value={
                "symbol": "NVDA", "price_as_of": "2026-09-22",
            }),
        ):
            with self.assertRaisesRegex(RuntimeError, "snapshot blocked"):
                api_server._build_home_dashboard_snapshot_payload(["ndx"], required_price_session="2026-09-23")

    def test_snapshot_excludes_small_stale_minority(self) -> None:
        rows = [{"symbol": f"T{i:02d}"} for i in range(20)]
        def compact(raw):
            return {"symbol": raw["symbol"], "price_as_of": "2026-09-22" if raw["symbol"] == "T19" else "2026-09-23"}
        with (
            patch.object(api_server, "_ranked_research_universes", return_value=[{"universe": "ndx", "label": "Nasdaq-100"}]),
            patch.object(api_server, "portfolio_timing_gate", return_value={}),
            patch.object(api_server, "summarize_peer_earnings_history", return_value={}),
            patch.object(api_server, "data_source_status", return_value={}),
            patch.object(api_server, "_research_signal_contexts_for_universe", return_value=rows),
            patch.object(api_server, "_research_context_sort_key", return_value=(0,)),
            patch.object(api_server, "_compact_research_context", side_effect=compact),
            patch.object(api_server, "review_top_contexts", return_value={}),
            patch.object(api_server, "validate_overnight_alpha_summary", return_value={}),
        ):
            result = api_server._build_home_dashboard_snapshot_payload(["ndx"], required_price_session="2026-09-23")
        self.assertEqual(result["excluded_stale_count"], 1)
        self.assertEqual(result["price_coverage"], 0.95)
        self.assertEqual(len(result["rows"]), 19)
        self.assertNotIn("T19", {row["symbol"] for row in result["rows"]})

    def test_explicit_partial_snapshot_publishes_only_current_candidates(self) -> None:
        rows = [{"symbol": "FRESH"}, {"symbol": "OLD"}]
        def compact(raw):
            return {"symbol": raw["symbol"], "price_as_of": "2026-09-23" if raw["symbol"] == "FRESH" else "2026-09-22"}
        with (
            patch.object(api_server, "_ranked_research_universes", return_value=[{"universe": "ndx", "label": "Nasdaq-100"}]),
            patch.object(api_server, "portfolio_timing_gate", return_value={}),
            patch.object(api_server, "summarize_peer_earnings_history", return_value={}),
            patch.object(api_server, "data_source_status", return_value={}),
            patch.object(api_server, "_research_signal_contexts_for_universe", return_value=rows),
            patch.object(api_server, "_research_context_sort_key", return_value=(0,)),
            patch.object(api_server, "_compact_research_context", side_effect=compact),
            patch.object(api_server, "review_top_contexts", return_value={}),
            patch.object(api_server, "validate_overnight_alpha_summary", return_value={}),
        ):
            with self.assertRaisesRegex(RuntimeError, "snapshot blocked"):
                api_server._build_home_dashboard_snapshot_payload(["ndx"], required_price_session="2026-09-23")
            result = api_server._build_home_dashboard_snapshot_payload(
                ["ndx"], required_price_session="2026-09-23", allow_partial_price_evidence=True,
            )
        self.assertEqual([row["symbol"] for row in result["rows"]], ["FRESH"])
        self.assertEqual(result["price_coverage"], 0.5)
        self.assertTrue(result["partial_price_evidence"])
        self.assertEqual(result["excluded_stale_symbols"], ["OLD"])

    def test_partial_snapshot_never_publishes_all_stale_candidates(self) -> None:
        with (
            patch.object(api_server, "_ranked_research_universes", return_value=[{"universe": "ndx"}]),
            patch.object(api_server, "portfolio_timing_gate", return_value={}),
            patch.object(api_server, "summarize_peer_earnings_history", return_value={}),
            patch.object(api_server, "data_source_status", return_value={}),
            patch.object(api_server, "_research_signal_contexts_for_universe", return_value=[{"symbol": "OLD"}]),
            patch.object(api_server, "_research_context_sort_key", return_value=(0,)),
            patch.object(api_server, "_compact_research_context", return_value={"symbol": "OLD", "price_as_of": "2026-09-22"}),
        ):
            with self.assertRaisesRegex(RuntimeError, "snapshot blocked"):
                api_server._build_home_dashboard_snapshot_payload(
                    ["ndx"], required_price_session="2026-09-23", allow_partial_price_evidence=True,
                )

    def test_finalize_only_reuses_pool_results_without_restarting_scan(self) -> None:
        with (
            patch.object(api_server, "_auto_scan_universe_ids", return_value=["ndx", "spx"]),
            patch.object(api_server, "_daily_post_scan_finalize", return_value={
                "snapshot_id": "snap-recovered", "snapshot_row_count": 20,
                "market_data_fresh": False, "timings": {},
            }) as finalize,
            patch.object(api_server, "_run_research_signal_hub_job", side_effect=AssertionError("pool scan restarted")),
            patch.object(api_server, "cache_set") as cache_set,
            patch.object(api_server, "_daily_post_scan_optional_enrichment"),
        ):
            api_server._run_daily_finalize_only()
        finalize.assert_called_once_with(["ndx", "spx"])
        self.assertEqual(cache_set.call_args.args[1]["snapshot_id"], "snap-recovered")
        self.assertEqual(cache_set.call_args.args[1]["status"], "completed")

    def test_finalize_only_partial_preserves_completed_pool_count(self) -> None:
        with (
            patch.object(api_server, "_auto_scan_universe_ids", return_value=["ndx", "spx"]),
            patch.object(api_server, "_auto_scan_last_record", return_value={
                "date": api_server._auto_scan_today_key(), "completed_universe_count": 2,
            }),
            patch.object(api_server, "_daily_post_scan_finalize", return_value={
                "snapshot_id": "snap-partial", "snapshot_row_count": 20,
                "excluded_stale_count": 1, "timings": {},
            }),
            patch.object(api_server, "cache_set") as cache_set,
        ):
            api_server._run_daily_finalize_only()
        record = cache_set.call_args.args[1]
        self.assertEqual(record["status"], "partial")
        self.assertEqual(record["completed_universe_count"], 2)


if __name__ == "__main__":
    unittest.main()
