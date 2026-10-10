import copy
import unittest
from unittest.mock import patch

import company_network_service as network
import market_data_service as market


def tables():
    return [{"api_name": "美股公司简介", "table_markdown":
        "|股票代码|公司英文名称|公司英文简称|所属FactSet行业|所属SIC行业|链接地址|\n"
        "|---|---|---|---|---|---|\n"
        "|AAOI|Applied Optoelectronics, Inc.|Applied Optoelectronics|semiconductor|3674|https://ao-inc.com|\n"
        "|COHR|Coherent Corp.|Coherent|semiconductor|3674|https://coherent.com|\n"
        "|MSFT|Microsoft Corporation|Microsoft|software|7372|https://microsoft.com|"},
        {"api_name": "公司简介", "table_markdown":
        "|聚源代码|股票名称|中文名称|英文名称|上市日期|公司网站|\n|---|---|---|---|---|---|\n"
        "|300502.SZ|新易盛|成都新易盛通信技术股份有限公司|Eoptolink Technology Inc., Ltd.|2016-03-03|https://eoptolink.com|"}]


def directory():
    return {"AAOI": {"name": "Applied Optoelectronics, Inc. - Common Stock", "etf": "N"},
            "COHR": {"name": "Coherent Corp. - Common Stock", "etf": "N"},
            "MSFT": {"name": "Microsoft Corporation - Common Stock", "etf": "N"}}


