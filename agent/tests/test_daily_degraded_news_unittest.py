from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
import unittest
from contextlib import contextmanager
from datetime import date
from unittest.mock import patch

import api_server as api
import deepseek_decision_assistant as review
import peer_earnings_signal_service as peer
import premarket_news_service as news


class DailyDegradedNewsTests(unittest.TestCase):
    def test_partial_prices_continue_without_advancing_sync_marker(self):
        records = {}
        with (
            patch.object(api, "_daily_sync_prices_strict", side_effect=RuntimeError("429")),
            patch("market_calendar.most_recent_session", return_value=date(2026, 9, 29)),
            patch.object(api, "_daily_report_price_coverage", return_value={"current": 3, "total": 10}),
            patch.object(api, "cache_get", side_effect=lambda k: records.get(k)),
            patch.object(api, "cache_set", side_effect=lambda k, v: records.update({k: v})),
        ):
            out = {}
            self.assertEqual(api._daily_sync_prices(["ndx"], out, allow_partial=True), "2026-09-29")
            self.assertTrue(out["data_warnings"])
            self.assertNotIn("market_calendar:last_synced_session", records)
            self.assertEqual(api._daily_finalize_outcome({**out, "snapshot_id": "partial", "snapshot_row_count": 3}, False, 0)[0], "partial")

    def test_zero_current_prices_fail_fast_instead_of_publishing_old_data(self):
        with (
            patch.object(api, "_daily_sync_prices_strict", side_effect=RuntimeError("429")),
            patch("market_calendar.most_recent_session", return_value=date(2026, 9, 29)),
            patch.object(api, "_daily_report_price_coverage", return_value={"current": 0, "total": 10}),
            patch.object(api, "cache_get", return_value=None), patch.object(api, "cache_set"),
        ):
            with self.assertRaisesRegex(RuntimeError, "旧榜单保留"):
                api._daily_sync_prices(["ndx"], {}, allow_partial=True)

    def test_paused_source_is_not_retried_during_finalize(self):
        with (
            patch.object(api, "cache_get", return_value={"session": "2026-09-29", "failed_at": api.time.time()}),
            patch.object(api, "cache_set"),
            patch("market_calendar.most_recent_session", return_value=date(2026, 9, 29)),
            patch.object(api, "_daily_report_price_coverage", return_value={"current": 3, "total": 10}),
            patch.object(api, "_daily_sync_prices_strict") as sync,
        ):
            api._daily_sync_prices(["ndx"], {}, allow_partial=True)
        sync.assert_not_called()

    def test_news_refresh_is_hard_bounded_and_shared(self):
        lock = threading.Lock()
        lock.acquire()
        with patch.object(api, "_PREMARKET_NEWS_REFRESH_LOCK", lock), patch.object(api.subprocess, "run") as run:
            self.assertEqual(api._refresh_premarket_news_bounded()["status"], "busy")
        run.assert_not_called()
        lock.release()
        with (
            patch.object(api, "_PREMARKET_NEWS_REFRESH_LOCK", lock),
            patch.object(api.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps({"status": "completed", "written": 2}), "")) as run,
        ):
            self.assertEqual(api._refresh_premarket_news_bounded(translate_top=0)["written"], 2)
            self.assertLessEqual(run.call_args.kwargs["timeout"], 600)
            self.assertEqual(json.loads(run.call_args.kwargs["input"]), {"translate_top": 0})
        self.assertFalse(lock.locked())

    def test_news_timeout_releases_lock_and_keeps_existing_data(self):
        lock = threading.Lock()
        with (
            patch.object(api, "_PREMARKET_NEWS_REFRESH_LOCK", lock),
            patch.object(api.subprocess, "run", side_effect=subprocess.TimeoutExpired("news", 1)),
            patch("app_database.connection", side_effect=RuntimeError("no rows")),
        ):
            self.assertEqual(api._refresh_premarket_news_bounded()["status"], "timeout")
        self.assertFalse(lock.locked())

    def test_cached_review_never_calls_llm_on_a_miss(self):
        with patch.object(review, "get_cached_review", return_value=None), patch.object(review, "_llm_review_only") as llm:
            self.assertEqual(review.review_top_contexts([{"symbol": "NVDA"}], cache_only=True), {})
        llm.assert_not_called()

    def test_missing_event_is_unknown_not_clear(self):
        with (
            patch.object(api, "_latest_launch_signals", {}), patch.object(api, "_latest_event_signals", {}),
            patch.object(api, "get_vix_regime", return_value={}), patch.object(api, "portfolio_timing_gate", return_value={}),
            patch.object(api, "_equity_hypothesis_test_plan", return_value={}),
            patch.object(api, "stock_panic_proxy", return_value={}),
            patch.object(api, "_research_evidence_summary", return_value={}),
            patch.object(api, "_apply_unified_evidence_to_hypothesis", return_value={}),
            patch.object(api, "get_cached_review", return_value=None),
        ):
            context = api._build_symbol_research_context("NVDA", scan_universe="ndx")
        self.assertFalse(context["dimensions"]["event_clear"])
        self.assertIn("事件证据未知", context["conflicts"][0])

    def test_peer_cache_miss_never_fetches_calendars(self):
        with patch.object(peer, "_memory_cache", {}), patch.object(peer, "_earnings_record") as fetch:
            result = peer.scan_peer_earnings_signals(cache_only=True, target_symbols=["NVDA"])
        self.assertEqual(result["evidence_status"], "unknown")
        fetch.assert_not_called()

    def test_daily_hub_defers_network_evidence_and_completes_technical_work(self):
        job = {"daily_price_cache_only": True}
        def scan(**kwargs):
            self.assertFalse(api.external_data_allowed())
            self.assertTrue(kwargs["cache_only"])
            return {"signals": [], "n_signals": 0}
        with (
            patch.object(api, "_research_signal_hub_jobs", {"test": job}),
            patch.object(api, "_stock_signal_report_reuse_status", return_value={"reusable": True}),
            patch.object(api, "_stock_signal_observation_pool", return_value=[{"ticker": "NVDA"}]),
            patch.object(api, "scan_launch_signals", side_effect=scan), patch.object(api, "_store_launch_result"),
            patch.object(api, "_event_scan_is_fresh", return_value=False),
            patch.object(api, "_run_event_driven_scan_sync") as events,
            patch.object(api, "portfolio_timing_gate", return_value={}), patch.object(api, "get_vix_regime", return_value={}),
            patch.object(api, "_build_symbol_research_context", return_value={"symbol": "NVDA"}),
            patch.object(api, "_research_context_sort_key", return_value=(0,)),
            patch.object(api, "review_top_contexts", return_value={}) as llm,
        ):
            api._run_research_signal_hub_job("test", api.ResearchSignalHubRunRequest(universe="ndx", refresh_evidence=False))
        self.assertEqual(job["status"], "completed")
        self.assertTrue(job["result"]["optional_evidence_pending"])
        self.assertTrue(job["result"]["data_warnings"])
        events.assert_not_called()
        self.assertTrue(llm.call_args.kwargs["cache_only"])
        self.assertTrue(api.external_data_allowed())

    def test_news_is_stored_before_yahoo_and_llm_and_preserves_feedback(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        @contextmanager
        def connection():
            yield db
        article = {"title": "NVDA raises guidance", "published_utc": "2026-09-29T20:00:00+00:00", "tickers": ["NVDA"]}
        item = {"news_id": "n1", "symbol": "NVDA", "title_original": article["title"], "published_utc": article["published_utc"]}
        def yahoo(*args):
            self.assertEqual(db.execute("SELECT COUNT(*) FROM premarket_news_items").fetchone()[0], 1)
            return []
        def llm(*args, **kwargs):
            self.assertEqual(db.execute("SELECT COUNT(*) FROM premarket_news_items").fetchone()[0], 1)
            return {"title_cn": "上调指引", "status": "translated"}
        with (
            patch.object(news, "connection", connection), patch.object(news, "ensure_database"),
            patch.object(news, "_universe_symbols", return_value=({"NVDA"}, {}, {}, [], [])),
            patch.object(news, "_fetch_massive_news", return_value=([article], {})),
            patch.object(news, "_fetch_yahoo_news", side_effect=yahoo),
            patch.object(news, "_build_item", return_value=item),
            patch.object(news, "_apply_source_mix_cap", side_effect=lambda rows, top: rows),
            patch.object(news, "enrich_news_llm", side_effect=llm), patch.object(news, "warm_news_embeddings"),
        ):
            result = news.refresh_premarket_news(start_utc="2026-09-29T00:00:00+00:00", end_utc="2026-09-30T00:00:00+00:00", translate_top=1)
            self.assertEqual(result["status"], "completed")
            db.execute("INSERT INTO premarket_news_feedback(news_id,symbol,decision,created_at,updated_at) VALUES('n1','NVDA','important','now','now')")
            news._store_items([{"news_id": "n1", "symbol": "NVDA", "title_original": "raw"}])
            self.assertEqual(db.execute("SELECT title_cn FROM premarket_news_items").fetchone()[0], "上调指引")
            self.assertEqual(db.execute("SELECT decision FROM premarket_news_feedback").fetchone()[0], "important")
            with (
                patch.object(news, "cache_get", return_value=None), patch.object(news, "cache_set"),
                patch.object(news, "_embed_enabled", return_value=True),
                patch.object(news, "_embed_text", return_value=None) as embed,
            ):
                news.build_preference_model()
                self.assertFalse(embed.call_args.kwargs["allow_fetch"])
        db.close()


if __name__ == "__main__":
    unittest.main()
