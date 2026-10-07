"""Does a pullback signal on a CHRONIC-DOWNTREND name (long pinned below EMA)
have a worse forward excess than a dip-in-uptrend? Find the cutoff, with CI.

Hypothesis under test (user): a name closed below EMA20/EMA15 for most of the last
quarter ("长期沉底" / falling knife) should NOT be treated as a rebound candidate,
even if it triggers the oversold pullback signal. We measure net-of-cost forward
excess (vs each stock's own baseline) within the pullback_hv edge cohort, bucketed
by how chronically it has been below EMA20, with cluster-bootstrap 95% CI.
"""
import sys, math, statistics
sys.path.insert(0, "agent")
sys.path.insert(0, "agent/scripts")
import pandas as pd, numpy as np
from research_signal_framework_backtest import _collect_symbol_events, _ticker_frame, _cluster_bootstrap_ci
from market_data_service import download_daily_history
from scripts.screening_framework_v2_optimized import resolve_universe

UNIV, PERIOD, H, MAXSYM, COOLDOWN = "spx", "3y", 10, 180, 3
COST = 0.001  # ~10bps round-trip, net-of-cost discipline

tickers, source, _ = resolve_universe(UNIV)
tickers = tickers[:MAXSYM]
print(f"universe={UNIV} symbols={len(tickers)} source={source} period={PERIOD} horizon={H}d", flush=True)
panel, _ = download_daily_history(tickers, period=PERIOD)

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
    ema15 = close.ewm(span=15, adjust=False).mean()
    ema20 = close.ewm(span=20, adjust=False).mean()
    frac20 = (close < ema20).astype(float).rolling(63).mean()
    frac15 = (close < ema15).astype(float).rolling(63).mean()
    dist20 = close / ema20 - 1.0
    above15 = close >= ema15
    streak15 = (~above15).astype(int).groupby(above15.cumsum()).cumsum()
    bydate = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(close.index)}
    for ev in evs:
        i = bydate.get(ev["date"])
        if i is None or ev.get("pullback_hv_score") is None or ev.get("excess_return") is None:
            continue
        f20 = frac20.iloc[i]
        if not math.isfinite(f20):
            continue
        ev["frac20"] = float(f20)
        ev["frac15"] = float(frac15.iloc[i]) if math.isfinite(frac15.iloc[i]) else None
        ev["dist20"] = float(dist20.iloc[i]) if math.isfinite(dist20.iloc[i]) else None
        ev["streak15"] = int(streak15.iloc[i])
        allev.append(ev)

print(f"resolved pullback events with trend feature: {len(allev)}", flush=True)
scores = sorted(e["pullback_hv_score"] for e in allev)
q75 = scores[int(0.75 * len(scores))] if scores else 0.0
cohort = [e for e in allev if e["pullback_hv_score"] >= q75]
print(f"edge cohort = top-25% pullback_hv (score>= {q75:.3f}): n={len(cohort)}\n", flush=True)


def net(e):
    return e["excess_return"] - COST


def stat(evs):
    if not evs:
        return None
    ex = [net(e) for e in evs]
    tmp = [{"ticker": e["ticker"], "excess_return": net(e)} for e in evs]
    lo, hi = _cluster_bootstrap_ci(tmp)
    return dict(n=len(evs), clu=len({e["ticker"] for e in evs}), mean=statistics.mean(ex),
                lo=lo, hi=hi, hit=sum(1 for x in ex if x > 0) / len(ex))


def show(label, s):
    if not s:
        print(f"  {label}: (empty)")
        return
    sig = "  <-- CI excludes 0 (edge)" if s["lo"] > 0 else ("  (negative)" if s["hi"] < 0 else "  (CI crosses 0, no edge)")
    print(f"  {label}: n={s['n']:5d} clu={s['clu']:3d} netExcess={s['mean']*100:+.2f}% "
          f"CI[{s['lo']*100:+.2f},{s['hi']*100:+.2f}] hit={s['hit']:.0%}{sig}")


print("=== net excess by chronic-below-EMA20 (fraction of last 63 trading days) ===")
for lo, hi in [(0.0, 0.25), (0.25, 0.5), (0.5, 0.75), (0.75, 1.01)]:
    show(f"frac20 [{lo:.2f},{hi:.2f})", stat([e for e in cohort if lo <= e["frac20"] < hi]))

print("\n=== filter backtest: keep frac20<cutoff vs the excluded tail ===")
show("BASE (no filter)", stat(cohort))
for cut in [0.4, 0.5, 0.6, 0.7, 0.8, 0.9]:
    show(f"KEEP frac20<{cut:.2f}", stat([e for e in cohort if e["frac20"] < cut]))
    show(f"  EXCL frac20>={cut:.2f}", stat([e for e in cohort if e["frac20"] >= cut]))

print("\n=== cross-check: consecutive days below EMA15 (streak) ===")
for lo, hi in [(0, 10), (10, 21), (21, 42), (42, 9999)]:
    show(f"streak15 [{lo},{hi})", stat([e for e in cohort if lo <= e["streak15"] < hi]))

# ---- MCD diagnostic ----
print("\n=== MCD diagnostic (latest) ===")
try:
    md, _ = download_daily_history(["MCD", "SPY"], period="6mo")
    mf = _ticker_frame(md, "MCD"); sf = _ticker_frame(md, "SPY")
    mc = mf["Close"].astype(float); sc = sf["Close"].astype(float)
    e15 = mc.ewm(span=15, adjust=False).mean(); e20 = mc.ewm(span=20, adjust=False).mean()
    f20 = (mc < e20).astype(float).rolling(63).mean().iloc[-1]
    f15 = (mc < e15).astype(float).rolling(63).mean().iloc[-1]
    a15 = mc >= e15
    stk = (~a15).astype(int).groupby(a15.cumsum()).cumsum().iloc[-1]
    rs20 = (mc.pct_change(20).iloc[-1] - sc.pct_change(20).iloc[-1])
    print(f"  MCD close={mc.iloc[-1]:.2f} EMA15={e15.iloc[-1]:.2f} EMA20={e20.iloc[-1]:.2f}")
    print(f"  frac_below_EMA20_63d={f20:.2f}  frac_below_EMA15_63d={f15:.2f}  consec_below_EMA15={int(stk)}d  dist_to_EMA20={(mc.iloc[-1]/e20.iloc[-1]-1)*100:+.1f}%")
    print(f"  rel_strength_20d vs SPY = {rs20*100:+.1f}%")
except Exception as e:
    print("  MCD diag failed:", e)
