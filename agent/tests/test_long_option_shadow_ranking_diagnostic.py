"""Rank diagnostics remain offline and never redefine a live win rate."""

from pathlib import Path

import pandas as pd
import pytest

from scripts import diagnose_long_option_shadow_ranking as diagnostic


def _event(symbol, score, net, *, day="2025-01-03", direction=None):
    side = direction or ("C" if score > 0 else "P" if score < 0 else "WAIT")
    return {"symbol": symbol, "signal_date": day,
            "phase": "retrospective_2025_plus_previously_inspected",
            "direction": side, "net_consensus": score,
            "signed_net_5": net if side != "WAIT" else None,
            "signed_spy_excess_5": net - 0.001 if side != "WAIT" else None,
            "policy_net_5": net if side != "WAIT" else 0.0,
            "always_call_net_5": 0.01}


def test_rank_uses_absolute_score_and_paired_same_day_returns():
    events = [
        _event("A", -0.9, 0.03), _event("B", 0.8, 0.02),
        _event("C", 0.7, 0.01), _event("D", 0.6, -0.01),
        _event("E", 0.5, -0.02), _event("F", 0.4, -0.03),
        _event("G", 0.99, 0.8, day="2025-01-06"),
    ]
    result = diagnostic.diagnose(events, bootstrap=0)
    phase = result["phases"]["retrospective_2025_plus_previously_inspected"]
    assert phase["top1_vs_rest"]["eligible_days"] == 1
    assert phase["top1_vs_rest"]["top_mean_net"] == pytest.approx(0.03)
    assert phase["top1_vs_rest"]["top_minus_rest"] == pytest.approx(0.036)
    assert phase["top3_vs_rest"]["top_mean_net"] == pytest.approx(0.02)
    assert phase["top3_vs_rest"]["rest_mean_net"] == pytest.approx(-0.02)
    assert phase["top3_vs_rest"]["top_minus_matched_always_call"] == pytest.approx(0.01)
    assert phase["put_top1_vs_put_rest"]["eligible_days"] == 0
    assert phase["call_top3_vs_call_rest"]["eligible_days"] == 0


def test_strength_bins_keep_call_put_wait_separate():
    events = [_event("A", 0.20, 0.02), _event("B", 0.60, -0.04),
              _event("C", -0.30, -0.03), _event("D", 0.0, None)]
    phase = diagnostic.diagnose(events, bootstrap=0)["phases"][
        "retrospective_2025_plus_previously_inspected"]
    assert phase["score_strength_by_side"]["C"]["0.12-0.25"]["events"] == 1
    assert phase["score_strength_by_side"]["P"]["0.25-0.50"]["events"] == 1
    assert phase["wait"]["events"] == 1
    assert phase["wait"]["policy_mean_per_candidate"] == 0
    assert phase["put"]["active_mean_net"] == pytest.approx(-0.03)


def test_spy_regime_uses_only_signal_day_and_previous_closes():
    days = pd.bdate_range("2024-01-02", periods=225)
    close = [100.0] * 200 + [90.0] * 25
    spy = pd.DataFrame({"Close": close}, index=days)
    observed = diagnostic.spy_regimes(spy)
    signal = days[210].date().isoformat()
    assert observed[signal] == {"below_ma200": True, "negative_prior20": True}
    spy.loc[days[220], "Close"] = 1000.0
    assert diagnostic.spy_regimes(spy)[signal] == observed[signal]


def test_put_regime_unknown_stays_unknown():
    events = [_event("A", -0.5, 0.02, day="2025-01-03"),
              _event("B", -0.7, -0.03, day="2025-01-06")]
    phase = diagnostic.diagnose(
        events, regimes={"2025-01-03": {"below_ma200": True, "negative_prior20": True}},
        bootstrap=0)["phases"]["retrospective_2025_plus_previously_inspected"]
    regimes = phase["put_by_spy_regime"]
    assert regimes["put_events_with_spy_context"] == 1
    assert regimes["put_events_without_spy_context"] == 1
    assert regimes["below_ma200_and_prior20_negative"]["active_mean_net"] == pytest.approx(0.02)
    assert regimes["other_spy_states"]["events"] == 0


def test_csv_rejects_duplicate_events_and_mismatched_direction(monkeypatch):
    path = Path("unused.csv")
    row = _event("A", 0.2, 0.01)
    monkeypatch.setattr(diagnostic.pd, "read_csv", lambda *_args, **_kwargs: pd.DataFrame([row, row]))
    with pytest.raises(ValueError, match="duplicate"):
        diagnostic.read_events(path, 5)
    monkeypatch.setattr(diagnostic.pd, "read_csv", lambda *_args, **_kwargs: pd.DataFrame(
        [{**row, "direction": "P"}]))
    with pytest.raises(ValueError, match="direction"):
        diagnostic.read_events(path, 5)
