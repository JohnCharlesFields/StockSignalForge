"""Backtest the user's hypothesis: do D. Trump-mentioned stocks that were OVERSOLD
at the mention subsequently outperform (vs oversold names with NO Trump mention)?

Honest framing: this tests whether a Trump mention ADDS to the already-validated
deep-oversold edge. Data: Polygon news (history to 2016) for Trump mentions per
ticker; price via get_daily_history. Trade convention = the user's real mechanics
(exit_model: buy close[T], sell open[T+H]); net of cost; excess vs each stock's
own baseline; cluster-bootstrap CI by ticker.

Groups (pullback universe, deep-oversold = z<-1.5 OR rsi2<10):
  A  Trump-mention & oversold        <- the hypothesis
  B  Trump-mention & NOT oversold
  C  oversold & NO Trump (control)   <- the existing edge
A "works as an add-on" only if A beats C with the A-minus-C difference CI > 0.
Caveats: Trump tagging is often incidental/macro; n for A is likely small.
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time
from statistics import mean

import numpy as np
import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
for _p in (AGENT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import cost_model  # noqa: E402
import exit_model  # noqa: E402
from market_data_service import get_daily_history  # noqa: E402

_BASE = "https://api.polygon.io"


def _universe(name, n):
    from screening_framework_v2_optimized import resolve_universe
    try:
        syms, _s, _m = resolve_universe(name, archive_only=True)
    except Exception:
        syms = []
    return [str(s).upper() for s in syms][:n]


def _trump_dates(sym, start_iso, key, max_pages=5):
    """Return {date_str: sentiment} for Trump-mention articles tagging `sym`."""
    out = {}
    url = f"{_BASE}/v2/reference/news"
    params = {"ticker": sym, "published_utc.gte": start_iso, "order": "desc",
              "sort": "published_utc", "limit": 1000, "apiKey": key}
    pages = 0
    while url and pages < max_pages:
        try:
            r = requests.get(url, params=params if pages == 0 else {"apiKey": key}, timeout=25)
            if r.status_code != 200:
                break
            j = r.json()
        except Exception:
            break
        for a in j.get("results") or []:
            blob = (str(a.get("title") or "") + " " + str(a.get("description") or "") + " "
                    + " ".join(map(str, a.get("keywords") or []))).lower()
            if "trump" not in blob:
                continue
            day = str(a.get("published_utc") or "")[:10]
            if not day:
                continue
            sent = "neutral"
            for ins in (a.get("insights") or []):
                if str(ins.get("ticker") or "").upper() == sym:
                    sent = ins.get("sentiment") or "neutral"
                    break
            out.setdefault(day, sent)
        url = j.get("next_url")
        pages += 1
        time.sleep(0.05)
    return out


def _osc(close):
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(2).mean()
    loss = (-delta.clip(upper=0)).rolling(2).mean()
    rsi2 = 100.0 - 100.0 / (1.0 + gain / loss.replace(0.0, np.nan))
    ma = close.rolling(20).mean()
    sd = close.rolling(20).std()
    z = (close - ma) / sd.replace(0.0, np.nan)
    return rsi2, z


def _cluster_ci(rows, n_boot=3000, seed=42):
    by = {}
    for e in rows:
        by.setdefault(e["ticker"], []).append(float(e["net"]))
    cl = [k for k in by if by[k]]
    if len(cl) < 2:
        return (None, None)
    rng = random.Random(seed)
    ests = []
    for _ in range(n_boot):
        s = [rng.choice(cl) for _ in cl]
        v = [x for c in s for x in by[c]]
        if v:
            ests.append(sum(v) / len(v))
    ests.sort()
    return (round(ests[int(0.025 * (len(ests) - 1))], 6), round(ests[int(0.975 * (len(ests) - 1))], 6))


def _diff_ci(a, b, n_boot=3000, seed=7):
    ba, bb = {}, {}
    for e in a:
        ba.setdefault(e["ticker"], []).append(float(e["net"]))
    for e in b:
        bb.setdefault(e["ticker"], []).append(float(e["net"]))
    cl = sorted(set(ba) | set(bb))
    if len(cl) < 2:
        return (None, None, None)
    rng = random.Random(seed)
    ests = []
    for _ in range(n_boot):
        s = [rng.choice(cl) for _ in cl]
        xa = [x for c in s for x in ba.get(c, [])]
        xb = [x for c in s for x in bb.get(c, [])]
        if xa and xb:
            ests.append(sum(xa) / len(xa) - sum(xb) / len(xb))
    if not ests:
        return (None, None, None)
    ests.sort()
    return (round(mean(ests), 6), round(ests[int(0.025 * (len(ests) - 1))], 6), round(ests[int(0.975 * (len(ests) - 1))], 6))


def _summ(label, rows):
    if not rows:
        print(f"  {label:34} n=0", flush=True)
        return
    nets = [e["net"] for e in rows]
    lo, hi = _cluster_ci(rows)
    sig = "SIG(>0)" if (lo is not None and lo > 0) else "  -   "
    ci = f"[{lo}, {hi}]" if lo is not None else "[n/a]"
    print(f"  {label:34} n={len(rows):<5} tk={len({e['ticker'] for e in rows}):<3} net={mean(nets)*100:+.3f}%  CI95={ci}  hit={sum(1 for v in nets if v>0)/len(nets):.0%}  {sig}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="spx")
    ap.add_argument("--max-symbols", type=int, default=150)
    ap.add_argument("--hold", type=int, default=8)
    ap.add_argument("--period", default="3y")
    ap.add_argument("--years", type=float, default=3.0)
    args = ap.parse_args()

    key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if not key:
        print("no MASSIVE_API_KEY", flush=True)
        return
    rt = cost_model.equity_round_trip_cost()
    H = args.hold
    syms = _universe(args.universe, args.max_symbols)
    start_iso = (pd.Timestamp.utcnow() - pd.Timedelta(days=int(args.years * 365))).strftime("%Y-%m-%d")
    print(f"universe={args.universe} symbols={len(syms)} hold=open[T+{H}] cost(rt)={rt:.4f} news_since={start_iso}", flush=True)

    A, B, C = [], [], []  # trump&oversold, trump&not, oversold&no-trump
    trump_events = 0
    for si, sym in enumerate(syms):
        try:
            frame, _s = get_daily_history(sym, period=args.period)
        except Exception:
            continue
        if frame is None or frame.empty or "Close" not in frame or "Open" not in frame or len(frame) < 60 + H:
            continue
        close = pd.to_numeric(frame["Close"], errors="coerce").reset_index(drop=True)
        open_ = pd.to_numeric(frame["Open"], errors="coerce").reset_index(drop=True)
        rsi2, z = _osc(close)
        n = len(close)
        baseline = exit_model.baseline_forward_returns(close, open_, H)
        base_mean = mean(baseline) if baseline else 0.0
        dates = [ts.strftime("%Y-%m-%d") for ts in frame.index]
        pos = {d: k for k, d in enumerate(dates)}

        def net_at(i):
            r = exit_model.forward_return(close, open_, i, H)
            return None if r is None else (r - base_mean - rt)

        def is_dos(i):
            zz, rr = z.iloc[i], rsi2.iloc[i]
            return (pd.notna(zz) and zz < -1.5) or (pd.notna(rr) and rr < 10)

        def entry_idx(day):
            # first session on/after the article day
            for k in range(len(dates)):
                if dates[k] >= day:
                    return k
            return None

        tdates = _trump_dates(sym, start_iso, key)
        trump_idx = set()
        last = -99
        for day in sorted(tdates):
            i = entry_idx(day)
            if i is None or i < 21 or i >= n - H:
                continue
            if i - last < 5:  # cooldown
                continue
            last = i
            trump_idx.add(i)
            net = net_at(i)
            if net is None:
                continue
            trump_events += 1
            row = {"ticker": sym, "net": net}
            (A if is_dos(i) else B).append(row)
        # control: oversold days with no trump mention nearby
        lastc = -99
        for i in range(21, n - H):
            if i in trump_idx:
                continue
            if not is_dos(i):
                continue
            if i - lastc < 5:
                continue
            lastc = i
            net = net_at(i)
            if net is not None:
                C.append({"ticker": sym, "net": net})
        if (si + 1) % 30 == 0:
            print(f"  ...{si+1}/{len(syms)} symbols, trump_events so far={trump_events}", flush=True)

    print(f"\ntrump_events_total={trump_events}  A(trump&oversold)={len(A)}  B(trump&not)={len(B)}  C(oversold,no-trump)={len(C)}", flush=True)
    print("\n=== RESULTS (net-of-cost excess vs own baseline; entry close[T] -> exit open[T+H]) ===", flush=True)
    _summ("A: Trump & oversold", A)
    _summ("B: Trump & NOT oversold", B)
    _summ("C: oversold, NO Trump (control)", C)

    print("\n=== HYPOTHESIS TEST: does Trump ADD to the oversold edge? ===", flush=True)
    d, lo, hi = _diff_ci(A, C)
    if d is None:
        print("  insufficient sample for A vs C contrast.", flush=True)
    else:
        verdict = "Trump ADDS edge (CI>0)" if lo > 0 else "no added edge (CI incl 0)"
        print(f"  [A] - [C]  diff={d*100:+.3f}%  CI95=[{lo}, {hi}]  -> {verdict}", flush=True)
    print("\n=== VERDICT ===", flush=True)
    if len(A) < 30:
        print(f"  -> A sample too thin (n={len(A)}). Cannot conclude; Trump+oversold is rare. NOT actionable.", flush=True)
    elif lo is not None and lo > 0:
        print("  -> Trump mention on an oversold name shows a significant ADD over plain oversold. Worth deeper study.", flush=True)
    else:
        print("  -> No evidence Trump mention adds to the oversold edge (the IBM coincidence was n=1).", flush=True)
    print("\nCaveats: Trump ticker-tagging is often incidental/macro; in-sample; archive_only universe (survivorship); news history per-ticker capped at 5 pages.", flush=True)


if __name__ == "__main__":
    main()
