from datetime import date, datetime
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import app_database as db
from pullback_parameter_service import activate_candidate, purged_split, predict_pick, settings, train_candidate, evaluate
from pullback_validation import EASTERN, known_open_baseline, next_session_open, block_interval


class ParameterIntegrityTests(unittest.TestCase):
    def test_forecast_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(db, "DB_PATH", Path(tmp)/"test.sqlite"), patch.object(db, "_INITIALIZED", False):
            row = {"as_of_date": "2026-10-07", "symbol": "NVDA", "rank": 1, "horizon_days": 5,
                   "calibrated_prob": .6, "forecast": {"version": "first"}, "evaluation_version": "recorded_open_v3"}
            self.assertEqual(db.predictions_log_many([row]), 1)
            self.assertEqual(db.predictions_log_many([{**row, "rank": 99, "calibrated_prob": .1, "forecast": {"version": "changed"}}]), 0)
            with db._connect() as conn:
                saved = conn.execute("SELECT rank,calibrated_prob,forecast_json FROM predictions").fetchone()
            self.assertEqual(saved["rank"], 1)
            self.assertEqual(saved["calibrated_prob"], .6)
            self.assertIn("first", saved["forecast_json"])

    def test_baseline_is_invariant_to_future_prices(self):
        dates = pd.bdate_range("2025-01-01", periods=400)
        frame = pd.DataFrame({"Open": np.linspace(100, 130, len(dates))}, index=dates)
        day = dates[300].date()
        first = known_open_baseline(frame, day, 5)
        frame.loc[frame.index > pd.Timestamp(day), "Open"] = 99999
        self.assertEqual(known_open_baseline(frame, day, 5), first)

    def test_after_market_prediction_never_uses_prior_close(self):
        self.assertEqual(next_session_open(datetime(2026,10,7,21,0,tzinfo=EASTERN), date(2026,10,7)), date(2026,10,8))

    def test_purge_covers_all_symbols_and_label_exit(self):
        dates = pd.bdate_range("2025-01-01", periods=40)
        rows = [{"date": d.date().isoformat(), "exit_date": (d+pd.offsets.BDay(5)).date().isoformat(), "symbol": symbol}
                for d in dates for symbol in ("A", "B")]
        start = dates[30].date().isoformat()
        train, test = purged_split(rows, start, gap_sessions=10)
        self.assertTrue(all(r["exit_date"] < start for r in train))
        self.assertTrue(all(r["date"] < dates[20].date().isoformat() for r in train))
        self.assertEqual(len(test), 20)

    def test_unknown_provenance_blocks_automatic_activation(self):
        with patch("pullback_parameter_service.cache_set") as write:
            self.assertFalse(activate_candidate({"eligible_for_activation": True, "provenance": {}}, {"auto_activate": True}))
            write.assert_not_called()

    def test_negative_holdout_cannot_activate_even_if_flag_is_true(self):
        prov = {k:True for k in ("point_in_time_universe", "corporate_actions_verified", "unseen_holdout", "account_backtest_verified")}
        with patch("pullback_parameter_service.cache_set") as write:
            self.assertFalse(activate_candidate({"eligible_for_activation": True, "provenance":prov, "holdout":{"ci95":[-.1,.1],"mean_net":-.01}}, {"auto_activate":True}))
            write.assert_not_called()

    def test_many_records_do_not_override_too_few_dates(self):
        self.assertEqual(block_interval({str(i): [.01]*200 for i in range(15)}, block_days=8), [None,None])

    def test_serialized_model_predicts_net_and_probability_separately(self):
        active = {"parameter_version":"test", "parameters":{"features":["oversold"], "center":[0], "scale":[1], "weights":[.02], "intercept":0, "entry_threshold":0, "horizon_days":5,
                  "probability":{"center":[0],"scale":[1],"weights":[0],"intercept":0}}}
        prediction = predict_pick({"raw_launch_score":.4,"hv":{"hv_rise":.1}}, active)
        self.assertAlmostEqual(prediction["expected_net_return"], .012)
        self.assertAlmostEqual(prediction["positive_net_probability"], .5)

    def test_no_trade_dates_remain_in_selection_metric(self):
        rows = [{"date":str(i), "symbol":"A", "net_return":.1, "net_excess":.1, "beta_adjusted_alpha":.1} for i in range(20)]
        metrics = evaluate(rows, [1]+[-1]*19, {"entry_threshold":0, "top_n":1, "training_horizon_days":5})
        self.assertEqual(metrics["dates"], 20)
        self.assertAlmostEqual(metrics["mean_daily_basket_net"], .005)

    def test_holdout_labels_cannot_change_chosen_weights(self):
        config = {**settings(), "min_training_dates":30, "validation_dates":20, "holdout_dates":30,
                  "regularization_grid":[1.0], "entry_threshold_grid":[0], "top_n":1}
        dates = pd.bdate_range("2024-01-01", periods=240)
        rows = [{"date":day.date().isoformat(), "exit_date":(day+pd.offsets.BDay(5)).date().isoformat(),
                 "symbol":sym, "oversold":value, "hv_norm":.5, "market_tag":0, "sector_tag":0,
                 "net_return":.02*(value-.5), "net_excess":.02*(value-.5), "beta_adjusted_alpha":.02*(value-.5)}
                for day in dates for sym,value in (("A",.8),("B",.2))]
        first = train_candidate(rows, config, {})
        changed = [{**r, "net_return":-r["net_return"]} if r["date"] >= first["sample"]["holdout_start"] else r for r in rows]
        second = train_candidate(changed, config, {})
        self.assertEqual(first["parameters"], second["parameters"])
        self.assertFalse(first["eligible_for_activation"])

    def test_scorecard_separates_versions_and_reports_negative_direction(self):
        import prediction_ledger_service as ledger
        row = {"as_of_date":"2026-09-22", "symbol":"A", "rank":1, "horizon_days":5,
               "calibrated_prob":.6, "win":0, "forward_return":-.02, "excess_return":-.02,
               "net_excess":-.021, "beta_adjusted_alpha":-.021, "evaluation_version":"legacy", "net_return":None}
        rows = [row, {**row,"as_of_date":"2026-09-23"}, {**row,"symbol":"B","evaluation_version":"recorded_open_v3","net_excess":.1}]
        with patch.object(ledger,"predictions_count",return_value={}), patch.object(ledger,"predictions_resolved",return_value=rows), \
                patch.object(ledger,"predictions_open",return_value=[]), patch.object(ledger,"_calibration_audit",return_value={}), \
                patch.object(ledger,"cache_set"), patch.object(ledger,"cache_get",return_value=None):
            result = ledger.prediction_scorecard(force=True)
        self.assertEqual(result["n_resolved"], 2)
        self.assertEqual(result["evidence_direction"], "negative")
        self.assertEqual(result["overlap_adjusted_ci"], [None,None])


if __name__ == '__main__':
    unittest.main()
