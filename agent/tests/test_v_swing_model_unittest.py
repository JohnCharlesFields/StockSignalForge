import unittest
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

import v_swing_model as model
from v_swing_validation import profit_calibration, settle_sell, portfolio_audit


def bars(n=100):
    close = np.linspace(100, 110, n)
    return pd.DataFrame({"Open": close-.2, "High": close+1, "Low": close-1, "Close": close,
                         "Volume": np.arange(n)+10000}, index=pd.bdate_range("2026-04-01", periods=n))


class VSwingModelTests(unittest.TestCase):
    def test_portfolio_cash_capacity_costs_and_no_future_position_sizing(self):
        frame = pd.DataFrame({"Open": [100, 100, 101], "High": [101, 104, 105], "Low": [99, 99, 100],
                              "Close": [100, 102, 104], "Volume": [1000]*3}, index=pd.to_datetime(["2026-10-07", "2026-10-08", "2026-10-09"]))
        plan = {"entry": 100., "stop": 95., "target": 110.}
        outcome = {"status": "pending"}
        events = [{"symbol": str(i), "side": "buy", "signal_date": "2026-10-07", "p": i/10,
                   "plan": plan, "outcome": outcome, "next_close_sensitivity": outcome} for i in range(7)]
        report = portfolio_audit({str(i): frame for i in range(7)}, events, model.config(), "next_open")
        self.assertEqual(report["open_positions"], 5)
        self.assertEqual(report["skipped_capacity_or_conflict"], 2)
        self.assertTrue(all(r["cash"] >= -1e-12 for r in report["curve"]))
        self.assertLess(report["net_return"], .04)
        self.assertGreater(report["net_return"], .038)
        changed = frame.copy()
        changed.loc["2026-10-08", "Close"] = 1000
        more = portfolio_audit({str(i): changed for i in range(7)}, events, model.config(), "next_open")
        self.assertAlmostEqual(more["curve"][1]["cash"], report["curve"][1]["cash"])

    def test_annotation_schema_and_actual_17_feature_order(self):
        supplied = {"AAPL": {"2026-05-01": "buy", "2026-05-04": "sell"}}
        with patch.object(model, "annotations", return_value=supplied):
            _, audit = model.training_panel({"AAPL": bars()}, "2026-06-30")
        self.assertEqual((audit["supplied_labels"], audit["supplied_buy"], audit["supplied_sell"]), (2, 1, 1))
        self.assertEqual(len(model.FEATURES), 17)
        self.assertEqual(list(model.features(bars()).columns), model.FEATURES)

    def test_features_are_prefix_invariant_with_future_extreme_bars(self):
        frame = bars()
        expected = model.features(frame.iloc[:60])
        frame.iloc[60:, :4] *= 100
        frame.iloc[60:, 4] *= 1000
        pd.testing.assert_frame_equal(expected, model.features(frame).iloc[:60])

    def test_rsi_limits_and_zero_volume_no_infinity(self):
        frame = bars()
        frame.Volume = 0
        f = model.features(frame)
        self.assertEqual(f.rsi14.iloc[-1], 100)
        self.assertTrue(f.vol_ratio.isna().all())
        frame[["Open", "High", "Low", "Close"]] = [100, 101, 99, 100]
        self.assertEqual(model.features(frame).rsi14.iloc[-1], 50)
        self.assertFalse(np.isinf(model.features(frame)).any(axis=None))

    def test_bad_bar_index_or_missing_fields_returns_no_features(self):
        for frame in [pd.DataFrame(), bars().reset_index(drop=True), bars().drop(columns="Low")]:
            self.assertTrue(model.features(frame).empty)
        frame = bars()
        frame.iloc[-1, frame.columns.get_loc("Low")] = 500
        self.assertTrue(model.features(frame).empty)

    def test_training_never_uses_after_cutoff_or_identity_exclusions(self):
        supplied = {"AAPL": {"2026-05-01": "buy", "2026-07-01": "sell"},
                    "SKHY": {"2026-05-01": "buy"}}
        self.addCleanup(patch.stopall)
        patch.object(model, "annotations", return_value=supplied).start()
        panel, audit = model.training_panel({"AAPL": bars(), "SKHY": bars()}, "2026-06-30")
        self.assertTrue((panel.date <= "2026-06-30").all())
        self.assertNotIn("SKHY", panel.symbol.tolist())
        self.assertTrue(all(e["date"] <= "2026-06-30" for e in audit["omitted"]))
        _, later = model.training_panel({"AAPL": bars(), "SKHY": bars()}, "2026-10-01")
        self.assertTrue(any(e["reason"] == "identity_unverified" for e in later["omitted"]))

    def test_empty_private_labels_fail_explicitly(self):
        with patch.object(model, "annotations", return_value={}):
            with self.assertRaisesRegex(ValueError, "private_annotations_required"):
                model.training_panel({}, "2026-10-09")

    def test_plan_risk_atr_box_and_position_units(self):
        frame = bars()
        plan = model.buy_plan(frame)
        self.assertLess(plan["stop"], plan["entry"])
        self.assertAlmostEqual(plan["target"]-plan["entry"], 2*(plan["entry"]-plan["stop"]))
        self.assertAlmostEqual(plan["one_pct_risk_position_pct"], 1/plan["stop_distance_pct"])
        self.assertTrue(plan["in_box"])

    def test_score_refuses_stale_or_training_period_and_two_sides_independent(self):
        frame = bars()
        stamp = frame.index[-1].date().isoformat()
        classifier = Mock()
        classifier.predict_proba.return_value = np.array([[.1, .9]])
        models = {"models": {"buy": classifier, "sell": classifier}, "thresholds": {"buy": .8, "sell": .8}, "cutoff": "2026-06-01"}
        out = model.score_day(models, "AAPL", frame, stamp)
        self.assertTrue(out["buy"] and out["sell"])
        self.assertIsNone(out["predicted_win_rate"])
        self.assertEqual(model.score_day(models, "AAPL", frame, "2026-12-01")["status"], "stale")
        models["cutoff"] = stamp
        self.assertEqual(model.score_day(models, "AAPL", frame, stamp)["status"], "training_period_not_forward")

    def test_same_bar_both_hits_is_stop_not_target(self):
        frame = pd.DataFrame({"Open": [100, 100], "High": [101, 125], "Low": [99, 85], "Close": [100, 110], "Volume": [1000, 1000]}, index=pd.to_datetime(["2026-07-01", "2026-07-02"]))
        out = model.settle_buy(frame, "2026-07-01", {"entry": 100, "stop": 90, "target": 120})
        self.assertEqual(out["exit_reason"], "stop")
        self.assertAlmostEqual(out["net_return"], -.101)

    def test_gap_through_stop_uses_open_and_entry_gap_skips(self):
        frame = pd.DataFrame({"Open": [100, 100, 80], "High": [101, 105, 85], "Low": [99, 95, 79], "Close": [100, 101, 82], "Volume": [1000]*3}, index=pd.bdate_range("2026-07-01", periods=3))
        out = model.settle_buy(frame, "2026-07-01", {"entry": 100, "stop": 90, "target": 120})
        self.assertEqual(out["exit_reason"], "gap_stop")
        self.assertEqual(out["exit_price"], 80)
        frame.iloc[1, frame.columns.get_loc("Open")] = 125
        frame.iloc[1, frame.columns.get_loc("High")] = 130
        self.assertEqual(model.settle_buy(frame, "2026-07-01", {"entry": 100, "stop": 90, "target": 120})["status"], "skipped")

    def test_break_even_effective_next_bar_and_pending_not_zero_return(self):
        frame = pd.DataFrame({"Open": [100, 100, 105], "High": [101, 111, 108], "Low": [99, 95, 99], "Close": [100, 108, 101], "Volume": [1000]*3}, index=pd.bdate_range("2026-07-01", periods=3))
        plan = {"entry": 100, "stop": 90, "target": 120}
        pending = model.settle_buy(frame.iloc[:2], "2026-07-01", plan)
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["managed_stop"], 100)
        self.assertNotIn("net_return", pending)
        out = model.settle_buy(frame, "2026-07-01", plan)
        self.assertEqual(out["exit_reason"], "stop")
        self.assertEqual(out["exit_price"], 100)

    def test_next_close_does_not_use_entry_day_intraday_stop(self):
        frame = pd.DataFrame({"Open": [100, 100, 101], "High": [101, 125, 105], "Low": [99, 85, 95], "Close": [100, 100, 101], "Volume": [1000]*3}, index=pd.bdate_range("2026-07-01", periods=3))
        out = model.settle_buy(frame, "2026-07-01", {"entry": 100, "stop": 90, "target": 120}, execution="next_close")
        self.assertEqual(out["status"], "pending")

    def test_sell_is_not_short_return_and_calibration_purges_overlapping_exits(self):
        out = settle_sell(bars(), "2026-04-01", model.config())
        self.assertTrue(out["not_short_trade"])
        events = [{"signal_date": "2026-08-30", "symbol": "AAPL", "p": .8, "outcome": {"exit_date": "2026-09-02", "win": True}}]*100
        cal = profit_calibration(events, "2026-09-01")
        self.assertFalse(cal["eligible"])
        self.assertEqual(cal["train_n"], 0)


if __name__ == "__main__":
    unittest.main()
