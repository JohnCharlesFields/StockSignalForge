"""Regression contracts: unavailable future data cannot change past decisions."""
from datetime import date
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtest.engines.global_equity import GlobalEquityEngine
from backtest.optimizers.equal_volatility import EqualVolatilityOptimizer
import exit_model
from market_data_service import _normalize_daily, _read_daily_cache, _write_daily_cache
from peer_earnings_signal_service import build_peer_earnings_signals_for_test
from scripts.research_signal_framework_backtest import _collect_symbol_events
from scripts import calibrate_playbook_soft_features as soft
from scripts import resolve_signal_events as resolver


def bars(n=180):
    rng = np.random.default_rng(74)
    close = 100 * np.exp(np.cumsum(rng.normal(.0007, .01, n)))
    open_ = close * (1 + rng.normal(0, .002, n))
    return pd.DataFrame({"Open": open_, "High": np.maximum(open_, close) * 1.01,
                         "Low": np.minimum(open_, close) * .99, "Close": close,
                         "Volume": rng.integers(100_000, 1_000_000, n)},
                        index=pd.bdate_range("2024-01-02", periods=n))


def test_cleaning_preserves_prefix_and_raw_suspect(monkeypatch, tmp_path):
    import market_data_service as market
    monkeypatch.setattr(market, "_CACHE_ROOT", tmp_path)
    frame = bars(4)
    frame["Close"] = [100., 100., 1000., 100.]
    prefix = _normalize_daily(frame.iloc[:3])
    full = _normalize_daily(frame)
    pd.testing.assert_frame_equal(prefix, full.iloc[:3])
    assert prefix.iloc[-1].Close == 1000
    assert prefix.attrs["price_jump_suspects"][0]["date"] == str(frame.index[2].date())
    _write_daily_cache("TEST", frame)
    assert _read_daily_cache("TEST").loc[frame.index[2], "Close"] == 1000


@pytest.mark.parametrize("mode", ["close_to_open", "close_to_close"])
def test_replay_matured_labels_do_not_depend_on_later_prices(monkeypatch, mode):
    monkeypatch.setenv("SIGNAL_EXIT_MODE", mode)
    frame = bars()
    changed = frame.copy()
    changed.iloc[130:, :4] *= np.exp(np.linspace(0, 2, 50))[:, None]
    kwargs = dict(min_launch_score=0, min_tunnel_score=0, cooldown_days=1)
    before = _collect_symbol_events("TEST", frame, 10, **kwargs)
    after = _collect_symbol_events("TEST", changed, 10, **kwargs)
    cutoff = str(frame.index[119].date())
    assert [r for r in before if r["date"] < cutoff] == [r for r in after if r["date"] < cutoff]
    assert before[0]["baseline_method"] == "known_matured_expanding_v1"
    assert before[0]["label_available_at"] == before[0]["exit_date"]


def test_known_baseline_uses_only_matured_convention(monkeypatch):
    monkeypatch.setenv("SIGNAL_EXIT_MODE", "close_to_open")
    close = pd.Series([100., 101., 102., 103., 104.])
    open_ = pd.Series([100., 110., 120., 130., 140.])
    result = exit_model.known_baseline_series(close, open_, 2)
    assert result.iloc[:2].isna().all()
    assert result.iloc[2] == pytest.approx(.2)
    assert result.iloc[3] == pytest.approx(np.mean([.2, 130 / 101 - 1]))
    assert result.iloc[4] == pytest.approx(np.mean(exit_model.baseline_forward_returns(close, open_, 2)))


def test_long_horizon_replay_does_not_fill_unknown_baseline_with_zero():
    frame = bars()
    events = _collect_symbol_events("TEST", frame, 60, min_launch_score=0, min_tunnel_score=0, cooldown_days=1)
    assert events
    assert all(e["date"] >= str(frame.index[60].date()) and e["excess_return"] is not None for e in events)


