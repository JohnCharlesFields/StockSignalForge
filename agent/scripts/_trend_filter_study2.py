"""v2: the user's two points TOGETHER -- chronic-below-EMA AND weak relative
strength (no hot money). Test if 'sunk + weak RS vs SPY' is the real falling-knife
(negative/no edge) while 'sunk + strong RS' (CAT-style) keeps the rebound edge.
"""
import sys, math, statistics
sys.path.insert(0, "agent"); sys.path.insert(0, "agent/scripts")
import pandas as pd, numpy as np
from research_signal_framework_backtest import _collect_symbol_events, _ticker_frame, _cluster_bootstrap_ci
from market_data_service import download_daily_history
from scripts.screening_framework_v2_optimized import resolve_universe

UNIV, PERIOD, H, MAXSYM, COOLDOWN, COST = "spx", "3y", 10, 180, 3, 0.001
RS_WIN = 20

tickers, source, _ = resolve_universe(UNIV); tickers = tickers[:MAXSYM]
print(f"symbols={len(tickers)} period={PERIOD} horizon={H}d RS_win={RS_WIN}", flush=True)
panel, _ = download_daily_history(tickers, period=PERIOD)
spy_panel, _ = download_daily_history(["SPY"], period=PERIOD)
spy_close = _ticker_frame(spy_panel, "SPY")["Close"].astype(float)
spy_ret = spy_close.pct_change(RS_WIN)
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
    ema20 = close.ewm(span=20, adjust=False).mean()
    frac20 = (close < ema20).astype(float).rolling(63).mean()
    sret = close.pct_change(RS_WIN)
    bydate = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(close.index)}
    for ev in evs:
        i = bydate.get(ev["date"])
        if i is None or ev.get("pullback_hv_score") is None or ev.get("excess_return") is None:
            continue
        f20 = frac20.iloc[i]
        srt = sret.iloc[i]
        spyr = spy_by.get(ev["date"])
        if not (math.isfinite(f20) and math.isfinite(srt)) or spyr is None or not math.isfinite(spyr):
            continue
        ev["frac20"] = float(f20)
        ev["rs"] = float(srt - spyr)  # relative strength vs SPY, 20d
        allev.append(ev)

scores = sorted(e["pullback_hv_score"] for e in allev)
q75 = scores[int(0.75 * len(scores))]
cohort = [e for e in allev if e["pullback_hv_score"] >= q75]
print(f"edge cohort n={len(cohort)} (score>={q75:.3f})\n", flush=True)


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


print("=== 2x2: chronic-below-EMA20 x relative strength vs SPY (within edge cohort) ===")
for fl, fh, flab in [(0.0, 0.5, "shallow(frac20<0.5)"), (0.5, 1.01, "SUNK(frac20>=0.5)")]:
    sub = [e for e in cohort if fl <= e["frac20"] < fh]
    show(f"{flab} & RS<0 (weak/no-hot-money)", stat([e for e in sub if e["rs"] < 0]))
    show(f"{flab} & RS>=0 (strong/hot-money) ", stat([e for e in sub if e["rs"] >= 0]))

print("\n=== finer: SUNK names (frac20>=0.5) by RS bucket ===")
sunk = [e for e in cohort if e["frac20"] >= 0.5]
for lo, hi, lab in [(-9, -0.05, "RS<-5%"), (-0.05, 0.0, "RS[-5%,0)"), (0.0, 0.05, "RS[0,5%)"), (0.05, 9, "RS>=5%")]:
    show(f"SUNK & {lab}", stat([e for e in sunk if lo <= e["rs"] < hi]))

print("\n=== filter test: does dropping 'SUNK & RS<0' BEAT base? ===")
show("BASE cohort", stat(cohort))
show("KEEP all EXCEPT (SUNK & RS<0)", stat([e for e in cohort if not (e["frac20"] >= 0.5 and e["rs"] < 0)]))
show("the DROPPED set (SUNK & RS<0)", stat([e for e in cohort if e["frac20"] >= 0.5 and e["rs"] < 0]))
