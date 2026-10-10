import copy
import json
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import api_server as api
import gildata_daily_service as daily
import market_data_service as market


def response(symbol="AAPL", bad="", dates=None):
    days = dates or ["2026-10-06", "2026-10-07", "2026-10-08"]
    header = "|证券代码|交易日期|币种|开盘价(元)|最高价(元)|最低价(元)|收盘价(元)|昨收价(元)|成交量(万股)|成交额(万元)|"
    lines = [header, "|" + "|".join(["---"] * 10) + "|"]
    for day in days:
        currency = "CNY" if bad == "currency" else "USD"
        high = 98 if bad == "bounds" else 102
        opening = 50 if bad == "price_basis" else 100
        volume = "-" if bad == "volume_missing" else "100" if bad == "volume_scale" else "0.1"
        close = 50 if bad == "split" and day == days[-1] else 100
        previous = 50 if bad == "previous_basis" and day == days[-1] else 100
        low = 49 if close == 50 else 99
        lines.append(f"|{symbol}|{day}|{currency}|{opening}|{high}|{low}|{close}|{previous}|{volume}|10|")
    return [{"api_name": "美股日行情", "table_markdown": "\n".join(lines)}]


def history():
    return pd.DataFrame({"Open": [100., 100.], "High": [102., 102.], "Low": [99., 99.],
                         "Close": [100., 100.], "Volume": [1000., 1000.]},
                        index=pd.DatetimeIndex(["2026-10-06", "2026-10-07"], name="Date"))


class GilDataDailyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        patch.object(market, "_CACHE_ROOT", Path(self.temp.name)).start()
        patch.dict(daily.os.environ, {"GILDATA_DAILY_PRIMARY": "1", "GILDATA_MCP_TOKEN": "test"}).start()
        patch.object(daily, "most_recent_session", return_value=date(2026, 10, 8)).start()
        self.records, self.evidence = {}, []
        patch.object(daily, "cache_get", side_effect=lambda key: self.records.get(key)).start()
        patch.object(daily, "cache_set", side_effect=lambda key, value, **kwargs: self.records.update({key: value})).start()
        patch.object(daily, "reference_evidence_append", side_effect=lambda *args: self.evidence.append(args)).start()
        market._write_daily_cache("AAPL", history())

    def test_validated_day_is_added_without_overwriting_history_and_rerun_is_cached(self):
        before = market._read_daily_cache("AAPL")
        with patch.object(daily.gil, "_query", return_value=(response(), {})) as query:
            result = daily.sync_daily(["AAPL", "AAPL"], "2026-10-08")
            again = daily.sync_daily(["AAPL"], "2026-10-08")
        self.assertEqual(result["symbols_written"], 1)
        self.assertEqual(again["symbols_cached"], 1)
        self.assertEqual(query.call_count, 1)
        after = market._read_daily_cache("AAPL")
        pd.testing.assert_frame_equal(before, after.loc[before.index])
        self.assertEqual(after.loc[pd.Timestamp("2026-10-08"), "Volume"], 1000.)
        self.assertEqual(len(self.evidence), 2)
        self.assertEqual(self.evidence[-1][-1]["status"], "written")

    def test_wrong_date_symbol_currency_and_units_cannot_write(self):
        for records in [response(symbol="NVDA"), response(bad="currency"), response(bad="bounds"),
                        response(bad="volume_missing"), response(bad="volume_scale"), response(bad="price_basis"),
                        response(dates=["2026-10-05", "2026-10-06", "2026-10-07"])]:
            with self.subTest(records=records), patch.object(daily.gil, "_query", return_value=(records, {})):
                self.records.clear()
                before = market._read_daily_cache("AAPL")
                result = daily.sync_daily(["AAPL"], "2026-10-08")
                self.assertEqual(result["symbols_written"], 0)
                pd.testing.assert_frame_equal(before, market._read_daily_cache("AAPL"))

    def test_split_and_adjusted_previous_close_are_rejected(self):
        for bad in ["split", "previous_basis"]:
            with self.subTest(bad=bad), patch.object(daily.gil, "_query", return_value=(response(bad=bad), {})):
                self.records.clear()
                result = daily.sync_daily(["AAPL"], "2026-10-08")
                self.assertEqual(result["symbols_written"], 0)
                self.assertIn("AAPL", result["rejected"])

    def test_missing_intermediate_session_cannot_create_a_fake_daily_return(self):
        plan = {"anchors": ["2026-10-06", "2026-10-07"], "missing": ["2026-10-08", "2026-10-09"]}
        quotes, _ = daily.parse_quotes(response(), ["AAPL"], set(plan["anchors"] + plan["missing"]))
        with self.assertRaisesRegex(ValueError, "missing"):
            daily.validate_missing("AAPL", plan, quotes, history())

    def test_conflicting_duplicate_quote_is_rejected(self):
        good = response()
        conflict = copy.deepcopy(good)
        conflict[0]["table_markdown"] = conflict[0]["table_markdown"].replace("|102|", "|103|")
        quotes, errors = daily.parse_quotes(good + conflict, ["AAPL"], {"2026-10-08"})
        self.assertNotIn(("AAPL", "2026-10-08"), quotes)
        self.assertEqual(errors["AAPL", "2026-10-08"], "conflicting_duplicate_bar")

    def test_class_share_alias_is_exact_not_a_foreign_exchange_suffix(self):
        quotes, _ = daily.parse_quotes(response("BRK.B") + response("AAPL.O"), ["BRK-B", "AAPL"], {"2026-10-08"})
        self.assertIn(("BRK-B", "2026-10-08"), quotes)
        self.assertNotIn(("AAPL", "2026-10-08"), quotes)

    def test_cache_only_and_disabled_modes_do_not_query(self):
        with market.external_data_scope(False), patch.object(daily.gil, "_query") as query:
            self.assertEqual(daily.sync_daily(["AAPL"], "2026-10-08")["symbols_written"], 0)
        query.assert_not_called()
        with patch.dict(daily.os.environ, {"GILDATA_DAILY_PRIMARY": "0"}), patch.object(daily.gil, "_query") as query:
            daily.sync_daily(["AAPL"], "2026-10-08")
        query.assert_not_called()

    def test_future_and_non_trading_dates_are_rejected(self):
        with patch.object(daily.gil, "_query") as query:
            for day in ["2026-10-09", "2026-10-10"]:
                with self.assertRaisesRegex(ValueError, "not_completed"):
                    daily.sync_daily(["AAPL"], day)
        query.assert_not_called()

    def test_provider_failure_is_redacted_and_cooled(self):
        with patch.object(daily.gil, "_query", side_effect=daily.gil.requests.Timeout("secret-token")) as query:
            result = daily.sync_daily(["AAPL"], "2026-10-08")
            again = daily.sync_daily(["AAPL"], "2026-10-08")
        self.assertNotIn("secret", str(result))
        self.assertEqual(again["cooldown_count"], 1)
        self.assertEqual(query.call_count, 1)

    def test_dry_run_does_not_write_cache_archive_or_attempt_state(self):
        with patch.object(daily.gil, "_query", return_value=(response(), {})):
            result = daily.sync_daily(["AAPL"], "2026-10-08", execute=False)
        self.assertEqual(result["validated_symbols"], ["AAPL"])
        self.assertEqual(len(market._read_daily_cache("AAPL")), 2)
        self.assertFalse(self.records)
        self.assertFalse(self.evidence)

    def test_budget_stops_before_query(self):
        with patch.object(daily.gil, "_query") as query:
            daily.sync_daily(["AAPL"], "2026-10-08", max_seconds=1)
        query.assert_not_called()

    def test_empty_history_does_not_abort_valid_symbols(self):
        with patch.object(daily.gil, "_query", return_value=(response(), {})) as query:
            result = daily.sync_daily(["MARKET", "AAPL"], "2026-10-08")
        self.assertEqual(result["symbols_written"], 1)
        self.assertEqual(result["rejected"]["MARKET"], "history_anchor_missing_or_future")
        self.assertEqual(result["rejection_reasons"], {"history_anchor_missing_or_future": 1})
        self.assertEqual(result["sync_version"], daily.SYNC_VERSION)
        self.assertNotIn("MARKET", query.call_args.args[1])

    def test_invalid_history_index_is_rejected_without_comparison(self):
        with patch.object(market, "_read_daily_cache", return_value=history().reset_index(drop=True)):
            self.assertEqual(daily._plan("AAPL", "2026-10-08")["reason"], "invalid_history_index")

    def test_one_cache_read_failure_does_not_abort_the_batch_or_leak_details(self):
        read = market._read_daily_cache
        def cache(symbol):
            if symbol == "MARKET":
                raise TypeError("private-token")
            return read(symbol)
        with patch.object(market, "_read_daily_cache", side_effect=cache), \
                patch.object(daily.gil, "_query", return_value=(response(), {})):
            result = daily.sync_daily(["MARKET", "AAPL"], "2026-10-08")
        self.assertEqual(result["symbols_written"], 1)
        self.assertEqual(result["rejected"]["MARKET"], "cache_planning_TypeError")
        self.assertNotIn("private", json.dumps(result))

    def test_fixed_local_error_gets_one_retry_even_with_old_source_pause(self):
        state = {"daily_three_layer_auto:price_source_pause": {"session": "2026-10-08", "failed_at": daily.time.time()},
                 "gildata:daily_last_status": {"session": "2026-10-08", "status": "unavailable", "error": "TypeError"}}
        with patch.object(api, "cache_get", side_effect=lambda key: state.get(key)), \
                patch("market_calendar.most_recent_session", return_value=date(2026, 10, 8)), \
                patch.object(api, "_daily_sync_prices_strict", return_value="2026-10-08") as sync:
            self.assertEqual(api._daily_sync_prices(["ndx"], {}, allow_partial=True), "2026-10-08")
        sync.assert_called_once()

    def test_current_version_error_preserves_cooldown_and_diagnostics(self):
        report = {"session": "2026-10-08", "status": "unavailable", "error": "TypeError", "sync_version": 2}
        state = {"daily_three_layer_auto:price_source_pause": {"session": "2026-10-08", "failed_at": daily.time.time()},
                 "gildata:daily_last_status": report}
        with patch.object(api, "cache_get", side_effect=lambda key: state.get(key)), patch.object(api, "cache_set"), \
                patch("market_calendar.most_recent_session", return_value=date(2026, 10, 8)), \
                patch.object(api, "_daily_report_price_coverage", return_value={"current": 1, "total": 2}), \
                patch.object(api, "_daily_sync_prices_strict") as sync:
            out = {}
            self.assertEqual(api._daily_sync_prices(["ndx"], out, allow_partial=True), "2026-10-08")
        sync.assert_not_called()
        self.assertEqual(out["gildata_price_sync"], report)

    def test_partial_primary_success_is_kept_when_fallback_is_unpublished(self):
        partial = {"total": 2, "current": 1, "ratio": .5, "missing_reports": [], "symbols": ["AAPL", "MARKET"]}
        stale = {**partial, "current": 0, "ratio": 0}
        records = {}
        with patch.object(api, "cache_get", side_effect=lambda key: records.get(key)), \
                patch.object(api, "cache_set", side_effect=lambda key, value: records.update({key: value})), \
                patch("market_calendar.most_recent_session", return_value=date(2026, 10, 8)), \
                patch.object(api, "_daily_report_price_coverage", side_effect=[stale, partial, partial, partial, partial]), \
                patch.object(api, "_daily_gildata_gap_repair", return_value={"status": "partial", "symbols_written": 1}), \
                patch("scripts.ingest_grouped_daily.ingest_recent_grouped_daily", return_value={"ok": False}), \
                patch.object(api, "_daily_databento_gap_repair", return_value={"ok": False, "status": "waiting_data"}):
            out = {}
            self.assertEqual(api._daily_sync_prices(["ndx"], out, allow_partial=True), "2026-10-08")
        self.assertEqual(out["gildata_price_sync"]["symbols_written"], 1)
        self.assertEqual(out["cached_price_coverage"]["current"], 1)
        self.assertTrue(out["data_warnings"])
        self.assertNotIn("market_calendar:last_synced_session", records)

    def test_turnover_warning_is_not_imported_as_price_or_volume(self):
        records = response()
        records[0]["table_markdown"] = records[0]["table_markdown"].replace("|0.1|10|", "|0.1|1|")
        quotes, _ = daily.parse_quotes(records, ["AAPL"], {"2026-10-06", "2026-10-07", "2026-10-08"})
        fresh, validation = daily.validate_missing("AAPL", daily._plan("AAPL", "2026-10-08"), quotes, history())
        self.assertEqual(validation["warnings"], ["turnover_inconsistent_not_used"])
        self.assertEqual(fresh.loc[pd.Timestamp("2026-10-08"), "Volume"], 1000.)
        self.assertNotIn("turnover", fresh.columns)

    def test_concurrent_bar_is_preserved_and_counted_as_cached(self):
        def fetch(*args, **kwargs):
            full = pd.concat([history(), history().tail(1).set_axis(pd.DatetimeIndex(["2026-10-08"], name="Date"))])
            market._write_daily_cache("AAPL", full)
            return response(), {}
        with patch.object(daily.gil, "_query", side_effect=fetch):
            result = daily.sync_daily(["AAPL"], "2026-10-08")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(result["symbols_cached"], 1)
        self.assertFalse(self.evidence)

    def test_authorization_failure_opens_circuit_for_remaining_batches(self):
        for symbol in ["NVDA", "ONDS"]:
            market._write_daily_cache(symbol, history())
        error = daily.gil.requests.HTTPError("private-token")
        error.response = daily.gil.requests.Response()
        error.response.status_code = 403
        with patch.object(daily.gil, "_query", side_effect=error) as query:
            result = daily.sync_daily(["AAPL", "NVDA", "ONDS"], "2026-10-08", batch_size=1, workers=1)
        self.assertEqual(query.call_count, 1)
        self.assertTrue(result["circuit_open"])
        self.assertEqual(result["pending_count"], 2)
        self.assertNotIn("private", str(result))

    def test_primary_success_bypasses_massive_and_databento(self):
        full = {"total": 1, "current": 1, "ratio": 1., "missing_reports": [], "symbols": ["AAPL"]}
        stale = {**full, "current": 0, "ratio": 0.}
        with patch.object(api, "cache_get", return_value=None), patch.object(api, "cache_set"), \
                patch("market_calendar.most_recent_session", return_value=date(2026, 10, 8)), \
                patch.object(api, "_daily_report_price_coverage", side_effect=[stale, full]), \
                patch.object(api, "_daily_gildata_gap_repair", return_value={"status": "completed", "symbols_written": 1}), \
                patch("scripts.ingest_grouped_daily.ingest_recent_grouped_daily") as massive, \
                patch.object(api, "_daily_databento_gap_repair") as databento:
            self.assertEqual(api._daily_sync_prices_strict(["ndx"], {"timings": {}}), "2026-10-08")
        massive.assert_not_called()
        databento.assert_not_called()

    def test_worker_is_hard_bounded_and_failure_does_not_include_credentials(self):
        with patch.object(api.subprocess, "run", side_effect=subprocess.TimeoutExpired("private-token", 180)) as run, \
                patch.object(api, "cache_set"):
            result = api._daily_gildata_gap_repair("2026-10-08", ["AAPL"])
        self.assertEqual(run.call_args.kwargs["timeout"], 180)
        self.assertEqual(result["status"], "timeout")
        self.assertNotIn("private", str(result))

    def test_healthy_primary_is_not_blocked_by_old_provider_pause(self):
        state = {"daily_three_layer_auto:price_source_pause": {"session": "2026-10-08", "failed_at": daily.time.time()},
                 "gildata:daily_last_status": {"session": "2026-10-08", "status": "completed"}}
        with patch.object(api, "cache_get", side_effect=lambda key: state.get(key)), \
                patch("market_calendar.most_recent_session", return_value=date(2026, 10, 8)), \
                patch.object(api, "_daily_sync_prices_strict", return_value="2026-10-08") as primary:
            self.assertEqual(api._daily_sync_prices(["ndx"], {}, allow_partial=True), "2026-10-08")
        primary.assert_called_once()


if __name__ == "__main__":
    unittest.main()