def test_event_resolver_freezes_baseline_at_signal_day(monkeypatch):
    frame = bars()
    event = {"ticker": "TEST", "symbol": "TEST", "horizon_days": 10,
             "as_of_date": str(frame.index[60].date())}
    monkeypatch.setattr(resolver, "get_daily_history", lambda *a, **k: (frame, "synthetic"))
    before = resolver._resolve_one(event, "2y")
    later = frame.copy()
    later.iloc[100:, :4] *= 20
    monkeypatch.setattr(resolver, "get_daily_history", lambda *a, **k: (later, "synthetic"))
    assert before == resolver._resolve_one(event, "2y")


def test_event_resolver_keeps_missing_signal_session_pending(monkeypatch):
    frame = bars()
    day = str(frame.index[60].date())
    monkeypatch.setattr(resolver, "get_daily_history", lambda *a, **k: (frame.drop(frame.index[60]), "synthetic"))
    assert resolver._resolve_one({"symbol": "TEST", "as_of_date": day, "horizon_days": 10}, "2y") is None


def test_open_weights_ignore_same_session_close():
    idx = pd.bdate_range("2026-01-02", periods=15)
    ret = pd.DataFrame({"A": np.sin(np.arange(15)) * .01,
                        "B": np.cos(np.arange(15)) * .01}, index=idx)
    positions = pd.DataFrame(1., index=idx, columns=["A", "B"])
    changed = ret.copy()
    changed.iloc[-1, 0] = .9
    opt = EqualVolatilityOptimizer(lookback=5)
    pd.testing.assert_series_equal(opt.optimize(ret, positions, idx).iloc[-1],
                                   opt.optimize(changed, positions, idx).iloc[-1])


def test_open_position_size_and_execution_guard_ignore_today_close():
    class RecordingEngine(GlobalEquityEngine):
        def __init__(self):
            super().__init__({"initial_cash": 10000, "slippage_us": 0})
            self.entries = []
            self.checked = []

        def can_execute(self, symbol, direction, bar):
            self.checked.append((symbol, float(bar["close"])))
            return True

        def on_bar(self, symbol, bar, timestamp):
            if timestamp == dates[-1]:
                self.entries.append({s: p.size for s, p in self.positions.items()})

    dates = pd.bdate_range("2026-01-02", periods=2)
    def run(last_close):
        engine = RecordingEngine()
        frames = {"A": pd.DataFrame({"open": [100., 100.], "close": [100., last_close]}, index=dates),
                  "B": pd.DataFrame({"open": [100., 100.], "close": [100., 100.]}, index=dates)}
        close = pd.DataFrame({s: f.close for s, f in frames.items()})
        weights = pd.DataFrame({"A": [.5, .5], "B": [0., .5]}, index=dates)
        engine._execute_bars(dates, frames, close, weights, ["A", "B"])
        return engine
    before, after = run(100), run(900)
    assert before.entries == after.entries
    assert before.checked == after.checked
    assert before.entries[-1]["B"] == 50


def test_intrabar_hook_cannot_reopen_at_earlier_open():
    class LiquidatingEngine(GlobalEquityEngine):
        def on_bar(self, symbol, bar, timestamp):
            self._close_position(symbol, float(bar.close), timestamp, "intrabar_test")
    dates = pd.bdate_range("2026-01-02", periods=2)
    frame = pd.DataFrame({"open": [100., 100.], "close": [100., 90.]}, index=dates)
    engine = LiquidatingEngine({"initial_cash": 10000, "slippage_us": 0})
    engine._execute_bars(dates, {"A": frame}, pd.DataFrame({"A": frame.close}),
                         pd.DataFrame({"A": [0., 1.]}, index=dates), ["A"])
    assert not engine.positions
    assert len(engine.trades) == 1
    assert engine.trades[0].exit_reason == "intrabar_test"


