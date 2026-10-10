import asyncio
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

import pandas as pd

import app_database as db
import v_swing_service as service
from v_swing_model import config
from test_v_swing_model_unittest import bars


def score(symbol="AAPL", p=.8, session="2026-10-09", **kw):
    return {"symbol": symbol, "status": "scored", "price_as_of": session, "signal_date": session,
            "close": 100., "buy": True, "sell": False, "buy_p": p, "sell_p": .01,
            "plan": {"entry": 100., "stop": 95., "target": 110., "in_box": True}, **kw}


class VSwingServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH, db._INITIALIZED
        db.DB_PATH, db._INITIALIZED = Path(self.temp.name)/"isolated.sqlite3", False
        service.ensure_schema()
        self.cfg = {**config(), "enabled": True}
        self.addCleanup(patch.stopall)
        patch.object(service, "config", return_value=self.cfg).start()
        patch("v_swing_model.annotations", return_value={"AAPL": {"2026-05-01": "buy", "2026-07-01": "sell"}}).start()

    def tearDown(self):
        db.DB_PATH, db._INITIALIZED = self.old
        self.temp.cleanup()

    def test_duplicate_run_freezes_original_plan_and_updates_probability(self):
        values = {"AAPL": score()}
        first = service.update_lifecycle(values, {}, "2026-10-09", "v1", True, self.cfg)
        self.assertEqual(first["open_buys"], 1)
        changed = {"AAPL": score(p=.95, plan={"entry": 120., "stop": 90., "target": 180.})}
        second = service.update_lifecycle(changed, {}, "2026-10-09", "v2", True, self.cfg)
        self.assertEqual(len(second["rows"]), 1)
        self.assertEqual(changed["AAPL"]["frozen_plan"]["entry"], 100.)
        self.assertEqual(second["rows"][0]["p"], .95)
        self.assertTrue(changed["AAPL"]["today_buy"])

    def test_continuing_without_new_trigger_preserves_reference_and_break_even(self):
        service.update_lifecycle({"AAPL": score(session="2026-10-08")}, {}, "2026-10-08", "v", True, self.cfg)
        frame = pd.DataFrame({"Open": [100, 100], "High": [101, 106], "Low": [99, 99],
                              "Close": [100, 105], "Volume": [1000, 1000]}, index=pd.to_datetime(["2026-10-08", "2026-10-09"]))
        values = {"AAPL": score(buy=False, close=105)}
        result = service.update_lifecycle(values, {"AAPL": frame}, "2026-10-09", "v", True, self.cfg)
        self.assertEqual(values["AAPL"]["lifecycle"], "continuing")
        self.assertEqual(values["AAPL"]["r_multiple"], 1.)
        self.assertEqual(values["AAPL"]["frozen_plan"]["managed_stop"], 100.)
        self.assertEqual(result["rows"][0]["stop"], 95.)
        self.assertFalse(values["AAPL"]["today_buy"])

    def test_concentration_highest_five_and_conflict_unsupported_stale_not_opened(self):
        values = {str(i): score(str(i), p=i/10) for i in range(1, 8)}
        values["conflict"] = score("conflict", p=.99, sell=True)
        values["stale"] = score("stale", status="stale")
        result = service.update_lifecycle(values, {}, "2026-10-09", "v", True, self.cfg)
        self.assertEqual(result["open_buys"], 5)
        self.assertEqual({r["ticker"] for r in result["rows"] if r["status"] == "open"}, {"3", "4", "5", "6", "7"})
        self.assertEqual(len(result["rows"]), 9)
        self.assertEqual(sum(r["status"] == "concentration_watch" for r in result["rows"]), 2)
        self.assertEqual(sum(r["status"] == "conflicting_watch" for r in result["rows"]), 2)
        self.assertEqual(values["conflict"]["lifecycle"], "conflicting_signals")
        self.assertEqual(values["1"]["lifecycle"], "concentration_watch")
        failed = {"B": score("B")}
        service.update_lifecycle(failed, {}, "2026-10-09", "v", False, self.cfg)
        self.assertFalse(failed["B"]["today_buy"])

    def test_sell_is_exit_only_and_does_not_have_buy_risk_lines(self):
        values = {"AAPL": score(buy=False, sell=True)}
        result = service.update_lifecycle(values, {}, "2026-10-08", "v", True, self.cfg)
        self.assertIsNone(result["rows"][0]["stop"])
        self.assertIsNone(result["rows"][0]["target"])
        again = {"AAPL": score(buy=False, sell=True)}
        service.update_lifecycle(again, {"AAPL": bars()}, "2026-10-09", "v", True, self.cfg)
        self.assertNotIn("frozen_plan", again["AAPL"])
        self.assertFalse(again["AAPL"]["today_buy"])

    def test_watch_can_be_promoted_without_losing_original_reference(self):
        waiting = {"AAPL": score()}
        first = service.update_lifecycle(waiting, {}, "2026-10-09", "v", True, {**self.cfg, "max_open_buys": 0})
        self.assertEqual(first["rows"][0]["status"], "concentration_watch")
        ready = {"AAPL": score(plan={"entry":120, "stop":100, "target":160})}
        second = service.update_lifecycle(ready, {}, "2026-10-09", "v", True, self.cfg)
        self.assertEqual(len(second["rows"]), 1)
        self.assertEqual(second["rows"][0]["status"], "open")
        self.assertEqual(ready["AAPL"]["frozen_plan"]["entry"], 100)
        self.assertEqual(second["open_buys"], 1)

    def test_weekly_only_mature_live_signals_and_at_most_once_per_week(self):
        self.assertEqual(service.adapt_weekly("2026-10-09", self.cfg), {"buy": 1., "sell": 1.})
        with db.connection() as conn:
            conn.execute("DELETE FROM v_swing_adapt_state")
            for i in range(20):
                conn.execute("INSERT INTO v_swing_signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("2026-08-03", "test", f"X{i}", "buy", .8, 100, 95, 110, 95, 110, 0, 0, "closed",
                     json.dumps({"win": False}), 95, "test", "now"))
            conn.commit()
        self.assertAlmostEqual(service.adapt_weekly("2026-10-09", self.cfg)["buy"], 1.1)
        self.assertAlmostEqual(service.adapt_weekly("2026-10-09", self.cfg)["buy"], 1.1)
        self.assertLessEqual(service.adapt_weekly("2026-10-16", self.cfg)["buy"], 1.6)

    def test_weekly_loosen_and_early_sell_tighten_are_bounded(self):
        with db.connection() as conn:
            for side in ("buy", "sell"):
                for i in range(20):
                    conn.execute("INSERT INTO v_swing_signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        ("2026-08-03", "test", f"X{i}", side, .8, 100, 95, 110, 95, 110, 0, 0, "closed",
                         json.dumps({"win": True, "subsequent_return": .05}), 95, "test", "now"))
                conn.execute("INSERT INTO v_swing_adapt_state VALUES (?,?,?,?)", (side, .7 if side == "buy" else 1.6, "old", "{}"))
            # Immature outcomes must not contaminate the latest 20 matured observations.
            conn.execute("INSERT INTO v_swing_signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("2026-10-08", "test", "BAD", "buy", .8, 100,95,110,95,110,0,0,"closed",json.dumps({"win":False}),95,"test","now"))
            conn.commit()
        mult = service.adapt_weekly("2026-10-09", self.cfg)
        self.assertEqual(mult, {"buy": .7, "sell": 1.6})
        with db.connection() as conn:
            audit = {r["side"]: json.loads(r["audit_json"]) for r in conn.execute("SELECT * FROM v_swing_adapt_state")}
        self.assertEqual(audit["buy"]["win_rate"], 1.)
        self.assertEqual(audit["buy"]["reason"], "few_new_buys_with_acceptable_mature_history")
        self.assertEqual(audit["sell"]["reason"], "sell_reference_too_early")

    def test_rank_today_first_then_profit_probability_not_matching_strength(self):
        snapshot = {"session": "2026-10-09", "pattern_supported": True,
                    "scores": {"LOW": score("LOW", p=.99), "HIGH": score("HIGH", p=.70)},
                    "new_buy_candidates": []}
        for s in snapshot["scores"].values():
            s["today_buy"] = True
        board = {"picks": [{"symbol": "OLD", "calibrated_probability": .9},
                            {"symbol": "LOW", "calibrated_probability": .51},
                            {"symbol": "HIGH", "calibrated_probability": .6}]}
        with patch.object(service, "cache_get", return_value=snapshot) as read, patch.object(service, "most_recent_session", return_value=date(2026, 10, 9)), patch.object(service, "fit_models", side_effect=AssertionError("page cannot train")):
            ranked = service.attach_board(board)
        self.assertEqual([p["symbol"] for p in ranked["picks"]], ["HIGH", "LOW", "OLD"])
        self.assertEqual(read.call_count, 1)
        self.assertNotIn("v_swing", board["picks"][0])

    def test_stale_does_not_promote_and_switches_restore_old_order(self):
        board = {"picks": [{"symbol": "AAPL", "calibrated_probability": .51}, {"symbol": "OTHER", "calibrated_probability": .6}]}
        snap = {"session": "2026-10-08", "pattern_supported": True, "scores": {"AAPL": score(today_buy=True)}}
        with patch.object(service, "cache_get", return_value=snap), patch.object(service, "most_recent_session", return_value=date(2026,10,9)):
            self.assertEqual(service.attach_board(board)["picks"][0]["symbol"], "OTHER")
            with patch.object(service, "config", return_value={**self.cfg, "rank_today_first": False}):
                self.assertEqual([p["symbol"] for p in service.attach_board(board)["picks"]], ["AAPL", "OTHER"])
            with patch.object(service, "config", return_value={**self.cfg, "enabled": False}):
                self.assertIs(service.attach_board(board), board)
                self.assertFalse(service.stock_snapshot("AAPL")["available"])

    def test_fresh_supplement_deduplicates_and_refreshes_quote_and_probability(self):
        snapshot = {"session": "2026-10-09", "pattern_supported": True, "scores": {"AAPL": score(today_buy=True), "NEW": score("NEW", today_buy=True)},
                    "new_buy_candidates": [{"symbol": "AAPL", "current_price": 100, "calibrated_probability": .59}, {"symbol": "NEW", "calibrated_probability": .58}]}
        with patch.object(service, "cache_get", return_value=snapshot), patch.object(service, "most_recent_session", return_value=date(2026,10,9)):
            result = service.attach_board({"picks": [{"symbol": "AAPL", "current_price": 80, "calibrated_probability": .8}]})
        self.assertEqual(len(result["picks"]), 2)
        self.assertEqual(result["picks"][0]["current_price"], 100)
        self.assertEqual(result["picks"][0]["calibrated_probability"], .59)

    def test_training_input_changed_refuses_to_publish(self):
        with patch.object(service, "_read_daily_cache", return_value=bars()), patch.object(service, "most_recent_session", return_value=date(2026,10,9)), patch.object(service, "cache_set") as write:
            with self.assertRaisesRegex(ValueError, "validation_training_input_changed"):
                service.run_scoring(["AAPL"], {"training_hash": "wrong"})
            write.assert_not_called()

    def test_old_chart_snapshot_does_not_call_yesterdays_signal_today(self):
        snapshot = {"session": "2026-10-08", "scores": {"AAPL": score(today_buy=True)}, "charts": {"AAPL": [{"time": "2026-10-08"}]}}
        with patch.object(service, "cache_get", return_value=snapshot), patch.object(service, "most_recent_session", return_value=date(2026,10,9)):
            result = service.stock_snapshot("AAPL")
        self.assertFalse(result["score"]["today_buy"])
        self.assertEqual(result["score"]["status"], "stale")
        self.assertEqual(len(result["markers"]), 1)
        self.assertTrue(snapshot["scores"]["AAPL"]["today_buy"])


class VSwingApiTests(unittest.TestCase):
    def test_status_is_read_only_and_restart_interrupt_is_visible(self):
        import api_server as api
        with patch.object(api, "cache_get", side_effect=lambda k: {"status": "running"} if k == service.JOB_KEY else {}), patch.object(api, "_V_SWING_THREAD", None), patch.object(api, "cache_set", side_effect=AssertionError("read cannot write")):
            self.assertEqual(asyncio.run(api.v_swing_status())["job"]["status"], "interrupted")

    def test_start_deduplicates_using_live_handle_not_saved_status(self):
        import api_server as api
        with patch.object(api, "_V_SWING_THREAD", Mock(is_alive=Mock(return_value=True))), patch.object(api.threading, "Thread") as thread:
            self.assertTrue(api._start_v_swing()["already_running"])
            thread.assert_not_called()

    def test_worker_failure_keeps_previous_snapshot_and_bounds_subprocess(self):
        import api_server as api
        records = {service.SNAPSHOT_KEY: {"session": "old"}}
        with patch.object(api, "cache_set", side_effect=lambda k,v: records.update({k:v})), patch.object(api, "_daily_report_price_coverage", return_value={"symbols": ["AAPL"]}), patch.object(api, "_auto_scan_universe_ids", return_value=[]), patch.object(api.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout='{"error_type":"ValueError"}')) as run:
            api._v_swing_run_worker("test")
        self.assertEqual(run.call_args.kwargs["timeout"], 240)
        self.assertEqual(records[service.SNAPSHOT_KEY]["session"], "old")
        self.assertEqual(records[service.JOB_KEY]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