class CompanyNetworkTests(unittest.TestCase):
    def test_current_us_listing_and_name_are_both_required(self):
        rows = network.parse_identities(tables(), directory())
        self.assertTrue(network.resolve_company("Microsoft", rows)["listing_verified"])
        self.assertEqual(network.resolve_company("Coherent", rows)["symbol"], "COHR")
        self.assertFalse(network.resolve_company("Coherent", network.parse_identities(tables()))["listing_verified"])

    def test_wrong_company_at_same_ticker_is_not_linkable(self):
        listings = directory()
        listings["COHR"]["name"] = "Different Corporation - Common Stock"
        self.assertFalse(network.resolve_company("Coherent", network.parse_identities(tables(), listings))["listing_verified"])

    def test_foreign_company_keeps_market_code_and_website(self):
        company = network.resolve_company("新易盛（Eoptolink）", network.parse_identities(tables(), directory()))
        self.assertEqual(company["market"], "CN")
        self.assertEqual(company["symbol"], "300502.SZ")
        self.assertEqual(company["website"], "https://eoptolink.com")

    def test_hong_kong_introduction_does_not_certify_listing(self):
        raw = [{"api_name": "港股公司简介", "table_markdown":
                "|股票代码|股票名称|公司名称|\n|---|---|---|\n|03308|中际旭创|中际旭创股份有限公司|"}]
        self.assertEqual(network.parse_identities(raw), [])
        raw.append({"api_name": "港股IPO发行情况", "table_markdown":
                    "|股票代码|上市状态|\n|---|---|\n|03308|上市|"})
        self.assertEqual(network.parse_identities(raw)[0]["market"], "HK")

    def test_ambiguous_company_does_not_receive_a_random_ticker(self):
        rows = [{"name": "Alpha Company", "aliases": ["Alpha"], "market": "US", "symbol": "AAA"},
                {"name": "Alpha Holdings", "aliases": ["Alpha"], "market": "US", "symbol": "BBB"}]
        self.assertIsNone(network.resolve_company("Alpha", rows))

    def test_brand_alone_is_not_silently_assigned_to_parent(self):
        identity = {"name": "Cisco Systems", "aliases": ["Cisco Systems"], "market": "US", "symbol": "CSCO"}
        self.assertIsNone(network.resolve_company("Acacia", [identity]))
        self.assertEqual(network.resolve_company("Cisco（Acacia）", [identity])["symbol"], "CSCO")
        other = {"name": "Acacia Research", "aliases": ["Acacia Research"], "market": "US", "symbol": "ACTG"}
        self.assertEqual(network.resolve_company("Cisco（Acacia）", [identity, other])["symbol"], "CSCO")

    def test_categories_are_not_displayed_as_specific_companies(self):
        names = ["射频/通信芯片供应商（推断）", "互联网数据中心运营商", "电信及FTTH设备商", "暂无"]
        for name in names:
            with self.subTest(name=name):
                self.assertIsNone(network._company_name(name))
        self.assertEqual(network._company_name("Source Photonics"), "Source Photonics")

    def test_urls_cannot_be_javascript_credentials_or_local_addresses(self):
        for url in ["javascript:alert(1)", "http://127.0.0.1", "http://router.local", "https://u:p@company.com", "http://[::1]"]:
            with self.subTest(url=url):
                self.assertIsNone(network._url(url))
        self.assertEqual(network._url("https://company.com/investors"), "https://company.com/investors")

    def test_declared_company_deduplicates_legacy_without_promoting_ai_claim(self):
        evidence = {"downstream_customers": [{"name": "Microsoft", "detail": "2025 report"}],
                    "source_url": "https://sec.gov/test", "disclosed_at": "2026-02-26"}
        profile = {"downstream_customers": ["Microsoft Corporation", "Unknown Company", "互联网运营商"]}
        old = copy.deepcopy(profile)
        with patch.object(network, "relation_evidence", return_value=evidence):
            rows, omitted, _ = network.relation_rows("AAOI", profile, network.parse_identities(tables(), directory()))
        self.assertEqual(len(rows["downstream_customers"]), 2)
        self.assertEqual(rows["downstream_customers"][0]["relationship_status"], "disclosed")
        self.assertEqual(rows["downstream_customers"][1]["relationship_status"], "ai_unverified")
        self.assertEqual(omitted["downstream_customers"], 1)
        self.assertEqual(profile, old)

    def test_industry_market_caps_sort_descending_and_require_equal_date(self):
        company = {"factset_industry": "semi", "sic_industry": "3674"}
        members = [{"symbol": s, "name": s, **company} for s in ["AAOI", "AAA", "BBB", "CCC"]]
        caps = {"AAA": {"as_of": "2026-10-08", "market_cap_usd": 1e9},
                "BBB": {"as_of": "2026-10-08", "market_cap_usd": 8e9},
                "CCC": {"as_of": "2026-10-07", "market_cap_usd": 99e9}}
        rows = network.industry_rows("AAOI", company, members, caps, ["AAA"], "2026-10-08")
        self.assertEqual([r["symbol"] for r in rows], ["BBB", "AAA"])
        self.assertEqual(rows[0]["peer_type"], "industry_reference")
        self.assertEqual(rows[1]["peer_type"], "curated_track_peer")
        self.assertNotIn("probability", rows[0])

    def test_missing_industry_is_not_an_industry_match(self):
        self.assertEqual(network.industry_rows("AAOI", {}, [{"symbol": "COHR", "name": "Coherent"}],
            {"COHR": {"as_of": "2026-10-08", "market_cap_usd": 9e9}}, [], "2026-10-08"), [])

    def test_cache_only_and_disabled_mode_never_fetch(self):
        with market.external_data_scope(False), patch.object(network.gil, "_query") as query:
            network.refresh_network("AAOI", {}, "2026-10-08")
        query.assert_not_called()
        with patch.object(network.gil, "research_enabled", return_value=False), patch.object(network.gil, "_query") as query:
            network.refresh_network("AAOI", {}, "2026-10-08")
        query.assert_not_called()

    def test_cached_network_is_read_only_and_marks_old_as_of(self):
        with patch.object(network, "cache_get", side_effect=[{"as_of": "2026-10-07", "industry_peers": []}, {}]), \
                patch.object(network.gil, "_query") as query:
            result = network.cached_network("AAOI", {"market_session": "2026-10-08"})
        self.assertTrue(result["stale"])
        query.assert_not_called()

    def test_refresh_uses_existing_cache_and_archives_without_changing_scores(self):
        values = {"company_network:us_listings:v1": directory()}
        stored = [network.gil.normalize_company(s, tables()) for s in ["AAOI", "COHR"]]
        caps = [{"api_name": "美股日行情", "table_markdown":
                 "|证券代码|交易日期|总市值(万元)|币种|\n|---|---|---|---|\n|COHR|2026-10-08|900000|USD|"},
                {"api_name": "美股价值分析", "table_markdown":
                 "|证券代码|交易日|总市值(亿元)|\n|---|---|---|\n|COHR|2026-10-08|90|"}]
        with patch.object(network.gil, "research_enabled", return_value=True), \
                patch.object(network, "cache_get", side_effect=lambda key: values.get(key)), \
                patch.object(network, "cache_set", side_effect=lambda key, value, **kwargs: values.update({key: value})), \
                patch.object(network, "_stored_companies", return_value=stored), \
                patch.object(network.gil, "_query", side_effect=[(tables(), {}), (caps, {})]) as query, \
                patch.object(network, "reference_evidence_append"):
            result = network.refresh_network("AAOI", {"competitors": ["Coherent"]}, "2026-10-08")
            again = network.refresh_network("AAOI", {"competitors": ["Coherent"]}, "2026-10-08")
        self.assertEqual(query.call_count, 2)
        self.assertEqual(again["status"], "cached_or_running")
        self.assertEqual(result["industry_peers"][0]["market_cap_usd"], 9e9)
        self.assertTrue(result["does_not_change_scores"])

    def test_failed_refresh_preserves_previous_industry_evidence(self):
        previous = [{"symbol": "COHR", "market_cap_date": "2026-10-07", "market_cap_usd": 9e9}]
        values = {"company_network:us_listings:v1": directory(), "company_network:v1:AAOI": {"industry_peers": previous}}
        stored = [network.gil.normalize_company(s, tables()) for s in ["AAOI", "COHR"]]
        with patch.object(network.gil, "research_enabled", return_value=True), \
                patch.object(network, "cache_get", side_effect=lambda key: values.get(key)), \
                patch.object(network, "cache_set", side_effect=lambda key, value, **kwargs: values.update({key: value})), \
                patch.object(network, "_stored_companies", return_value=stored), \
                patch.object(network.gil, "_query", side_effect=TimeoutError("private-token")), \
                patch.object(network, "reference_evidence_append"):
            result = network.refresh_network("AAOI", {}, "2026-10-08")
        self.assertEqual(result["industry_peers"], previous)
        self.assertEqual(result["status"], "partial")
        self.assertNotIn("private-token", str(result))

    def test_official_directory_preserves_us_navigation_when_mcp_times_out(self):
        values = {"company_network:us_listings:v1": directory()}
        stored = [network.gil.normalize_company(s, tables()) for s in ["AAOI", "COHR"]]
        with patch.object(network.gil, "research_enabled", return_value=True), \
                patch.object(network, "cache_get", side_effect=lambda key: values.get(key)), \
                patch.object(network, "cache_set", side_effect=lambda key, value, **kwargs: values.update({key: value})), \
                patch.object(network, "_stored_companies", return_value=stored), \
                patch.object(network.gil, "_query", side_effect=TimeoutError("private-token")), \
                patch.object(network, "reference_evidence_append"):
            result = network.refresh_network("AAOI", {"downstream_customers": ["Microsoft"]}, "2026-10-08")
        company = next(r for r in result["relationships"]["downstream_customers"] if r.get("symbol") == "MSFT")
        self.assertTrue(company["listing_verified"])
        self.assertEqual(company["identity_source"], "nasdaq:symbol_directory")
        self.assertNotIn("private-token", str(result))

    def test_late_profile_populates_empty_relationship_cache_without_fetch(self):
        old = {"as_of": "2026-10-08", "relationships": {kind: [] for kind in network.RELATIONS}}
        identities = network.parse_identities(tables(), directory())
        values = {"company_network:v1:AAOI": old, "company_network:identities:v1": identities}
        with patch.object(network, "cache_get", side_effect=values.get), patch.object(network.gil, "_query") as query:
            result = network.cached_network("AAOI", {"ai_available": True, "competitors": ["Coherent"], "market_session": "2026-10-08"})
        self.assertEqual(result["relationships"]["competitors"][0]["symbol"], "COHR")
        self.assertEqual(old["relationships"]["competitors"], [])
        query.assert_not_called()

    def test_retry_does_not_duplicate_running_network(self):
        with patch.object(network.gil, "research_enabled", return_value=True), \
                patch.object(network, "cache_get", side_effect=lambda k: {"status": "running"} if ":attempt:" in k else {}), \
                patch.object(network.gil, "_query") as query:
            result = network.refresh_network("AAOI", {}, "2026-10-08", retry=True)
        self.assertEqual(result["status"], "cached_or_running")
        query.assert_not_called()

    def test_failed_retry_keeps_previous_relationships(self):
        old_rows = [{"display_name": "Coherent", "symbol": "COHR"}]
        values = {"company_network:us_listings:v1": directory(),
                  "company_network:v1:AAOI": {"as_of": "2026-10-07", "relationships": {"competitors": old_rows}}}
        with patch.object(network.gil, "research_enabled", return_value=True), \
                patch.object(network, "cache_get", side_effect=values.get), \
                patch.object(network, "cache_set", side_effect=lambda k, v, **kw: values.update({k: v})), \
                patch.object(network, "_stored_companies", return_value=[]), \
                patch.object(network, "relation_evidence", return_value={}), \
                patch.object(network.gil, "_query", side_effect=TimeoutError()), patch.object(network, "reference_evidence_append"):
            result = network.refresh_network("AAOI", {}, "2026-10-08", retry=True)
        self.assertEqual(result["relationships"]["competitors"], old_rows)
        self.assertEqual(result["relationships_retained_as_of"], "2026-10-07")


if __name__ == "__main__":
    unittest.main()
