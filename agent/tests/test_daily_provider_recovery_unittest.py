import json
import unittest
from unittest.mock import Mock, patch

import app_database as database
import market_data_service as market
import api_server as api
import subprocess


class ProviderRecoveryTests(unittest.TestCase):
    def test_pre_download_failure_releases_only_its_reservation(self):
        records = {}
        response = subprocess.CompletedProcess([], 1, "", json.dumps({
            "phase": "cost_estimate", "download_started": False, "repair_error": "TimeoutError"}))
        with patch.dict(api.os.environ, {"DATABENTO_API_KEY": "test", "VIBE_DAILY_PRICE_REPAIR_MAX_COST_USD": "1"}), \
                patch.object(api, "cache_get", side_effect=lambda k: records.get(k)), \
                patch.object(api, "cache_set", side_effect=lambda k, v: records.update({k: v})), \
                patch.object(api.subprocess, "run", return_value=response):
            result = api._daily_databento_gap_repair("2026-10-07")
        self.assertFalse(result["budget_reservation_retained"])
        self.assertEqual(records["daily_three_layer_auto:price_repair_budget"]["committed_usd"], 0)

    def test_429_respects_retry_after_and_counts_every_attempt(self):
        limited = Mock(status_code=429, headers={"Retry-After": "30"})
        success = Mock(status_code=200)
        success.json.return_value = {"ok": True}
        with patch.dict(market.os.environ, {"MASSIVE_API_KEY": "test"}), \
                patch.object(market, "_claim", return_value=True) as claim, \
                patch.object(market.requests, "get", side_effect=[limited, success]), \
                patch.object(market.time, "sleep") as sleep:
            self.assertEqual(market._massive_get("/test"), {"ok": True})
        sleep.assert_called_once_with(30.0)
        self.assertEqual(claim.call_count, 2)

    def test_long_provider_pause_does_not_retry_early(self):
        limited = Mock(status_code=429, headers={"Retry-After": "120"})
        limited.raise_for_status.side_effect = market.requests.HTTPError("429")
        with patch.dict(market.os.environ, {"MASSIVE_API_KEY": "test"}), \
                patch.object(market, "_claim", return_value=True), \
                patch.object(market.requests, "get", return_value=limited) as get, \
                patch.object(market.time, "sleep") as sleep:
            with self.assertRaises(market.requests.HTTPError):
                market._massive_get("/test")
        sleep.assert_not_called()
        self.assertEqual(get.call_count, 1)

    def test_fresh_small_snapshot_beats_old_large_snapshot(self):
        old = {"data_as_of": "2026-09-29", "rows": [{}] * 127,
               "loaded_universe_count": 11, "failed_universe_count": 0}
        fresh = {**old, "data_as_of": "2026-10-07", "rows": [{}] * 5}
        rows = [{"snapshot_id": name, "generated_at": stamp, "status": "completed",
                 "source": "daily_auto", "payload_json": json.dumps(payload)}
                for name, stamp, payload in [("fresh", "2026-10-08", fresh), ("old", "2026-09-30", old)]]
        conn = Mock()
        conn.execute.return_value.fetchall.return_value = rows
        context = Mock()
        context.__enter__ = Mock(return_value=conn)
        context.__exit__ = Mock(return_value=False)
        with patch.object(database, "ensure_database"), patch.object(database, "_connect", return_value=context):
            selected = database.latest_home_dashboard_snapshot()
        self.assertEqual(selected["snapshot_id"], "fresh")


if __name__ == "__main__":
    unittest.main()
