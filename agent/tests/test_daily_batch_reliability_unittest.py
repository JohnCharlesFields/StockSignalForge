from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch

import api_server as api
from batch_runtime import check_scan_budget, watch_process
from scripts import screening_framework_v2_optimized as screen


class ImmediateThread:
    def __init__(self, target, args=(), kwargs=None, **ignored):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)

    def is_alive(self):
        return False

    def join(self, timeout=None):
        pass


class DailyBatchReliabilityTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(api, "_start_daily_news_update")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_silent_subprocess_is_really_terminated(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdout=subprocess.PIPE)
        done = threading.Event()
        try:
            job = {"deadline_monotonic": time.monotonic() + 0.1}
            watchdog = watch_process(proc, job, done)
            watchdog.join(timeout=8)
            self.assertFalse(watchdog.is_alive())
            self.assertIsNotNone(proc.poll())
            self.assertTrue(job["cancel_requested"])
        finally:
            done.set()
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            proc.stdout.close()

    def test_cancelled_job_cannot_publish_success(self):
        with self.assertRaises(TimeoutError):
            check_scan_budget({"cancel_requested": True})
        check_scan_budget({})  # Interactive jobs retain their original behavior.

    def test_price_failure_stops_before_pool_work_and_keeps_timings(self):
        records = {}
        def sync(pools, out, **kwargs):
            out["timings"]["grouped_daily_ingest"] = 2.5
            raise RuntimeError("HTTP 429")
        with (
            patch.object(api, "_AUTO_SCAN_JOB", {}),
            patch.object(api, "_auto_scan_universe_ids", return_value=["ndx", "spx"]),
            patch.object(api, "_daily_sync_prices", side_effect=sync),
            patch.object(api, "cache_set", side_effect=lambda k, v: records.update({k: copy.deepcopy(v)})),
            patch.object(api, "_run_research_signal_hub_job") as scan,
        ):
            api._run_daily_three_layer_auto_scan(reason="test")
        scan.assert_not_called()
        record = records[api._AUTO_SCAN_STATE_KEY]
        self.assertEqual(record["phase"], "price_preflight")
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["phase_timings"]["grouped_daily_ingest"], 2.5)

    def test_retry_only_runs_failed_pool_and_checkpoint_survives_finalizer_error(self):
        records, calls = {}, []
        attempt = [1]
        def scan(job_id, payload):
            calls.append(payload.universe)
            api._research_signal_hub_jobs[job_id].update({
                "status": "failed" if attempt[0] == 1 and payload.universe == "spx" else "completed",
                "result": {"llm_review_available_count": 1, "rows": [{"large": "not persisted"}]},
            })
        def finalize(pools):
            if attempt[0] == 1:
                raise RuntimeError("transient persistence failure")
            return {"snapshot_id": "new", "snapshot_row_count": 2}
        with (
            patch.object(api, "_AUTO_SCAN_JOB", {}),
            patch.object(api, "_DAILY_ACTIVE_WORKERS", {}),
            patch.object(api, "_research_signal_hub_jobs", {}),
            patch.object(api, "_auto_scan_universe_ids", return_value=["ndx", "spx"]),
            patch.object(api, "_daily_sync_prices", return_value="2026-09-29"),
            patch.object(api, "cache_get", side_effect=lambda k: copy.deepcopy(records.get(k))),
            patch.object(api, "cache_set", side_effect=lambda k, v: records.update({k: copy.deepcopy(v)})),
            patch.object(api, "_run_research_signal_hub_job", side_effect=scan),
            patch.object(api, "_daily_post_scan_finalize", side_effect=finalize),
            patch.object(api, "_daily_post_scan_optional_enrichment"),
            patch.object(api.threading, "Thread", ImmediateThread),
        ):
            api._run_daily_three_layer_auto_scan(reason="test")
            self.assertEqual(records["daily_three_layer_auto:checkpoint"]["results"]["ndx"]["status"], "completed")
            self.assertNotIn("rows", records["daily_three_layer_auto:checkpoint"]["results"]["ndx"]["result"])
            attempt[0] = 2
            api._run_daily_three_layer_auto_scan(reason="retry")
            self.assertEqual(calls, ["ndx", "spx", "spx"])
            self.assertEqual(records[api._AUTO_SCAN_STATE_KEY]["resumed_universe_count"], 1)
            self.assertEqual(records[api._AUTO_SCAN_STATE_KEY]["status"], "completed")
            api._run_daily_three_layer_auto_scan(reason="force", force=True)
            self.assertEqual(calls[-2:], ["ndx", "spx"])

    def test_queued_run_cannot_start_a_duplicate(self):
        with (
            patch.object(api, "_AUTO_SCAN_JOB", {"status": "queued"}),
            patch.object(api, "_auto_scan_state", return_value={"status": "queued"}),
            patch.object(api.threading, "Thread") as thread,
        ):
            api._start_daily_three_layer_auto_scan(reason="double-click", force=True)
        thread.assert_not_called()

    def test_draining_worker_blocks_retry(self):
        with (
            patch.object(api, "_AUTO_SCAN_JOB", {"status": "failed"}),
            patch.object(api, "_auto_scan_state", return_value={"status": "failed"}),
            patch.object(api, "_daily_active_worker_ids", return_value=["old"]),
            patch.object(api.threading, "Thread") as thread,
        ):
            result = api._start_daily_three_layer_auto_scan(reason="retry", force=True)
        self.assertTrue(result["blocked_by_active_worker"])
        thread.assert_not_called()

    def test_databento_repair_is_cost_capped_and_bounded(self):
        response = subprocess.CompletedProcess([], 0, json.dumps({"symbols_filled": 2, "estimated_cost_usd": 0}), "")
        with (
            patch.dict(api.os.environ, {"DATABENTO_API_KEY": "test", "VIBE_DAILY_PRICE_REPAIR_MAX_COST_USD": "0"}),
            patch.object(api.subprocess, "run", return_value=response) as run,
        ):
            result = api._daily_databento_gap_repair("2026-09-29")
        self.assertTrue(result["ok"])
        self.assertEqual(run.call_args.kwargs["timeout"], 120)
        self.assertEqual(run.call_args.args[0][-1], "0.0")
        self.assertIn("2026-09-30", run.call_args.args[0])

    def test_automatic_retry_is_delayed_and_limited(self):
        now = datetime.now(api._auto_scan_timezone())
        record = {"date": api._auto_scan_today_key(now), "status": "failed", "phase": "price_preflight",
                  "finished_at": (now - timedelta(minutes=31)).isoformat()}
        with patch.object(api, "cache_get", return_value={"date": record["date"], "count": 0}):
            self.assertTrue(api._daily_auto_retry_due(now, record))
            self.assertFalse(api._daily_auto_retry_due(now, {**record, "finished_at": now.isoformat()}))
            self.assertFalse(api._daily_auto_retry_due(now, {**record, "phase": "pool_scan"}))
        with patch.object(api, "cache_get", return_value={"date": record["date"], "count": 2}):
            self.assertFalse(api._daily_auto_retry_due(now, record))

    def test_cost_cap_error_is_not_reported_as_provider_permission_failure(self):
        response = subprocess.CompletedProcess([], 1, "", "RuntimeError: Estimated cost $0.0017 exceeds cap $0.0000")
        with (
            patch.dict(api.os.environ, {"DATABENTO_API_KEY": "test", "VIBE_DAILY_PRICE_REPAIR_MAX_COST_USD": "0"}),
            patch.object(api.subprocess, "run", return_value=response),
        ):
            result = api._daily_databento_gap_repair("2026-09-29")
        self.assertFalse(result["ok"])
        self.assertEqual(result["estimated_cost_usd"], 0.0017)
        self.assertIn("cost cap", result["error"])

    def test_repair_budget_is_cumulative_and_unknown_failure_keeps_reservation(self):
        records = {}
        response = subprocess.CompletedProcess([], 0, json.dumps({"symbols_filled": 2, "estimated_cost_usd": 0.6}), "")
        with (
            patch.dict(api.os.environ, {"DATABENTO_API_KEY": "test", "VIBE_DAILY_PRICE_REPAIR_MAX_COST_USD": "1"}),
            patch.object(api, "cache_get", side_effect=lambda k: copy.deepcopy(records.get(k))),
            patch.object(api, "cache_set", side_effect=lambda k, v: records.update({k: copy.deepcopy(v)})),
            patch.object(api.subprocess, "run", return_value=response) as run,
        ):
            self.assertTrue(api._daily_databento_gap_repair("2026-09-29")["ok"])
            run.return_value = subprocess.CompletedProcess([], 1, "", "unexpected post-download failure")
            self.assertFalse(api._daily_databento_gap_repair("2026-09-29")["ok"])
            self.assertAlmostEqual(float(run.call_args.args[0][-1]), 0.4)
            exhausted = api._daily_databento_gap_repair("2026-09-29")
            self.assertIn("exhausted", exhausted["error"])
            self.assertEqual(run.call_count, 2)

    def test_finalizer_error_retains_completed_stage_measurements(self):
        def fail(pools, out):
            out["timings"]["snapshot_build"] = 1.2
            raise RuntimeError("ledger unavailable")
        with patch.object(api, "_daily_post_scan_finalize_impl", side_effect=fail):
            with self.assertRaises(RuntimeError) as caught:
                api._daily_post_scan_finalize(["ndx"])
        self.assertEqual(caught.exception.daily_result["timings"]["snapshot_build"], 1.2)

    def test_daily_history_does_not_trigger_per_symbol_provider(self):
        import pandas as pd
        frame = pd.DataFrame({"Close": [10]}, index=pd.DatetimeIndex(["2026-09-29"], name="Date"))
        with (
            patch.dict(screen.CONFIG, {"daily_price_cache_only": True}),
            patch.object(screen, "get_daily_history", return_value=(frame, "cache")) as get,
        ):
            screen.cached_history(object(), "NVDA", "6mo")
        self.assertFalse(get.call_args.kwargs["allow_yfinance_fallback"])

    def test_concurrent_state_writes_and_saves_do_not_mutate_during_iteration(self):
        failures = []
        def worker(offset):
            try:
                for i in range(10):
                    api._store_launch_result("ndx", {"signals": [{"symbol": f"T{offset+i}"}], "scan_time": "now"})
            except Exception as exc:
                failures.append(exc)
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(api, "RUNS_DIR", Path(directory)),
                patch.object(api, "_RESEARCH_SIGNAL_STATE_PATH", Path(directory) / "state.json"),
                patch.object(api, "_latest_launch_signals", {}),
                patch.object(api, "_latest_event_signals", {}),
                patch.object(api, "_latest_event_scans", {}),
            ):
                threads = [threading.Thread(target=worker, args=(i*10,)) for i in range(5)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                self.assertEqual(len(json.loads(api._RESEARCH_SIGNAL_STATE_PATH.read_text())["launch"]), 50)
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