def test_peer_replay_honours_as_of_and_known_evidence():
    idx = pd.bdate_range("2026-04-20", periods=35)
    histories = {"DELL": pd.DataFrame({"Close": [100.] * 30 + [106., 110., 112., 113., 114.],
                                       "Volume": [100000] * 30 + [260000, 180000, 150000, 130000, 120000]}, index=idx),
                 "HPE": pd.DataFrame({"Close": [50.] * 30 + [50.5, 50.8, 51., 51.2, 51.4],
                                      "Volume": [90000] * 30 + [100000, 105000, 110000, 115000, 130000]}, index=idx)}
    groups = [{"id": "enterprise_servers", "label": "AI servers", "similarity": .96, "symbols": ["DELL", "HPE"]}]
    earnings = {"DELL": {"symbol": "DELL", "latest_report_date": "2026-06-01", "eps_surprise_pct": .18, "revenue_yoy": .22},
                "HPE": {"symbol": "HPE", "latest_report_date": "2026-03-01", "next_report_date": "2026-06-15"}}
    day = date(2026, 6, 1)
    assert build_peer_earnings_signals_for_test(groups, earnings, histories, day) == \
        build_peer_earnings_signals_for_test(groups, earnings, {s: f.loc[:str(day)] for s, f in histories.items()}, day)
    assert build_peer_earnings_signals_for_test(groups, earnings, histories, date(2026, 6, 5))
    earnings["DELL"]["available_at"] = "2026-06-06T12:00:00+00:00"
    assert not build_peer_earnings_signals_for_test(groups, earnings, histories, date(2026, 6, 5))


