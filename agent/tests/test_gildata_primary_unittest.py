from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

import app_database as db
import gildata_shadow_service as gil
import market_data_service as market
import macro_panic_service as macro


def table(name, header, *rows):
    return {"api_name": name, "table_markdown": "\n".join([header, "|" + "|".join("---" for _ in header.strip("|").split("|")) + "|", *rows])}


def observations():
    return [
        table("美股日行情", "|证券代码|交易日期|币种|总市值(万元)|开盘价(元)|最高价(元)|最低价(元)|收盘价(元)|昨收价(元)|成交量(万股)|成交额(万元)|",
              "|AAPL|2026-10-07|USD|491342258|336.96|338.67|332.78|336.67|333.63|3414.79|1147320.10|"),
        table("美股价值分析", "|证券代码|交易日|总市值(亿元)|市盈率PE|", "|AAPL|2026-10-07|49134.2258|-|"),
        table("美股机构评级", "|股票代码|截止日期|统计周期(天)|买入评级机构数(个)|增持评级机构数(个)|中性评级机构数(个)|减持评级机构数(个)|卖出评级机构数(个)|评级机构总数(个)|",
              "|AAPL|2026-10-07|100|19|8|13|2|3|45|"),
        table("美股盈利预测", "|股票代码|截止日期|预测指标|预测报告期|预测数据币种|预测数据单位|预测值平均数|统计周期(天)|预测次数(次)|",
              "|AAPL|2026-10-07|目标价|-|美元|元|335.75|100|35|",
              "|AAPL|2026-10-07|每股收益|2027-09-30|美元|元/股|9.61|100|39|"),
    ]


