"""Re-test the validated edge under the user's ACTUAL mechanics.

User trades: BUY at close[T] (盘末买入), SELL at open[T+k] (盘初卖出) for some k in
1..10 trading days -- a flexible-exit short-term swing, NOT a fixed close-to-close
10-day hold (which is what _collect_symbol_events / _forward_return measure).

Picking the best k ex-post is lookahead/cheating. The honest evaluation is to sweep
k = 1..10 and report the edge at EACH hold length, so the user can pick a hold rule.

  entry      = close[T]
  exit_k     = open[T+k]
  ret_k      = open[T+k]/close[T] - 1
  baseline_k = mean over all days t of (open[t+k]/close[t] - 1)   (same convention)
  excess_k   = ret_k - baseline_k
  net_k      = excess_k - round_trip_cost

Groups: pullback_hv top bucket, and the shipped deep-oversold (z<-1.5 OR rsi2<10)
subset within it. Cluster-bootstrap CI by ticker; in-sample / survivorship caveats.
This tells us whether the shipped edge survives the close->open exit convention and
at which hold length it peaks.
"""
from __future__ import annotations

import argparse
import os
import random
import sys
from statistics import mean

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
for _p in (AGENT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import cost_model  # noqa: E402
from market_data_service import get_daily_history  # noqa: E402
from research_signal_framework_backtest import _collect_symbol_events  # noqa: E402


def _universe(name, max_symbols):
    from screening_framework_v2_optimized import resolve_universe
    try:
        syms, _s, _m = resolve_universe(name, archive_only=True)
    except Exception:
        syms = []
    return [str(s).upper() for s in syms][:max_symbols]


def _osc(close):
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(2).mean()
    loss = (-delta.clip(upper=0)).rolling(2).mean()
    rsi2 = 100.0 - 100.0 / (1.0 + gain / loss.replace(0.0, np.nan))
    ma = close.rolling(20).mean()
    sd = close.rolling(20).std()
    z = (close - ma) / sd.replace(0.0, np.nan)
    return rsi2, z


def _ci(rows, key, n_boot=2500, seed=42):
    by = {}
    for e in rows:
        v = e.get(key)
        if v is not None:
            by.setdefault(e["ticker"], []).append(float(v))
    cl = [k for k in by if by[k]]
    if len(cl) < 2:
        return (None, None, None, 0)
    rng = random.Random(seed)
    ests = []
    for _ in range(n_boot):
        s = [rng.choice(cl) for _ in cl]
        vals = [x for c in s for x in by[c]]
        if vals:
            ests.append(sum(vals) / len(vals))
    ests.sort()
    allvals = [x for c in cl for x in by[c]]
    return (round(mean(allvals), 6),
            round(ests[int(0.025 * (len(ests) - 1))], 6),
            round(ests[int(0.975 * (len(ests) - 1))], 6),
            len(allvals))


def _hit(rows, key):
    vals = [e[key] for e in rows if e.get(key) is not None]
    return round(sum(1 for v in vals if v > 0) / len(vals), 4) if vals else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="spx")
    ap.add_argument("--max-symbols", type=int, default=150)
    ap.add_argument("--period", default="2y")
    ap.add_argument("--max-k", type=int, default=10)
    ap.add_argument("--collect-horizon", type=int, default=10, help="horizon used only to enumerate signal events")
    ap.add_argument("--top-quantile", type=float, default=0.70)
    args = ap.parse_args()

    rt = cost_model.equity_round_trip_cost()
    syms = _universe(args.universe, args.max_symbols)
    print(f"universe={args.universe} symbols={len(syms)} max_k={args.max_k} cost(rt)={rt:.4f}", flush=True)
    print("mechanics: ENTRY=close[T], EXIT=open[T+k]  (盘末买 -> 盘初卖)", flush=True)

    events = []
    used = 0
    for sym in syms:
        try:
            frame, _s = get_daily_history(sym, period=args.period)
        except Exception:
            continue
        if frame is None or frame.empty or "Close" not in frame or "Open" not in frame or len(frame) < 60 + args.max_k:
            continue
        evs = _collect_symbol_events(sym, frame, args.collect_horizon, min_launch_score=0.0, min_tunnel_score=0.0, cooldown_days=5)
        if not evs:
            continue
        close = pd.to_numeric(frame["Close"], errors="coerce").reset_index(drop=True)
        openp = pd.to_numeric(frame["Open"], errors="coerce").reset_index(drop=True)
        rsi2, z = _osc(close)
        n = len(close)
        # baseline_k: mean over all days of open[t+k]/close[t]-1
        baseline = {}
        for k in range(1, args.max_k + 1):
            vals = []
            for t in range(0, n - k):
                c0 = close.iloc[t]
                ok = openp.iloc[t + k]
                if pd.notna(c0) and c0 > 0 and pd.notna(ok):
                    vals.append(ok / c0 - 1.0)
            baseline[k] = mean(vals) if vals else None
        pos = {ts.strftime("%Y-%m-%d"): kk for kk, ts in enumerate(frame.index)}
        for e in evs:
            if e.get("pullback_hv_score") is None:
                continue
            i = pos.get(e["date"])
            if i is None or i < 21 or i >= n - args.max_k:
                continue
            c0 = close.iloc[i]
            if pd.isna(c0) or c0 <= 0:
                continue
            e["rsi2"] = float(rsi2.iloc[i]) if pd.notna(rsi2.iloc[i]) else None
            e["z"] = float(z.iloc[i]) if pd.notna(z.iloc[i]) else None
            for k in range(1, args.max_k + 1):
                ok = openp.iloc[i + k]
                if pd.isna(ok) or baseline.get(k) is None:
                    continue
                ret = ok / c0 - 1.0
                e[f"net_{k}"] = (ret - baseline[k]) - rt
            events.append(e)
        used += 1

    scores = sorted(e["pullback_hv_score"] for e in events)
    cut = scores[int(args.top_quantile * (len(scores) - 1))] if scores else 0.0
    top = [e for e in events if e["pullback_hv_score"] >= cut]
    dos = [e for e in top if (e.get("z") is not None and e["z"] < -1.5) or (e.get("rsi2") is not None and e["rsi2"] < 10)]
    print(f"symbols_with_data={used} events={len(events)} top={len(top)} dos={len(dos)}", flush=True)

    def curve(label, rows):
        print(f"\n=== {label} (net-of-cost excess vs own baseline; entry close[T] -> exit open[T+k]) ===", flush=True)
        print("   k(hold)   n      net_excess   CI95                  hit    sig", flush=True)
        best = None
        for k in range(1, args.max_k + 1):
            key = f"net_{k}"
            m, lo, hi, nn = _ci(rows, key)
            if m is None:
                print(f"   T+{k:<5} n/a", flush=True)
                continue
            sig = "SIG(>0)" if lo > 0 else "  -   "
            h = _hit(rows, key)
            print(f"   T+{k:<5} {nn:<6} {m*100:+.3f}%     [{lo:+.5f}, {hi:+.5f}]   {h:.0%}   {sig}", flush=True)
            if lo > 0 and (best is None or m > best[1]):
                best = (k, m, lo, hi, nn, h)
        if best:
            print(f"   -> best significant hold: T+{best[0]} morning, net {best[1]*100:+.3f}% (CI>0, n={best[4]}, hit {best[5]:.0%})", flush=True)
        else:
            print("   -> NO hold length has CI>0 under close->open mechanics.", flush=True)
        return best

    b_top = curve("pullback_hv TOP bucket", top)
    b_dos = curve("deep-oversold subset (shipped filter)", dos)

    print("\n=== COMPARISON NOTE ===", flush=True)
    print("Old backtests used exit=close[T+10] (fixed). This uses exit=open[T+k] swept 1..10 (your mechanics).", flush=True)
    print("Caveats: in-sample, single period, archive_only (survivorship). 'best k' shown is descriptive,", flush=True)
    print("not a tradable rule (picking k ex-post is lookahead); use it to choose a FIXED default hold.", flush=True)


if __name__ == "__main__":
    main()
