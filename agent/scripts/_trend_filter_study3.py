"""v3 confirmation: WITHIN the pullback_hv edge cohort, does preferring RS>=0
beat the base edge (CI excluding 0)? This is the discipline test for whether to
lift relative-strength from a tiebreaker to a ranking driver. Reconcile with the
board's prior 'chasing' note by also checking the high-RS extreme.
"""
import sys, math, statistics
sys.path.insert(0, "agent"); sys.path.insert(0, "agent/scripts")
import pandas as pd, numpy as np
from research_signal_framework_backtest import _collect_symbol_events, _ticker_frame, _cluster_bootstrap_ci
from market_data_service import download_daily_history
from scripts.screening_framework_v2_optimized import resolve_universe

UNIV, PERIOD, H, MAXSYM, COOLDOWN, COST, RS_WIN = "spx", "3y", 10, 180, 3, 0.001, 20
tickers, _, _ = resolve_universe(UNIV); tickers = tickers[:MAXSYM]
panel, _ = download_daily_history(tickers, period=PERIOD)
spy_panel, _ = download_daily_history(["SPY"], period=PERIOD)
spy_ret = _ticker_frame(spy_panel, "SPY")["Close"].astype(float).pct_change(RS_WIN)
spy_by = {d.strftime("%Y-%m-%d"): spy_ret.iloc[i] for i, d in enumerate(spy_ret.index)}

allev = []
for tk in tickers:
    try:
        frame = _ticker_frame(panel, tk)
    except Exception:
        continue
    if frame is None or frame.empty or "Close" not in frame or len(frame) < 130:
        continue
    evs = _collect_symbol_events(tk, frame, H, min_launch_score=0.0, min_tunnel_score=0.0, cooldown_days=COOLDOWN)
    if not evs:
        continue
    close = frame.dropna(subset=["Close"])["Close"].astype(float)
    sret = close.pct_change(RS_WIN)
    bydate = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(close.index)}
    for ev in evs:
        i = bydate.get(ev["date"])
        if i is None or ev.get("pullback_hv_score") is None or ev.get("excess_return") is None:
            continue
        srt = sret.iloc[i]; spyr = spy_by.get(ev["date"])
        if not math.isfinite(srt) or spyr is None or not math.isfinite(spyr):
            continue
        ev["rs"] = float(srt - spyr); allev.append(ev)

scores = sorted(e["pullback_hv_score"] for e in allev)
q75 = scores[int(0.75 * len(scores))]
cohort = [e for e in allev if e["pullback_hv_score"] >= q75]


def stat(evs):
    if len(evs) < 30:
        return None
    ex = [e["excess_return"] - COST for e in evs]
    lo, hi = _cluster_bootstrap_ci([{"ticker": e["ticker"], "excess_return": e["excess_return"] - COST} for e in evs])
    return dict(n=len(evs), clu=len({e["ticker"] for e in evs}), mean=statistics.mean(ex), lo=lo, hi=hi,
                hit=sum(1 for x in ex if x > 0) / len(ex))


def show(label, s):
    if not s:
        print(f"  {label}: (n<30)"); return
    sig = "  EDGE" if s["lo"] > 0 else ("  NEGATIVE" if s["hi"] < 0 else "  no-edge(CI~0)")
    print(f"  {label}: n={s['n']:4d} clu={s['clu']:3d} net={s['mean']*100:+.2f}% CI[{s['lo']*100:+.2f},{s['hi']*100:+.2f}] hit={s['hit']:.0%}{sig}")


print(f"cohort n={len(cohort)}")
print("\n=== ranking test: prefer RS>=0 within pullback_hv cohort ===")
show("BASE cohort", stat(cohort))
show("KEEP RS>=0", stat([e for e in cohort if e["rs"] >= 0]))
show("DROP (RS<0)", stat([e for e in cohort if e["rs"] < 0]))

print("\n=== reconcile 'chasing': full cohort by RS quintile ===")
rs_sorted = sorted(cohort, key=lambda e: e["rs"])
n = len(rs_sorted); q = n // 5
for k in range(5):
    seg = rs_sorted[k * q:(k + 1) * q if k < 4 else n]
    rlo, rhi = seg[0]["rs"], seg[-1]["rs"]
    show(f"RS quintile {k+1} [{rlo*100:+.1f}%,{rhi*100:+.1f}%]", stat(seg))
