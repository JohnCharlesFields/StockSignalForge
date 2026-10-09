from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import app_database as db
import gildata_shadow_service as gil
import market_data_service as market
import premarket_news_service as news
import deepseek_decision_assistant as assistant


def table(name, columns, *rows):
    return {"api_name": name, "table_markdown": "\n".join(["|" + "|".join(columns) + "|",
            "|" + "|".join("---" for _ in columns) + "|", *("|" + "|".join(r) + "|" for r in rows)])}


def forecasts(stamp="2026-10-07", mean="10", period="2027-01-31", window="100", unit="元/股"):
    return [table("美股盈利预测", ["股票代码", "截止日期", "预测指标", "预测报告期", "统计周期(天)",
        "预测数据币种", "预测数据单位", "预测值平均数", "预测值标准差", "预测次数(次)", "预测值调高次数", "预测值调低次数"],
        ["NVDA", stamp, "每股收益", period, window, "美元", unit, mean, "2", "20", "12", "3"],
        ["NVDA", stamp, "营业收入", period, window, "美元", "百万元", "400000", "10000", "40", "25", "2"])]


def company():
    return [table("美股公司简介", ["股票代码", "公司英文名称", "股票简称", "业务简介", "所属FactSet行业", "链接地址"],
                 ["NVDA", "NVIDIA Corp.", "英伟达", "设计芯片", "电子科技-半导体", "https://nvidia.com"])]


def article(title="英伟达发布新产品", stamp="2026-10-07 10:00:00"):
    return [{"api_name": "资讯舆情库", "title": title,
             "table_markdown": f"报告标题：{title}；\n撰写时间：{stamp}；\n新闻舆情来源：上海证券报\n原文：英伟达发布新产品，仍需核验。"}]


class GilDataResearchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(patch.stopall)
        patch.object(db, "DB_PATH", Path(tmp.name) / "test.sqlite3").start()
        patch.object(db, "_INITIALIZED", False).start()
        patch.dict(gil.os.environ, {"GILDATA_REFERENCE_PRIMARY": "1", "GILDATA_RESEARCH_ENABLED": "1",
                                  "GILDATA_NEWS_SUPPLEMENT": "1", "GILDATA_MCP_TOKEN": "test"}).start()

    def test_company_exact_identity_and_unsafe_url(self):
        self.assertIsNone(gil.normalize_company("AAPL", company()))
        data = company()
        data[0]["table_markdown"] = data[0]["table_markdown"].replace("https://nvidia.com", "javascript:bad")
        self.assertIsNone(gil.normalize_company("NVDA", data)["website"])
        self.assertIsNone(gil.normalize_company("NVDA", data)["vendor_updated_at"])

    def test_forecast_units_and_dispersion_not_probability(self):
        eps, revenue = gil.normalize_forecasts("NVDA", "2026-10-07", forecasts())
        self.assertEqual(eps["relative_dispersion"], .2)
        self.assertEqual(eps["revision_balance"], .6)
        self.assertEqual(revenue["mean"], 400_000_000_000)
        self.assertNotIn("probability", eps)
        self.assertEqual(gil.normalize_forecasts("AAPL", "2026-10-07", forecasts()), [])
        self.assertEqual(gil.normalize_forecasts("NVDA", "2026-10-06", forecasts()), [])

    def test_unknown_unit_zero_mean_and_non_usd(self):
        rows = gil.normalize_forecasts("NVDA", "2026-10-07", forecasts(mean="0"))
        self.assertIsNone(rows[0]["relative_dispersion"])
        self.assertEqual(len(gil.normalize_forecasts("NVDA", "2026-10-07", forecasts(unit="未知"))), 1)
        data = forecasts()
        data[0]["table_markdown"] = data[0]["table_markdown"].replace("美元", "人民币")
        self.assertEqual(gil.normalize_forecasts("NVDA", "2026-10-07", data), [])

    def test_archive_preserves_first_observation_and_versions(self):
        p = {"source": "gildata:forecast", "mean": 10, "fetched_at": "2026-10-07T00:00:00+00:00"}
        first = db.reference_evidence_append("NVDA", "forecast", "2026-10-06", p)
        same = db.reference_evidence_append("NVDA", "forecast", "2026-10-06", {**p, "fetched_at": "2026-10-08T00:00:00+00:00"})
        self.assertEqual(first, same)
        self.assertEqual(db.reference_evidence_previous("NVDA", "forecast", "2026-10-07", "2026-10-07T01:00:00+00:00")["fetched_at"], p["fetched_at"])
        self.assertIsNone(db.reference_evidence_previous("NVDA", "forecast", "2026-10-07", "2026-10-06T00:00:00+00:00"))
        self.assertNotEqual(first, db.reference_evidence_append("NVDA", "forecast", "2026-10-06", {**p, "mean": 11}))

    def test_comparable_period_and_window_only(self):
        today = gil.normalize_forecasts("NVDA", "2026-10-07", forecasts(mean="11"))
        old = {"estimates": gil.normalize_forecasts("NVDA", "2026-10-06", forecasts(stamp="2026-10-06"))}
        changed = gil._forecast_changes(today, old)[0]
        self.assertAlmostEqual(changed["mean_change_pct"], .1)
        for item in old["estimates"]:
            item["window_days"] = 30
        self.assertIsNone(gil._forecast_changes(today, old)[0]["mean_change_pct"])
        old["estimates"][0].update(window_days=100, report_period="2028-01-31")
        self.assertIsNone(gil._forecast_changes(today, old)[0]["mean_change_pct"])

    def test_refresh_persists_and_deduplicates(self):
        with patch.object(gil, "_finquery", side_effect=[(company(), {}), (forecasts(), {})]) as query:
            a = gil.refresh_research(["NVDA"], "2026-10-07")
            b = gil.refresh_research(["NVDA"], "2026-10-07")
        self.assertEqual(a["written"], 2)
        self.assertEqual(b["written"], 0)
        self.assertEqual(query.call_count, 2)
        with db.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM reference_evidence_snapshots").fetchone()[0], 2)
        self.assertIsNone(gil.cached_research("NVDA")["forecast"]["estimates"][0]["mean_change_pct"])

    def test_failure_cooldown_and_good_cache_retention(self):
        db.cache_set("gildata:research:forecast:NVDA", {"estimates": [{"mean": 10}]})
        with patch.object(gil, "_finquery", side_effect=gil.requests.Timeout("secret token")) as query:
            failed = gil.refresh_research(["NVDA"], "2026-10-07")
            gil.refresh_research(["NVDA"], "2026-10-07")
        self.assertEqual(query.call_count, 2)
        self.assertNotIn("secret", json.dumps(failed))
        self.assertEqual(gil.cached_research("NVDA")["forecast"]["estimates"][0]["mean"], 10)

    def test_partial_same_day_does_not_erase_valid_estimates(self):
        estimates = gil.normalize_forecasts("NVDA", "2026-10-07", forecasts())
        old = {"as_of": "2026-10-07", "estimates": estimates}
        partial = {"as_of": "2026-10-07", "estimates": [{**estimates[0], "mean": 11}]}
        merged = gil._merge_same_day_forecasts(partial, old)
        self.assertEqual(len(merged["estimates"]), 2)
        self.assertEqual(merged["estimates"][0]["mean"], 11)
        partial["as_of"] = "2026-10-08"
        self.assertEqual(len(gil._merge_same_day_forecasts(partial, old)["estimates"]), 1)

    def test_cache_only_and_disabled_never_query(self):
        with market.external_data_scope(False), patch.object(gil, "_finquery") as query:
            self.assertEqual(gil.refresh_research(["NVDA"], "2026-10-07")["status"], "disabled_or_cache_only")
            gil.cached_research("NVDA")
        query.assert_not_called()
        with patch.dict(gil.os.environ, {"GILDATA_RESEARCH_ENABLED": "0"}):
            self.assertEqual(gil.cached_research("NVDA"), {})

    def test_news_wrong_subject_market_and_old_hits_rejected(self):
        window = ("2026-10-01T00:00:00+00:00", "2026-10-08T12:00:00+00:00", "2026-10-08T12:00:00+00:00")
        for rows in (article("齐鲁高速月报"), article("某公司(300496.SZ)合作英伟达"), article(stamp="2026-09-01 10:00:00")):
            self.assertEqual(gil.normalize_news(rows, "NVDA", ["英伟达"], *window), [])

    def test_news_unknown_timezone_not_assigned_and_no_fake_url(self):
        receipt = "2026-10-08T12:00:00+00:00"
        rows = gil.normalize_news(article(), "NVDA", ["英伟达"], "2026-10-01T00:00:00+00:00", receipt, receipt)
        self.assertEqual(rows[0]["published_utc"], receipt)
        self.assertEqual(rows[0]["article_url"], "")
        self.assertEqual(rows[0]["gildata_provenance"]["time_status"], "timezone_unknown")
        self.assertFalse(rows[0]["gildata_provenance"]["original_verified"])

    def test_news_known_timezone_is_converted(self):
        rows = gil.normalize_news(article(stamp="2026-10-07T10:00:00+08:00"), "NVDA", ["英伟达"],
            "2026-10-01T00:00:00+00:00", "2026-10-08T12:00:00+00:00", "2026-10-08T12:00:00+00:00")
        self.assertEqual(rows[0]["published_utc"], "2026-10-07T02:00:00+00:00")

    def test_news_snippet_cannot_overwrite_original_or_feedback(self):
        news.ensure_premarket_news_tables()
        original = {"news_id": "one", "symbol": "NVDA", "title_original": "headline", "source": "yahoo:news",
                    "published_utc": "2026-10-07T00:00:00+00:00", "article_url": "https://example.com/original"}
        news._store_items([original])
        with db.connection() as conn:
            conn.execute("INSERT INTO premarket_news_feedback(news_id,symbol,decision,created_at,updated_at) VALUES('one','NVDA','important','now','now')")
            conn.commit()
        news._store_items([{**original, "source": "gildata:news", "article_url": ""}])
        with db.connection() as conn:
            self.assertEqual(conn.execute("SELECT article_url,source FROM premarket_news_items").fetchone()["source"], "yahoo:news")
            self.assertEqual(conn.execute("SELECT decision FROM premarket_news_feedback").fetchone()[0], "important")

    def test_repeated_supplement_retains_first_queue_time(self):
        item = {"news_id": "one", "symbol": "NVDA", "source": "gildata:news", "published_utc": "2026-10-07T00:00:00+00:00"}
        news._store_items([item])
        news._store_items([{**item, "published_utc": "2026-10-08T00:00:00+00:00"}])
        with db.connection() as conn:
            self.assertEqual(conn.execute("SELECT published_utc FROM premarket_news_items").fetchone()[0], item["published_utc"])

    def test_macro_upward_inflation_is_not_labeled_bullish(self):
        now = datetime.now(timezone.utc).isoformat()
        rows = gil.normalize_news(article("美联储上调通胀预期"), "MARKET", ["美联储"],
                                  "2026-10-01T00:00:00+00:00", "2026-10-08T23:59:59+00:00", now)
        def feed(symbols, start, end, publish):
            publish(rows)
            return rows, {"status": "completed", "accepted": len(rows)}
        with patch.object(news, "_universe_symbols", return_value=({"NVDA"}, {}, {}, [], [])), \
             patch.object(news, "_fetch_massive_news", return_value=([], {})), \
             patch.object(news, "_fetch_yahoo_news", return_value=[]), patch.object(gil, "fetch_news_supplement", side_effect=feed):
            result = news.refresh_premarket_news(start_utc="2026-10-01T00:00:00+00:00", end_utc="2026-10-08T23:59:59+00:00", translate_top=0, enrich_metadata=False)
        self.assertEqual(result["matched_item_count"], 1)
        with db.connection() as conn:
            row = conn.execute("SELECT sentiment,estimated_gap_pct FROM premarket_news_items").fetchone()
        self.assertEqual(row["sentiment"], "unreviewed")
        self.assertIsNone(row["estimated_gap_pct"])

    def test_supplement_cache_only_and_query_bound(self):
        now = datetime.now(timezone.utc).isoformat()
        with market.external_data_scope(False), patch.object(gil, "_query") as query:
            gil.fetch_news_supplement(["NVDA"], now, now)
        query.assert_not_called()
        with patch.object(gil, "_query", return_value=([], {})) as query:
            gil.fetch_news_supplement(["NVDA", "AAPL", "TSLA"], now, now)
        self.assertEqual(query.call_count, 3)
        self.assertTrue(all(c.kwargs["max_attempts"] == 1 for c in query.call_args_list))

    def test_deepseek_digest_changes_with_reference_not_rule_score(self):
        context = {"symbol": "NVDA", "gildata_reference": {"estimates": [{"mean": 10}]}, "decision": "观察"}
        self.assertNotEqual(assistant._context_digest(context), assistant._context_digest({**context, "gildata_reference": {"estimates": [{"mean": 11}]}}))
        prompt = assistant._build_prompt(context)
        self.assertIn("聚源参考证据", prompt[1]["content"])
        self.assertEqual(context["decision"], "观察")


if __name__ == "__main__":
    unittest.main()