def load_template(name):
    path = Path(__file__).resolve().parents[1] / "src" / "skills" / name / "example_signal_engine.py"
    spec = importlib.util.spec_from_file_location("temporal_" + name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", ["elliott-wave", "harmonic"])
def test_wave_templates_never_backfill_past_signals(name):
    module = load_template(name)
    engine = module.SignalEngine(swing_window=2)
    if name == "harmonic":
        engine.use_pyharmonics = False
    frame = bars(100).rename(columns=str.lower)
    for end in (35, 50, 75):
        prefix = engine.generate({"TEST": frame.iloc[:end]})["TEST"]
        full = engine.generate({"TEST": frame})["TEST"]
        pd.testing.assert_series_equal(prefix, full.iloc[:end])


def test_elliott_abc_is_emitted_on_confirmation_not_pivot():
    module = load_template("elliott-wave")
    prices = np.interp(np.arange(49), [0, 8, 16, 24, 32, 40, 48], [140, 160, 110, 140, 90, 125, 130])
    frame = pd.DataFrame({"high": prices + 1, "low": prices - 1, "close": prices},
                         index=pd.bdate_range("2026-01-02", periods=49))
    signal = module.SignalEngine(swing_window=2, min_wave_bars=1, fib_tolerance=.25).generate({"TEST": frame})["TEST"]
    assert signal.iloc[32] == 0
    assert signal.iloc[34] == 1


def test_harmonic_detector_output_is_frozen_on_first_observation(monkeypatch):
    module = load_template("harmonic")
    engine = module.SignalEngine(swing_window=2)
    frame = bars(40).rename(columns=str.lower)
    def detect(prefix):
        return [{"pattern": "synthetic", "d_index": frame.index[32], "direction": "bullish"}] if len(prefix) >= 35 else []
    monkeypatch.setattr(engine, "_detect", detect)
    full = engine.generate({"TEST": frame})["TEST"]
    assert full.iloc[32] == 0
    assert full.iloc[34] == 1
    pd.testing.assert_series_equal(engine.generate({"TEST": frame.iloc[:33]})["TEST"], full.iloc[:33])


def test_soft_split_groups_dates_and_purges_unmatured_labels():
    dates = pd.bdate_range("2025-01-02", periods=40)
    rows = [{"_date": str(d.date()), "_label_available_date": str((d + pd.offsets.BDay(10)).date()),
             "ticker": symbol} for d in dates for symbol in ("A", "B")]
    train, test = soft._temporal_split(rows, .7, embargo_sessions=2)
    assert set(r["_date"] for r in train).isdisjoint(r["_date"] for r in test)
    start = min(r["_date"] for r in test)
    assert all(r["_label_available_date"] < start for r in train)
    assert train and test


def test_soft_split_rejects_unverified_label_dates(tmp_path):
    path = tmp_path / "events.json"
    path.write_text('{"events":[{"date":"2025-01-02","pullback_hv_score":0.8,"net_own_excess":0.01}]}')
    with pytest.raises(ValueError, match="exit_date|label_available"):
        soft._build_rows(path)


def test_soft_yearly_cv_purges_december_labels(monkeypatch):
    idx = pd.bdate_range("2024-01-02", "2025-02-28")
    rows = [{"_date": str(d.date()), "_year": d.year,
             "_label_available_date": str((d + pd.offsets.BDay(10)).date())} for d in idx]
    seen = []
    def evaluate(name, features, train, test):
        seen.append((train, test))
        return {"name": name, "skipped": True}
    monkeypatch.setattr(soft, "_evaluate_model", evaluate)
    # Multiple symbols give the existing 100-test-row minimum enough support.
    soft._yearly_cv(rows * 3, [("test", [])], min_train=50)
    assert seen
    for train, test in seen:
        assert max(r["_label_available_date"] for r in train) < min(r["_date"] for r in test)


def test_new_calibration_is_candidate_not_production_activation(monkeypatch):
    from scripts import build_signal_calibration as builder
    writes = []
    monkeypatch.setattr(builder, "signal_calibration_upsert", lambda **kw: writes.append(kw) or 1)
    rows = [{"ticker": f"T{i % 12}", "pullback_hv_score": i / 299,
             "forward_return": .01, "excess_return": (i / 299 - .5) * .1,
             "baseline_method": "known_matured_expanding_v1"} for i in range(300)]
    result = builder._build_for_signal(rows, signal_type="pullback_hv", horizon=10,
                                       cost_bps=3, universe_set="synthetic", source="replay")
    assert writes[0]["make_active"] is False
    assert result["activated"] is False
    assert result["curve"]["global"]["provenance"]["unseen_holdout"] is False


def test_short_horizon_derivation_freezes_event_baselines():
    from scripts import derive_playbook_short_horizon_events as derive
    frame = bars()
    payload = {"events": [{"ticker": "TEST", "date": str(frame.index[i].date()), "beta": 0}
                          for i in (60, 70)]}
    first = derive._derive_for_horizon(payload, 5, {"TEST": frame, "SPY": frame})["events"]
    later = frame.copy()
    later.iloc[100:, :4] *= 20
    second = derive._derive_for_horizon(payload, 5, {"TEST": later, "SPY": frame})["events"]
    assert first and first == second
    assert first[0]["symbol_baseline_return"] != first[1]["symbol_baseline_return"]


@pytest.mark.parametrize("name", ["build_pead_calibration", "build_fundamental_momentum_calibration"])
def test_earnings_research_baseline_does_not_read_later_prices(monkeypatch, name):
    import importlib
    module = importlib.import_module("scripts." + name)
    frame = bars()
    day = str(frame.index[60].date())
    if name == "build_pead_calibration":
        monkeypatch.setattr(module, "get_earnings_events", lambda *a, **k: [{"date": day, "surprise": .2}])
        monkeypatch.setattr(module, "get_daily_history", lambda *a, **k: (frame, "synthetic"))
    else:
        fins = [{"filing_date": day, "diluted_eps": 2}] + [{"diluted_eps": 1}] * 4
        monkeypatch.setattr(module, "get_financials", lambda *a, **k: fins)
        monkeypatch.setattr(module, "_massive_aggs", lambda *a, **k: frame)
    before = module._collect_symbol_events("TEST", [5])
    later = frame.copy()
    later.iloc[100:, :4] *= 20
    if name == "build_pead_calibration":
        monkeypatch.setattr(module, "get_daily_history", lambda *a, **k: (later, "synthetic"))
    else:
        monkeypatch.setattr(module, "_massive_aggs", lambda *a, **k: later)
    assert before[5] and before == module._collect_symbol_events("TEST", [5])
