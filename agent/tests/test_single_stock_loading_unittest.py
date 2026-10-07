from __future__ import annotations

import asyncio
import threading
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

import pandas as pd

import api_server as api
import market_data_service as market
import news_sentiment_service as news


class SingleStockLoadingTests(unittest.TestCase):
    def test_page_cache_is_scoped_and_cache_only(self):
        records = {}

        def build(symbol, universe, period, **kwargs):
            self.assertFalse(market.external_data_allowed())
            self.assertFalse(kwargs["include_context"])
            return {"symbol": symbol, "day_change": {"as_of": "2026-09-28"}}

        with (
            patch.object(api, "cache_get", side_effect=records.get),
            patch.object(api, "cache_set", side_effect=lambda k, v, **kw: records.update({k: v})),
            patch.object(api, "_build_single_stock_payload", side_effect=build) as builder,
        ):
            first = api._single_stock_cached_payload("NVDA", "auto", "2y")
            again = api._single_stock_cached_payload("NVDA", "auto", "2y")
            self.assertEqual(first["page_revision"], again["page_revision"])
            self.assertTrue(again["page_cache_hit"])
            self.assertEqual(again["day_change"]["as_of"], "2026-09-28")
            api._single_stock_cached_payload("NVDA", "ndx", "2y")
            api._single_stock_cached_payload("NVDA", "auto", "6mo")
            self.assertEqual(builder.call_count, 3)

    def test_background_refresh_is_deduplicated_and_releases_slot(self):
        active = set()
        slots = threading.BoundedSemaphore(1)
        records = {}
        with (
            patch.object(api, "_SSO_REFRESH_ACTIVE", active),
            patch.object(api, "_SSO_REFRESH_SLOTS", slots),
            patch.object(api, "cache_get", side_effect=records.get),
            patch.object(api, "cache_set", side_effect=lambda k, v, **kw: records.update({k: v})),
            patch.object(api.threading, "Thread") as thread,
            patch.object(api, "get_daily_history"), patch.object(api, "get_next_earnings"),
            patch.object(news, "news_digest"),
            patch.object(api, "_build_single_stock_payload", return_value={"symbol": "NVDA"}),
            patch.object(api, "_single_stock_summary_lazy", return_value={}),
            patch.object(api, "_warm_single_stock_enrichment"),
        ):
            self.assertTrue(api._start_single_stock_refresh("NVDA", "auto", "2y"))
            self.assertFalse(api._start_single_stock_refresh("NVDA", "auto", "2y"))
            self.assertFalse(api._start_single_stock_refresh("AAPL", "auto", "2y"))
            self.assertEqual(thread.call_count, 1)
            thread.call_args.kwargs["target"]()
            self.assertFalse(active)
            self.assertTrue(slots.acquire(blocking=False))
            slots.release()
            page = records[api._single_stock_page_key("NVDA", "auto", "2y")]
            self.assertIn("page_revision", page)
            self.assertFalse(api._start_single_stock_refresh("NVDA", "auto", "2y"))

    def test_failed_worker_keeps_existing_page_and_unlocks(self):
        key = api._single_stock_page_key("NVDA", "auto", "2y")
        records = {key: {"symbol": "NVDA", "page_revision": "previous"}}
        slots = threading.BoundedSemaphore(1)
        active = set()
        with (
            patch.object(api, "_SSO_REFRESH_ACTIVE", active),
            patch.object(api, "_SSO_REFRESH_SLOTS", slots),
            patch.object(api, "cache_get", side_effect=records.get),
            patch.object(api, "cache_set", side_effect=lambda k, v, **kw: records.update({k: v})),
            patch.object(api.threading, "Thread") as thread,
            patch.object(api, "get_daily_history", side_effect=RuntimeError("provider unavailable")),
            patch.object(news, "news_digest", side_effect=RuntimeError("provider unavailable")),
        ):
            api._start_single_stock_refresh("NVDA", "auto", "2y")
            thread.call_args.kwargs["target"]()
            self.assertEqual(records[key]["page_revision"], "previous")
            self.assertEqual(records[key + ":refresh"]["status"], "unavailable")
            self.assertFalse(active)
            self.assertTrue(slots.acquire(blocking=False))

    def test_news_cache_miss_does_not_fetch_or_store_fake_empty_news(self):
        with (
            market.external_data_scope(False), patch.object(news, "cache_get", return_value=None),
            patch.object(news, "cache_set") as store, patch.object(market, "_massive_get") as fetch,
        ):
            out = news.news_digest("NVDA")
        self.assertEqual(out["status"], "loading")
        fetch.assert_not_called()
        store.assert_not_called()

    def test_all_core_sections_are_cache_only_even_for_unknown_symbol(self):
        frame = pd.DataFrame({"Open": [99.] * 80, "High": [102.] * 80,
                              "Low": [98.] * 80, "Close": [100.] * 80, "Volume": [100000.] * 80},
                             index=pd.bdate_range("2026-06-01", periods=80))
        calls = []

        def section(*args, **kwargs):
            calls.append(market.external_data_allowed())
            return {}

        def history(*args, **kwargs):
            self.assertFalse(market.external_data_allowed())
            return frame, "cache:ohlcv"

        with ExitStack() as stack:
            stack.enter_context(patch.object(api, "get_daily_history", side_effect=history))
            stack.enter_context(patch.object(api, "_single_stock_row_from_snapshot", return_value=None))
            for name in ("validate_symbol_overnight_alpha", "_single_stock_correlation_pack",
                         "_single_option_iv", "_single_open_action_plan", "_single_company_profile",
                         "get_next_earnings", "_single_three_month_comparison", "_track_leader_compare",
                         "_build_symbol_research_context"):
                stack.enter_context(patch.object(api, name, side_effect=section))
            stack.enter_context(patch.object(api, "_compact_research_context", side_effect=lambda x: x))
            stack.enter_context(patch("priority_board_service.single_stock_signal_read", side_effect=section))
            stack.enter_context(patch.object(news, "news_digest", side_effect=section))
            out = api._build_single_stock_payload("NVDA", "auto", "2y")
            with patch.object(api, "_build_symbol_research_context") as context:
                first = api._build_single_stock_payload("NVDA", "auto", "2y", include_context=False)
                context.assert_not_called()
                self.assertEqual(first["row"]["context_status"], "loading")
        self.assertTrue(calls)
        self.assertFalse(any(calls))
        self.assertEqual(out["day_change"]["price"], 100.)

    def test_enrich_only_sends_changed_revision(self):
        page = {"symbol": "NVDA", "page_revision": "new"}
        key = api._single_stock_page_key("NVDA", "auto", "2y")
        records = {key: page, key + ":refresh": {"status": "completed"}}
        with (
            patch.object(api, "cache_get", side_effect=records.get),
            patch.object(api, "_single_company_profile", return_value={}),
            patch.object(api, "_track_leader_compare", return_value={}),
            patch.object(api, "_single_option_iv", return_value={}),
            patch.object(api, "get_next_earnings", return_value={}),
        ):
            old = asyncio.run(api.single_stock_overnight_enrich("NVDA", "auto", "2y", "old"))
            same = asyncio.run(api.single_stock_overnight_enrich("NVDA", "auto", "2y", "new"))
        self.assertEqual(old["core_payload"], page)
        self.assertIsNone(same["core_payload"])
        self.assertTrue(same["done"])

    def test_summary_cache_miss_only_starts_one_worker(self):
        with (
            patch.object(api, "_SSO_SUMMARY_ACTIVE", set()),
            patch.object(api, "cache_get", return_value=None), patch.object(api, "cache_set"),
            patch.object(api.threading, "Thread") as thread,
        ):
            payload = {"symbol": "NVDA"}
            api._single_stock_summary_lazy(payload)
            api._single_stock_summary_lazy(payload)
            self.assertEqual(thread.call_count, 1)

    def test_daily_chart_uses_existing_ohlcv_without_provider_probe(self):
        frame = pd.DataFrame({"Open": [99., 100.], "High": [102., 103.], "Low": [98., 99.],
                              "Close": [100., 101.], "Volume": [100., 120.]},
                             index=pd.to_datetime(["2026-09-28", "2026-09-29"]))
        with (
            patch.object(api, "cache_get", return_value=None), patch.object(api, "cache_set"),
            patch.object(api, "get_daily_history", return_value=(frame, "cache:ohlcv")),
            patch.object(market, "_massive_get") as fetch,
            patch("scripts.research_signal_framework_backtest._collect_symbol_events", return_value=[]),
        ):
            out = api._single_candles("NVDA", "daily")
        fetch.assert_not_called()
        self.assertTrue(out["available"])
        self.assertEqual(out["as_of_date"], "2026-09-29")
        self.assertEqual(out["source"], "cache:ohlcv")

    def test_cold_profile_does_not_cache_an_empty_ai_profile(self):
        with (
            market.external_data_scope(False), patch.object(api, "cache_get", return_value=None),
            patch.object(api, "cache_set") as store,
            patch.object(api, "_single_company_facts") as facts,
        ):
            out = api._single_company_profile("NVDA")
        self.assertEqual(out["status"], "loading")
        facts.assert_not_called()
        store.assert_not_called()

    def test_cold_peer_comparison_does_not_loop_over_peer_prices(self):
        with (
            market.external_data_scope(False), patch.object(api, "cache_get", return_value=None),
            patch.object(api, "get_daily_history") as prices,
        ):
            out = api._track_leader_compare("NVDA", {"peer_tickers": ["NVDA", "AMD"]})
        self.assertEqual(out["status"], "loading")
        prices.assert_not_called()


if __name__ == "__main__":
    unittest.main()
