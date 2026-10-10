import asyncio
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import api_server as api
import company_network_service as network
import gildata_shadow_service as gil
import market_data_service as market


class CompanyRefreshTests(unittest.TestCase):
    def test_company_retry_deduplicates_and_never_runs_market_or_signals(self):
        records, active = {}, set()
        slots = threading.BoundedSemaphore(1)
        with patch.object(api, "_SSO_COMPANY_ACTIVE", active), patch.object(api, "_SSO_REFRESH_ACTIVE", set()), \
                patch.object(api, "_SSO_REFRESH_SLOTS", slots), patch.object(api, "cache_get", side_effect=records.get), \
                patch.object(api, "cache_set", side_effect=lambda k, v, **kw: records.update({k: v})), \
                patch.object(api.threading, "Thread") as thread, \
                patch.object(api, "_single_company_profile", return_value={"ai_available": True}), \
                patch.object(gil, "refresh_research"), \
                patch.object(network, "refresh_network", return_value={"status": "completed"}) as refresh, \
                patch.object(api, "get_daily_history") as prices, patch.object(api, "_build_single_stock_payload") as signals:
            self.assertTrue(api._start_single_company_refresh("PTC")["started"])
            self.assertEqual(records["single_company_refresh:v1:PTC"]["status"], "queued")
            self.assertFalse(api._start_single_company_refresh("PTC")["started"])
            thread.call_args.kwargs["target"]()
            self.assertEqual(records["single_company_refresh:v1:PTC"]["status"], "completed")
            self.assertEqual(api._start_single_company_refresh("PTC")["status"], "cooldown")
            self.assertFalse(active)
            self.assertTrue(slots.acquire(blocking=False))
        self.assertTrue(refresh.call_args.kwargs["retry"])
        prices.assert_not_called()
        signals.assert_not_called()

    def test_failure_keeps_valid_payload_and_releases_slot(self):
        old = {"relationships": {"competitors": [{"symbol": "ADSK"}]}}
        records = {"company_network:v1:PTC": old}
        slots = threading.BoundedSemaphore(1)
        with patch.object(api, "_SSO_COMPANY_ACTIVE", set()), patch.object(api, "_SSO_REFRESH_ACTIVE", set()), \
                patch.object(api, "_SSO_REFRESH_SLOTS", slots), patch.object(api, "cache_get", side_effect=records.get), \
                patch.object(api, "cache_set", side_effect=lambda k, v, **kw: records.update({k: v})), \
                patch.object(api.threading, "Thread") as thread, patch.object(gil, "refresh_research"), \
                patch.object(api, "_single_company_profile", return_value={"ai_available": True}), \
                patch.object(network, "refresh_network", side_effect=TimeoutError("secret-token")):
            api._start_single_company_refresh("PTC")
            thread.call_args.kwargs["target"]()
        self.assertEqual(records["company_network:v1:PTC"], old)
        self.assertEqual(records["single_company_refresh:v1:PTC"]["error"], "TimeoutError")
        self.assertNotIn("secret-token", str(records))
        self.assertTrue(slots.acquire(blocking=False))

    def test_existing_core_refresh_blocks_duplicate_company_worker(self):
        with patch.object(api, "_SSO_COMPANY_ACTIVE", set()), \
                patch.object(api, "_SSO_REFRESH_ACTIVE", {api._single_stock_page_key("PTC", "auto", "2y")}), \
                patch.object(api, "cache_get", return_value=None), patch.object(api.threading, "Thread") as thread:
            self.assertEqual(api._start_single_company_refresh("PTC")["status"], "running")
        thread.assert_not_called()

    def test_company_only_ai_retry_reuses_cached_facts_without_market_fetch(self):
        cached = {"available": True, "ai_available": False, "facts": {"name": "PTC", "market_cap": 10e9}}
        records = {"single_company_profile:v3:PTC": cached}
        answer = SimpleNamespace(content=json.dumps({"competitors": ["Autodesk"]}))
        with patch.object(api, "cache_get", side_effect=records.get), \
                patch.object(api, "cache_set", side_effect=lambda k, v, **kw: records.update({k: v})), \
                patch.object(api, "_single_profile_research_overlay", side_effect=lambda s, p: p), \
                patch.object(api, "_single_company_facts") as facts, \
                patch.object(api, "_single_enrich_company_facts") as enrich, \
                patch("src.providers.chat.ChatLLM") as llm:
            llm.return_value.chat.return_value = answer
            result = api._single_company_profile("PTC", retry_ai=True)
        self.assertTrue(result["ai_available"])
        self.assertEqual(result["facts"]["market_cap"], 10e9)
        self.assertEqual(result["competitors"], ["Autodesk"])
        facts.assert_not_called()
        enrich.assert_not_called()

    def test_poll_continues_for_company_worker_after_core_completed(self):
        key = api._single_stock_page_key("PTC", "auto", "2y")
        records = {key + ":refresh": {"status": "completed"}, "single_company_refresh:v1:PTC": {"status": "queued"}}
        with patch.object(api, "cache_get", side_effect=records.get), \
                patch.object(api, "_single_company_profile", return_value={}), patch.object(api, "_track_leader_compare", return_value={}), \
                patch.object(api, "_single_option_iv", return_value={}), patch.object(api, "get_next_earnings", return_value={}):
            running = asyncio.run(api.single_stock_overnight_enrich("PTC", "auto", "2y", ""))
            records["single_company_refresh:v1:PTC"] = {"status": "completed"}
            finished = asyncio.run(api.single_stock_overnight_enrich("PTC", "auto", "2y", ""))
        self.assertFalse(running["done"])
        self.assertTrue(finished["done"])


if __name__ == "__main__":
    unittest.main()
