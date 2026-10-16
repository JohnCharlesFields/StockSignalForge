from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from gex_quality import normalize_gex, snapshot_gex_view
from scripts import screening_framework_v2_optimized as screening


def chain(oi=100, strike=100):
    return pd.DataFrame({"strike": [strike], "openInterest": [oi], "impliedVolatility": [.3]})


def valid_gex(net=10):
    return {"available": True, "net_gex": net, "call_gex": -10., "put_gex": 10. + net,
            "regime": "positive" if net > 0 else "neutral", "risk_level": "low", "gamma_flip": 80.}


class GexQualityTests(unittest.TestCase):
    def summary(self, calls, puts):
        with patch.dict(screening.CONFIG, {"gex_enabled": True}):
            return screening.gamma_exposure_summary(calls, puts, 100, .3, 30)

    def test_empty_chain_is_unavailable_not_neutral(self):
        result = self.summary(pd.DataFrame(), pd.DataFrame())
        self.assertFalse(result["available"])
        self.assertIsNone(result["gamma_flip"])
        self.assertIsNone(result["net_gex"])
        self.assertEqual(result["regime"], "unknown")

    def test_missing_oi_is_not_zero(self):
        result = self.summary(chain().drop(columns="openInterest"), chain())
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "invalid_or_missing_open_interest")

    def test_invalid_oi_fails_closed_including_mixed_chain(self):
        for oi in (None, np.nan, np.inf, -np.inf, -1, "bad", True):
            with self.subTest(oi=oi):
                result = self.summary(chain(oi), chain())
                self.assertFalse(result["available"])
                self.assertIsNone(result["net_gex"])
                self.assertEqual(result["risk_level"], "unknown")

    def test_known_zero_oi_does_not_count_as_exposure(self):
        result = self.summary(chain(0), chain(0))
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "no_open_interest_exposure")

    def test_bad_strike_or_iv_does_not_crash(self):
        for frame in (chain(strike=np.nan), chain(strike=-1), chain().assign(impliedVolatility="bad")):
            self.assertFalse(self.summary(frame, chain())["available"])

    def test_bad_spot_or_dte_is_unavailable(self):
        for spot, dte in ((np.nan, 30), (0, 30), (100, 0), (100, np.inf)):
            self.assertFalse(screening.gamma_exposure_summary(chain(), chain(), spot, .3, dte)["available"])

    def test_valid_balanced_exposure_retains_zero_without_flip(self):
        result = self.summary(chain(), chain())
        self.assertTrue(result["available"])
        self.assertEqual(result["valid_oi_contracts"], 2)
        self.assertEqual(result["net_gex"], 0)
        self.assertLess(result["call_gex"], 0)
        self.assertGreater(result["put_gex"], 0)
        self.assertEqual(result["regime"], "neutral")
        self.assertIsNone(result["gamma_flip"])
        self.assertIsNone(result["distance_to_flip"])

    def test_valid_call_put_exposure_preserves_original_sign_and_size(self):
        for calls, puts, sign in ((chain(), pd.DataFrame(), -1), (pd.DataFrame(), chain(), 1)):
            result = self.summary(calls, puts)
            self.assertTrue(result["available"])
            expected = sign * screening.bs_gamma(100, 100, 30 / 365.25, screening.CONFIG["risk_free_rate"], .3) * 100 * 100 * 100 * 100 / 100
            self.assertAlmostEqual(result["net_gex"], round(expected, 2))

    def test_zero_curve_has_no_flip_but_real_crossing_is_retained(self):
        with patch.object(screening, "estimate_net_gex_at_price", return_value=0):
            self.assertIsNone(screening.estimate_gamma_flip(chain(), chain(), 100, .3, 30))
        with patch.object(screening, "estimate_net_gex_at_price", side_effect=lambda c, p, price, *args: price - 100):
            self.assertEqual(screening.estimate_gamma_flip(chain(), chain(), 100, .3, 30), 100)

    def test_legacy_flag_alone_and_nonfinite_exposure_are_rejected(self):
        for raw in ({"available": True, "regime": "positive", "risk_level": "low"},
                    {**valid_gex(), "net_gex": np.nan}, {**valid_gex(), "call_gex": None},
                    {**valid_gex(), "net_gex": 25}, {**valid_gex(), "net_gex": 0, "call_gex": 0, "put_gex": 0}):
            result = normalize_gex(raw)
            self.assertFalse(result["available"])
            self.assertIsNone(result["net_gex"])
            self.assertEqual(result["regime"], "unknown")

    def test_legacy_true_zero_is_kept_and_input_not_modified(self):
        raw = valid_gex(0)
        before = deepcopy(raw)
        result = normalize_gex(raw)
        self.assertTrue(result["available"])
        self.assertEqual(result["net_gex"], 0)
        self.assertIsNone(result["gamma_flip"])
        self.assertEqual(raw, before)

    def test_snapshot_display_invalidates_derived_score_only(self):
        raw = {"snapshot_id": "old", "payload": {"rows": [{"symbol": "NVDA", "gamma_exposure": {"available": True, "net_gex": None},
                  "gex_level": "low", "gex_regime": "positive", "unified_probability": .54, "calibrated_probability": .6, "priority_score": .4}]}}
        before = deepcopy(raw)
        result = snapshot_gex_view(raw)
        row = result["payload"]["rows"][0]
        self.assertFalse(row["gamma_exposure"]["available"])
        self.assertIsNone(row["gex_level"])
        self.assertIsNone(row["unified_probability"])
        self.assertEqual(row["calibrated_probability"], .6)
        self.assertEqual(row["priority_score"], .4)
        self.assertEqual(raw, before)


