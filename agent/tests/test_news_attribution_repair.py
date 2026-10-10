import copy
import sqlite3
import threading
import unittest
from datetime import datetime, timezone
from contextlib import contextmanager
from unittest.mock import patch

import pandas as pd
import premarket_news_service as news
import company_relationship_research as relations
import company_network_service as network


class NewsAttributionTests(unittest.TestCase):
    def test_product_owner_not_retail_channel(self):
        article = {"data_source": "gildata:news", "tickers": ["AMZN"],
                   "title": "苹果最新款Mac在亚马逊上迎来史上最大降价"}
        self.assertEqual(news._subject_symbols(article), {"AAPL"})
        self.assertEqual(news._primary_symbol(article, ["AMZN", "AAPL"]), "AAPL")

    def test_chinese_names_not_queried_ticker(self):
        article = {"data_source": "gildata:news", "tickers": ["AMZN"], "title": "英伟达发布季度财报"}
        self.assertEqual(news._subject_symbols(article), {"NVDA"})

    def test_two_real_subjects_and_channel_only_story(self):
        self.assertEqual(news._subject_symbols({"data_source": "gildata:news", "tickers": ["AMZN"],
            "title": "苹果与亚马逊达成云计算合作协议"}), {"AAPL", "AMZN"})
        self.assertEqual(news._subject_symbols({"data_source": "gildata:news", "tickers": ["AMZN"],
            "title": "亚马逊公布年度财报"}), {"AMZN"})

    def test_english_word_boundary_does_not_match_pineapple(self):
        self.assertNotIn("AAPL", news._mentioned_brands("Pineapple exports rose"))

    def test_one_bar_does_not_invent_previous_close(self):
        frame = pd.DataFrame({"Close": [100.]}, index=pd.to_datetime(["2026-10-08"]))
        with patch.object(news, "cache_get", return_value=None), patch.object(news, "cache_set"), \
                patch.object(news, "get_daily_history", return_value=(frame, "cache:test")), \
                patch("gildata_shadow_service.cached_equity", return_value=None):
            price = news._price_snapshot("AAPL", force_refresh=True)
        self.assertEqual(price["quote_as_of"], "2026-10-08")
        self.assertIsNone(price["previous_close"])
        self.assertIsNone(price["change_pct"])

    def test_gildata_fallback_keeps_date_and_no_fake_atr(self):
        daily = {"daily_quote": {"as_of": "2026-10-08", "currency": "USD", "close": 100., "previous_close": 99.}}
        with patch.object(news, "cache_get", return_value=None), patch.object(news, "cache_set"), \
                patch.object(news, "get_daily_history", side_effect=RuntimeError()), \
                patch("gildata_shadow_service.cached_equity", return_value=daily):
            price = news._price_snapshot("AAPL", force_refresh=True)
        self.assertEqual(price["current_price"], 100.)
        self.assertIsNone(price["atr_pct"])
        self.assertFalse(price["quote_is_realtime"])

    def test_symbol_correction_clears_wrong_quotes_preserves_feedback(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        @contextmanager
        def conn():
            yield db
        with patch.object(news, "connection", conn), patch.object(news, "ensure_database"), \
                patch.object(news, "cache_get", return_value=None):
            old = news._build_item({"id": "repair", "title": "苹果Mac在亚马逊上降价", "tickers": ["AMZN"],
                "data_source": "gildata:news", "published_utc": "2026-10-08T00:00:00+00:00"}, "AMZN", {}, {}, enrich_metadata=False)
            old.update(current_price=999, previous_close=998)
            news._store_items([old])
            db.execute("INSERT INTO premarket_news_feedback(news_id,symbol,decision,created_at,updated_at) VALUES(?,?,'important','now','now')", (old["news_id"], "AMZN"))
            new = copy.deepcopy(old)
            new.update(symbol="AAPL", company_name="Apple", current_price=None, previous_close=None)
            news._store_items([new])
            self.assertEqual(db.execute("SELECT symbol,current_price FROM premarket_news_items").fetchone()[0], "AAPL")
            self.assertIsNone(db.execute("SELECT current_price FROM premarket_news_items").fetchone()[0])
            self.assertEqual(db.execute("SELECT decision FROM premarket_news_feedback").fetchone()[0], "important")
        db.close()

    def test_background_job_dedup_and_slot_release(self):
        slots = threading.BoundedSemaphore(1)
        records = {}
        with patch.object(news, "cache_get", side_effect=records.get), \
                patch.object(news, "cache_set", side_effect=lambda k,v,**kw: records.update({k:v})), \
                patch.object(news.threading, "Thread") as thread, \
                patch.object(news, "enrich_existing_queue", side_effect=RuntimeError()):
            self.assertTrue(news.start_queue_enrichment(slots)["started"])
            self.assertFalse(news.start_queue_enrichment(slots)["started"])
            thread.call_args.kwargs["target"]()
            self.assertEqual(records["premarket_news:metadata_status"]["status"], "partial")
            self.assertTrue(slots.acquire(blocking=False))
            slots.release()

    def test_existing_low_priority_snippet_repaired_in_place_without_llm_or_new_news(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        records = {}
        @contextmanager
        def conn():
            yield db
        frame = pd.DataFrame({"Close": [100.]*30, "High": [103.]*30, "Low": [97.]*30},
                             index=pd.date_range(end="2026-10-08", periods=30))
        with patch.object(news, "connection", conn), patch.object(news, "ensure_database"), \
                patch.object(news, "cache_get", side_effect=records.get), \
                patch.object(news, "cache_set", side_effect=lambda k,v,**kw: records.update({k:v})), \
                patch.object(news, "_universe_symbols", return_value=({"AAPL", "AMZN"}, {"AAPL":["spx"]}, {"AAPL":["S&P500"]}, [], [])), \
                patch.object(news, "get_daily_history", return_value=(frame, "cache:test")), \
                patch("gildata_shadow_service.cached_equity", return_value=None), \
                patch("gildata_shadow_service.cached_research", return_value={}), \
                patch("gildata_shadow_service.refresh_references") as fetch, \
                patch.object(news, "enrich_news_llm") as llm:
            item = news._build_item({"title": "苹果Mac在亚马逊上降价", "tickers":["AMZN"], "data_source":"gildata:news",
                "published_utc":datetime.now(timezone.utc).isoformat()}, "AMZN", {}, {}, enrich_metadata=False)
            item["news_id"] = "legacy-id-preserved"
            news._store_items([item])
            db.execute("INSERT INTO premarket_news_feedback(news_id,symbol,decision,created_at,updated_at) VALUES('legacy-id-preserved','AMZN','important','now','now')")
            result = news.enrich_existing_queue()
            rows = db.execute("SELECT * FROM premarket_news_items").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["news_id"], "legacy-id-preserved")
            self.assertEqual(rows[0]["symbol"], "AAPL")
            self.assertEqual(rows[0]["current_price"], 100.)
            self.assertIsNone(rows[0]["estimated_gap_pct"])
            self.assertAlmostEqual(rows[0]["impact_band_pct"], .06)
            self.assertEqual(result["attribution_repaired"], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM premarket_news_feedback").fetchone()[0], 1)
            fetch.assert_not_called()
            llm.assert_not_called()
        db.close()


class RelationshipEvidenceTests(unittest.TestCase):
    def test_pdf_pages_are_explicitly_closed_without_context_protocol(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        textpage = SimpleNamespace(get_text_range=lambda:"Cisco lists Flex as a supplier.", close=MagicMock())
        page = SimpleNamespace(get_textpage=lambda:textpage, close=MagicMock())
        document = MagicMock()
        document.__enter__.return_value = document
        document.__len__.return_value = 1
        document.__getitem__.return_value = page
        response = MagicMock()
        response.status_code = 200
        response.headers = {"Content-Type":"application/pdf"}
        response.iter_content.return_value = [b'PDF fixture']
        response.__enter__.return_value = response
        with patch.object(relations.requests.utils, "get_environ_proxies", return_value={"https":"http://configured-proxy"}), \
                patch.object(relations.requests, "get", return_value=response), \
                patch("pypdfium2.PdfDocument", return_value=document):
            self.assertIn("Flex", relations._document("https://cisco.com/test.pdf", {"cisco.com"}))
        page.close.assert_called_once()
        textpage.close.assert_called_once()

    def test_direct_private_dns_is_rejected(self):
        addresses = [(2,1,6,'',('127.0.0.1',443))]
        with patch.object(relations.requests.utils, "get_environ_proxies", return_value={}), \
                patch.object(relations.socket, "getaddrinfo", return_value=addresses), \
                patch.object(relations.requests, "get") as fetch:
            with self.assertRaisesRegex(ValueError, "non_public_source"):
                relations._document("https://cisco.com/test", {"cisco.com"})
            fetch.assert_not_called()

    def test_configured_https_proxy_uses_proxy_resolution_not_local_fake_ip(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.status_code = 200
        response.headers = {"Content-Type":"text/html"}
        response.encoding = "utf-8"
        response.iter_content.return_value = [b'<p>Official corporate disclosure</p>']
        response.__enter__.return_value = response
        with patch.object(relations.requests.utils, "get_environ_proxies", return_value={"https":"http://configured-proxy"}), \
                patch.object(relations.socket, "getaddrinfo") as dns, \
                patch.object(relations.requests, "get", return_value=response) as fetch:
            self.assertIn("Official corporate", relations._document("https://cisco.com/test", {"cisco.com"}))
            dns.assert_not_called()
            self.assertFalse(fetch.call_args.kwargs["allow_redirects"])
            self.assertNotIn("verify", fetch.call_args.kwargs)  # requests default verification is retained.

    def test_fake_dns_mapping_is_limited_to_curated_source_not_vendor_url_or_lan(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.status_code = 200
        response.headers = {"Content-Type":"text/html"}
        response.encoding = "utf-8"
        response.iter_content.return_value = [b'Known official source']
        response.__enter__.return_value = response
        with patch.object(relations.requests.utils, "get_environ_proxies", return_value={}), \
                patch.object(relations.socket, "getaddrinfo", return_value=[(2,1,6,'',('198.18.0.2',443)), (10,1,6,'',('2001:2::77',443))]), \
                patch.object(relations.requests, "get", return_value=response):
            with self.assertRaises(ValueError):
                relations._document("https://cisco.com/test", {"cisco.com"})
            self.assertIn("Known official", relations._document("https://cisco.com/test", {"cisco.com"}, curated=True))
        with patch.object(relations.requests.utils, "get_environ_proxies", return_value={}), \
                patch.object(relations.socket, "getaddrinfo", return_value=[(2,1,6,'',('192.168.1.2',443))]):
            with self.assertRaises(ValueError):
                relations._document("https://cisco.com/test", {"cisco.com"}, curated=True)

    def test_official_host_only_no_lookalike_token_or_internal_url(self):
        for url in ["http://cisco.com/x", "https://cisco.com.evil.test/x", "https://localhost/x", "https://cisco.com/x?token=secret", "https://user@cisco.com/x"]:
            self.assertFalse(relations._allowed(url, {"cisco.com"}))
        self.assertTrue(relations._allowed("https://newsroom.cisco.com/release", {"cisco.com"}))

    def test_only_literal_source_quote_can_support_relationship(self):
        quote = "Cisco names Flex as a manufacturing supplier."
        sources = [{"id": "1", "url": "https://cisco.com/x", "text": quote}]
        row = {"name": "Flex", "kind": "upstream_suppliers", "source_id": "1", "source_quote": quote}
        self.assertEqual(relations._validate([row], sources, ["Flex"])["upstream_suppliers"][0]["relationship_status"], "source_supported")
        for change in ({"source_quote": "Fabricated Flex contract worth one billion dollars."}, {"name": "Jabil"}, {"kind": "partner"}, {"source_id": "99"}):
            self.assertFalse(any(relations._validate([{**row, **change}], sources, ["Flex"]).values()))

    def test_quote_about_someone_else_not_promoted(self):
        text = "Cisco names Flex as a manufacturing supplier."
        self.assertFalse(any(relations._validate([{"name": "Jabil", "kind": "upstream_suppliers", "source_id": "1", "source_quote": text}],
            [{"id": "1", "url": "https://cisco.com/x", "text": text}], ["Jabil"]).values()))

    def test_per_relation_source_overrides_document_defaults(self):
        with patch.object(network, "cache_get", return_value=None), patch.object(network, "relation_evidence", return_value={
            "source_url": "https://example.com/default", "source_title": "Annual report",
            "upstream_suppliers": [{"name": "Flex", "source_url": "https://flex.com/disclosure", "source_title": "Supplier award"}]}):
            groups, _, _ = network.relation_rows("CSCO", {}, [])
        self.assertEqual(groups["upstream_suppliers"][0]["source_url"], "https://flex.com/disclosure")

    def test_cisco_curated_source_keeps_historical_caveat(self):
        value = network.relation_evidence("CSCO")
        self.assertIn("FY2025", value["report_period"])
        self.assertIn("不再合作", value["upstream_note"])
        self.assertIn("Flex", [r["name"] for r in value["upstream_suppliers"]])
        self.assertNotIn("Jabil", [r["name"] for r in value["upstream_suppliers"]])