class GilDataPrimaryTests(unittest.TestCase):
    def setUp(self):
        self.records = {}
        self.samples = {}
        self.addCleanup(patch.stopall)
        patch.dict(gil.os.environ, {"GILDATA_REFERENCE_PRIMARY": "1", "GILDATA_MCP_TOKEN": "test", "GILDATA_VIX_FALLBACK": "1"}).start()
        patch.object(db, "cache_get", side_effect=lambda k: self.records.get(k)).start()
        patch.object(db, "cache_set", side_effect=lambda k, v, **kwargs: self.records.update({k: v})).start()
        patch.object(gil, "cached_equity", side_effect=lambda s: self.samples.get(s)).start()
        patch.object(gil, "_write_sample", side_effect=lambda s, v: self.samples.update({s: v})).start()
        patch("market_calendar.most_recent_session", return_value=date(2026, 10, 7)).start()

    def test_batch_units_and_date_are_checked(self):
        sample = gil.normalize_equity("AAPL", "2026-10-07", observations(), observations())
        self.assertEqual(sample["market_cap_usd"], 4_913_422_580_000)
        self.assertEqual(sample["daily_quote"]["volume"], 34_147_900)
        self.assertAlmostEqual(sample["daily_quote"]["turnover_usd"], 11_473_201_000)
        self.assertEqual(sample["daily_quote"]["adjustment"], "unspecified")
        self.assertFalse(sample["daily_quote"]["is_realtime"])
        self.assertIsNone(sample["pe"])
        self.assertEqual(sample["annual_eps_estimates"][0]["report_period"], "2027-09-30")
        wrong = gil.normalize_equity("NVDA", "2026-10-07", observations(), observations())
        self.assertNotIn("daily_quote", wrong)
        self.assertEqual(wrong["available_fields"], [])

    def test_exact_date_filter_precedes_table_row_limit(self):
        old_rows = ["|MSFT|2020-01-01|10|"] * 5001
        records = table("sample", "|股票代码|截止日期|数值|", *old_rows, "|NVDA|2026-10-07|62|")
        found = gil._table([records], "sample", "截止日期", "2026-10-07")
        self.assertEqual(found, [{"股票代码": "NVDA", "截止日期": "2026-10-07", "数值": "62"}])

    def test_multiple_same_api_tables_are_combined(self):
        first = table("sample", "|股票代码|", "|AAPL|")
        second = table("sample", "|股票代码|", "|NVDA|")
        self.assertEqual(gil._table([first, second], "sample"), [{"股票代码": "AAPL"}, {"股票代码": "NVDA"}])

    def test_invalid_ohlc_and_non_usd_not_accepted(self):
        rows = observations()
        rows[0]["table_markdown"] = rows[0]["table_markdown"].replace("|338.67|", "|330.0|")
        self.assertNotIn("daily_quote", gil.normalize_equity("AAPL", "2026-10-07", rows, rows))
        rows[0]["table_markdown"] = rows[0]["table_markdown"].replace("|USD|", "|CNY|")
        self.assertNotIn("market_cap_usd", gil.normalize_equity("AAPL", "2026-10-07", rows, rows))

    def test_background_batch_deduplicates_requests(self):
        with patch.object(gil, "_finquery", return_value=(observations(), {})) as query:
            first = gil.refresh_references(["AAPL", "AAPL"], "2026-10-07")
            again = gil.refresh_references(["AAPL"], "2026-10-07")
        self.assertEqual(first["written"], 1)
        self.assertEqual(again["status"], "cached")
        self.assertEqual(query.call_count, 1)
        self.assertEqual(self.samples["AAPL"]["ratings"]["buy"], 19)

    def test_failure_is_cooled_and_previous_cache_kept(self):
        self.samples["AAPL"] = {"as_of": "2026-10-06", "market_cap_usd": 100}
        with patch.object(gil, "_finquery", side_effect=gil.requests.Timeout("secret-url-must-not-appear")) as query:
            failure = gil.refresh_references(["AAPL"], "2026-10-07")
            again = gil.refresh_references(["AAPL"], "2026-10-07")
        self.assertEqual(failure["status"], "unavailable")
        self.assertNotIn("secret", str(failure))
        self.assertEqual(self.samples["AAPL"]["as_of"], "2026-10-06")
        self.assertEqual(query.call_count, 1)
        self.assertEqual(again["status"], "cached")

    def test_cache_only_never_fetches(self):
        with market.external_data_scope(False), patch.object(gil, "_finquery") as query:
            result = gil.refresh_references(["AAPL"], "2026-10-07")
        self.assertEqual(result["status"], "disabled_or_cache_only")
        query.assert_not_called()

    def test_primary_cap_replaces_massive_reference_not_history(self):
        self.samples["AAPL"] = gil.normalize_equity("AAPL", "2026-10-07", observations(), observations())
        with patch.object(market, "_read_json", return_value=None), patch.object(market, "_massive_get") as massive, market.external_data_scope(False):
            result = market.get_market_cap_snapshot("AAPL", refresh=True)
        self.assertTrue(result["source"].startswith("gildata"))
        self.assertEqual(result["data_as_of_date"], "2026-10-07")
        massive.assert_not_called()

    def test_stale_gil_cap_falls_back_to_existing_source(self):
        self.samples["AAPL"] = {"as_of": "2026-10-06", "market_cap_usd": 100}
        with patch.object(gil, "refresh_references", return_value={}), patch.object(market, "_read_json", return_value=None), \
                patch.object(market, "_write_json"), patch.object(market, "_massive_get", return_value={"results": {"market_cap": 500}}):
            result = market.get_market_cap_snapshot("AAPL", refresh=True)
        self.assertEqual(result["source"], "massive:reference")

    def test_primary_ratings_keep_original_categories_and_target_window(self):
        self.samples["AAPL"] = gil.normalize_equity("AAPL", "2026-10-07", observations(), observations())
        with market.external_data_scope(False), patch.object(market, "_fmp_get") as fmp:
            result = market.get_analyst_view("AAPL")
        self.assertEqual(result["source"], "gildata:FinQuery")
        self.assertEqual(result["ratings"]["buy"], 19)
        self.assertEqual(result["target_window_days"], 100)
        self.assertNotIn("target_avg_quarter", result)
        fmp.assert_not_called()

    def test_cold_profile_exposes_cached_reference_without_llm_or_network(self):
        import api_server as api
        self.samples["AAPL"] = gil.normalize_equity("AAPL", "2026-10-07", observations(), observations())
        with market.external_data_scope(False), patch.object(market, "_read_json", return_value=None), \
                patch.object(api, "cache_get", return_value=None), patch.object(gil, "_finquery") as query:
            result = api._single_company_profile("AAPL")
        self.assertEqual(result["status"], "reference_only")
        self.assertTrue(result["available"])
        self.assertEqual(result["gildata_research"]["as_of"], "2026-10-07")
        self.assertEqual(result["facts"]["market_cap"], 4_913_422_580_000)
        query.assert_not_called()

    def test_vix_primary_does_not_use_cboe_when_valid(self):
        with patch.object(gil, "refresh_vix", return_value={"value": 18.4, "as_of": "2026-10-07"}), \
                patch.object(macro, "_write_cache"), patch.object(macro, "get_cboe_vix_latest") as cboe:
            result = macro.get_vix_regime(force_refresh=True)
        self.assertEqual(result["value"], 18.4)
        self.assertEqual(result["data_as_of_date"], "2026-10-07")
        cboe.assert_not_called()

    def test_news_semantic_hits_are_not_publishable(self):
        bad = [{"title": "齐鲁高速月报", "table_markdown": "港股新闻"}]
        with patch.object(gil, "_query", return_value=(bad, {})):
            result = gil.probe_us_news("2026-10-01", "2026-10-08")
        self.assertEqual(result["accepted_count"], 0)
        self.assertFalse(result["primary_enabled"])

    def test_disabled_primary_restores_legacy_routing(self):
        with patch.dict(gil.os.environ, {"GILDATA_REFERENCE_PRIMARY": "0"}), patch.object(market, "_fmp_get", return_value=[]), \
                patch.object(gil, "_finquery") as query:
            self.assertFalse(market.get_analyst_view("AAPL")["available"])
        query.assert_not_called()

    def test_oversized_batch_fails_before_network(self):
        with patch.object(gil, "_finquery") as query:
            with self.assertRaises(ValueError):
                gil.refresh_references([f"T{i}" for i in range(21)], "2026-10-07")
        query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