class GexConsumerTests(unittest.TestCase):
    def test_invalid_gex_not_in_coverage_or_evidence(self):
        import api_server as api
        bad = {"available": True, "net_gex": np.nan, "call_gex": np.nan, "put_gex": np.nan, "regime": "positive", "risk_level": "low"}
        rows = [{"ticker": "TEST", "gamma_exposure": bad, "trend_30d": .1}]
        with patch.object(api, "_stock_signal_filter_results", return_value=rows), patch.object(api, "_stock_signal_run_id", return_value="test"):
            metrics = api._universe_recent_win_metrics("test", (Path("test.json"), {}))
        self.assertEqual(metrics["gex_coverage"], 0)
        self.assertEqual(metrics["gex_available_count"], 0)
        with patch.object(api, "panic_evidence_for_opportunity", return_value=None), patch.object(api, "stock_panic_evidence", return_value=None):
            evidence = api._research_evidence_summary({"gamma_exposure": bad}, None, {})
        self.assertFalse(evidence["components"])

    def test_valid_gex_preserves_weight_and_probability(self):
        import api_server as api
        with patch.object(api, "panic_evidence_for_opportunity", return_value=None), patch.object(api, "stock_panic_evidence", return_value=None):
            evidence = api._research_evidence_summary({"gamma_exposure": valid_gex()}, None, {})
        component = evidence["components"][0]
        self.assertEqual(component["weight"], .10)
        self.assertEqual(component["probability"], .54)

    def test_compact_and_persisted_snapshot_read_clean_invalid_gex(self):
        import api_server as api
        bad = {"available": True, "net_gex": None, "regime": "positive", "risk_level": "low"}
        with patch.object(api, "get_cached_review", return_value=None):
            row = api._compact_research_context({"opportunity": {"gamma_exposure": bad}})
        self.assertFalse(row["gamma_exposure"]["available"])
        self.assertIsNone(row["gex_level"])
        with patch.object(api, "_stored_home_dashboard_snapshot", return_value={"payload": {"rows": [{"gamma_exposure": bad, "gex_level": "low"}]}}), patch.object(api, "cache_set", side_effect=AssertionError("write")):
            response = asyncio.run(api.get_home_dashboard_snapshot())
        self.assertIsNone(response["snapshot"]["payload"]["rows"][0]["gex_level"])

    def test_stock_candidate_does_not_trust_detached_gamma_risk_flag(self):
        from stock_signal_candidate_service import stock_signal_item
        result = stock_signal_item({"ticker": "TEST", "spot": 100, "trend_30d": .1, "liquidity_score": .9,
                                   "gamma_risk_level": "high", "gamma_exposure": {"available": True, "net_gex": None}}, {}, "run")
        self.assertEqual(result["gamma_risk_level"], "unknown")
        self.assertFalse(result["gamma_exposure"]["available"])

    def test_invalid_gex_does_not_reduce_stock_panic(self):
        import stock_panic_service as panic
        opportunity = {"recent_closes": [100 + np.sin(i) for i in range(40)]}
        regime = {"available": True, "value": 28, "mode": "defensive"}
        with patch.object(panic, "_market_returns", return_value=pd.Series([.001] * 40)):
            bad = panic.stock_panic_proxy("TEST", {**opportunity, "gamma_exposure": {"available": True, "regime": "positive", "risk_level": "low"}}, macro_regime=regime)
            good = panic.stock_panic_proxy("TEST", {**opportunity, "gamma_exposure": valid_gex()}, macro_regime=regime)
        self.assertEqual(bad["gex_penalty"], 0)
        self.assertEqual(good["gex_penalty"], -2)

    def test_invalid_gex_does_not_penalize_macro_evidence(self):
        from macro_panic_service import panic_evidence_for_opportunity
        regime = {"available": True, "mode": "panic_reclaim", "value": 40}
        baseline = panic_evidence_for_opportunity({}, regime)
        bad = panic_evidence_for_opportunity({"gamma_exposure": {"available": True, "risk_level": "high"}}, regime)
        good = panic_evidence_for_opportunity({"gamma_exposure": {**valid_gex(-5), "risk_level": "high", "regime": "negative_high"}}, regime)
        self.assertEqual(bad["probability"], baseline["probability"])
        self.assertAlmostEqual(good["probability"], baseline["probability"] - .04)

    def test_legacy_priority_sentiment_cannot_be_reused_without_exposure(self):
        import priority_board_service as board
        self.assertEqual(board._options_sentiment({"gex_regime": "positive"})["tone"], "unknown")
        self.assertEqual(board._options_sentiment({"gamma_exposure": valid_gex()})["tone"], "bullish")


if __name__ == "__main__":
    unittest.main()
