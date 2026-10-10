import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import app_database as db
import supply_chain_graph_service as graph


def frame():
    dates = pd.bdate_range("2026-05-01", "2026-10-08")
    return pd.DataFrame({"Close": range(100, 100 + len(dates))}, index=dates).astype(float)


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stopall)
        patch.object(db, "DB_PATH", Path(self.tmp.name) / "test.sqlite3").start()
        patch.object(db, "_INITIALIZED", False).start()
        patch.object(graph, "_ACTIVE", set()).start()
        patch.object(graph, "most_recent_session", return_value=pd.Timestamp("2026-10-08").date()).start()
        db.ensure_database()

    def test_raw_return_has_actual_dates_and_excludes_future(self):
        data = frame()
        data.loc[pd.Timestamp("2026-10-09"), "Close"] = 999999
        result = graph.price_metrics(data, "2026-10-08")
        self.assertEqual(result["price_date"], "2026-10-08")
        self.assertEqual(result["return_start_date"], "2026-07-08")
        self.assertEqual(result["price_basis"], "raw_unadjusted")
        self.assertGreater(result["return_3m_pct"], 0)

    def test_missing_or_short_history_does_not_invent_return(self):
        self.assertIsNone(graph.price_metrics(pd.DataFrame(), "2026-10-08")["close"])
        result = graph.price_metrics(frame().tail(10), "2026-10-08")
        self.assertIsNone(result["return_3m_pct"])
        self.assertIsNotNone(result["close"])

    def test_suspicious_split_like_jump_suppresses_return(self):
        data = frame()
        data.loc[data.index >= "2026-09-01", "Close"] /= 10
        self.assertEqual(graph.price_metrics(data, "2026-10-08")["return_status"], "price_jump_needs_review")

    def test_supply_links_are_directional_and_industry_is_not_competitor(self):
        company = {"relationships": {
            "upstream_suppliers": [{"name": "Supplier", "symbol": "FLEX", "market": "US", "listing_verified": True}],
            "downstream_customers": [{"name": "Client", "symbol": "MSFT", "market": "US", "listing_verified": True, "relationship_status": "disclosed"}],
            "competitors": [{"name": "Competitor", "symbol": "ANET", "market": "US", "listing_verified": True}]},
            "industry_peers": [{"name": "Apple", "symbol": "AAPL", "market": "US", "listing_verified": True}]}
        with patch.object(graph.network, "cached_network", return_value=company), \
                patch.object(graph.gil, "cached_equity", return_value={}), \
                patch.object(graph, "get_daily_history", return_value=(pd.DataFrame(), "cache")) as prices:
            result = graph.build_graph("CSCO")
        root = next(n["id"] for n in result["nodes"] if n["role"] == "target")
        upstream = next(e for e in result["edges"] if e["role"] == "upstream")
        downstream = next(e for e in result["edges"] if e["role"] == "downstream")
        rival = next(e for e in result["edges"] if e["role"] == "competitor")
        self.assertEqual(upstream["target"], root)
        self.assertEqual(downstream["source"], root)
        self.assertFalse(upstream["verified"])
        self.assertTrue(downstream["verified"])
        self.assertFalse(rival["directed"])
        industry = next(n["id"] for n in result["nodes"] if n["role"] == "industry")
        self.assertFalse(any(industry in (e["source"], e["target"]) for e in result["edges"]))
        self.assertTrue(all(call.kwargs["skip_massive"] for call in prices.call_args_list))

    def test_foreign_and_unverified_symbols_never_request_us_prices(self):
        rows = [{"name": "Foreign", "symbol": "601138.SH", "market": "CN", "listing_verified": True},
                {"name": "Unverified", "symbol": "JNPR", "market": "US", "listing_verified": False}]
        with patch.object(graph.network, "cached_network", return_value={"relationships": {"competitors": rows}}), \
                patch.object(graph.gil, "cached_equity", return_value={}), \
                patch.object(graph, "get_daily_history", return_value=(pd.DataFrame(), "cache")) as prices:
            result = graph.build_graph("CSCO")
        self.assertEqual([call.args[0] for call in prices.call_args_list], ["CSCO"])
        self.assertEqual(next(n for n in result["nodes"] if n.get("market") == "CN")["metrics"], {})

    def test_one_bad_history_cache_does_not_discard_company_graph(self):
        company = {"relationships": {"upstream_suppliers": [{"name": "Flex", "symbol": "FLEX", "market": "US", "listing_verified": True}]}}
        with patch.object(graph.network, "cached_network", return_value=company), \
                patch.object(graph.gil, "cached_equity", return_value={}), \
                patch.object(graph, "get_daily_history", side_effect=[ValueError("bad cache"), (frame(), "cache")]):
            result = graph.build_graph("CSCO")
        self.assertEqual(len(result["edges"]), 1)
        self.assertIsNone(result["nodes"][0]["metrics"]["close"])
        self.assertIsNotNone(result["nodes"][1]["metrics"]["return_3m_pct"])

    def test_future_reference_is_not_used_and_negative_pe_is_not_zero(self):
        with patch.object(graph.network, "cached_network", return_value={}), \
                patch.object(graph.gil, "cached_equity", return_value={"as_of": "2026-10-09", "pe": 999}), \
                patch.object(graph, "get_daily_history", return_value=(pd.DataFrame(), "cache")):
            self.assertIsNone(graph.build_graph("CSCO")["nodes"][0]["metrics"]["pe"])
        with patch.object(graph.network, "cached_network", return_value={}), \
                patch.object(graph.gil, "cached_equity", return_value={"as_of": "2026-10-08", "pe": -2}), \
                patch.object(graph, "get_daily_history", return_value=(pd.DataFrame(), "cache")):
            self.assertEqual(graph.build_graph("CSCO")["nodes"][0]["metrics"]["pe"], -2)

    def test_refresh_deduplicates_caps_batch_and_uses_existing_slots(self):
        slots = threading.BoundedSemaphore(1)
        with patch.object(graph.threading, "Thread") as thread, \
                patch.object(graph, "build_graph", return_value={"as_of": "2026-10-08", "needs_metrics": [f"A{i}" for i in range(30)]}), \
                patch.object(graph.gil, "refresh_references", return_value={"status": "completed"}) as refresh:
            self.assertTrue(graph.start_refresh("CSCO", slots)["started"])
            self.assertFalse(graph.start_refresh("CSCO", slots)["started"])
            self.assertEqual(graph.start_refresh("MSFT", slots)["status"], "busy")
            thread.call_args.kwargs["target"]()
        self.assertEqual(len(refresh.call_args.args[0]), 20)
        self.assertTrue(slots.acquire(blocking=False))
        self.assertFalse(graph.start_refresh("CSCO", slots)["started"])

    def test_failure_releases_capacity_and_preserves_company_cache(self):
        old = {"relationships": {"competitors": [{"name": "Arista"}]}}
        db.cache_set("company_network:v1:CSCO", old)
        slots = threading.BoundedSemaphore(1)
        with patch.object(graph.threading, "Thread") as thread, \
                patch.object(graph, "build_graph", side_effect=RuntimeError("secret-token")):
            graph.start_refresh("CSCO", slots)
            thread.call_args.kwargs["target"]()
        self.assertEqual(db.cache_get("company_network:v1:CSCO"), old)
        self.assertNotIn("secret-token", str(db.cache_get("supply_chain:refresh:v1:CSCO")))
        self.assertTrue(slots.acquire(blocking=False))


if __name__ == "__main__":
    unittest.main()
