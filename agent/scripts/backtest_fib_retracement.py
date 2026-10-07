"""Backtest: does a Fibonacci-retracement filter improve the validated
pullback_hv edge?

Same honesty rules as the rest of the system:
  - "win" = net-of-cost forward excess vs each stock's OWN unconditional baseline.
  - cluster-bootstrap CI (cluster by ticker) -- an edge only counts if CI excludes 0.
  - in-sample / current-membership caveats stated in the report.

We reuse research_signal_framework_backtest._collect_symbol_events (the exact
pullback_hv replay) with NO score floor, then tag each event with whether the
price sat in the 38.2-61.8% Fibonacci retracement of the trailing swing, and
compare the top pullback_hv bucket WITH vs WITHOUT the fib filter.
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


def _universe(name: str, max_symbols: int) -> list[str]:
    from screening_framework_v2_optimized import resolve_universe

    try:
        syms, _src, _m = resolve_universe(name, archive_only=True)
    except Exception:
        syms = []
    return [str(s).upper() for s in syms][:max_symbols]


def _fib_flag(high: pd.Series, low: pd.Series, close: pd.Series, i: int, window: int) -> dict:
    """Was bar i inside the 38.2-61.8% retracement of the trailing `window` swing?
    Only defined for an up-leg (high after low). Returns flag + retracement depth."""
    if i < window:
        return {"ok": False}
    wh = high.iloc[i - window:i + 1]
    wl = low.iloc[i - window:i + 1]
    hi, lo = float(wh.max()), float(wl.min())
    if hi <= lo:
        return {"ok": False}
    rng = hi - lo
    trend_up = wh.idxmax() >= wl.idxmin()
    c = float(close.iloc[i])
    if not trend_up:
        return {"ok": True, "in_fib": False, "trend_up": False, "retr": None}
    buy_lo = hi - rng * 0.618
    buy_hi = hi - rng * 0.382
    return {"ok": True, "in_fib": bool(buy_lo <= c <= buy_hi), "trend_up": True,
            "retr": (hi - c) / rng}


def _cluster_ci(rows: list[dict], n_boot: int = 2000, seed: int = 42) -> tuple:
    by: dict[str, list[float]] = {}
    for e in rows:
        by.setdefault(e["ticker"], []).append(float(e["net_excess"]))
    clusters = [k for k in by if by[k]]
    if len(clusters) < 2:
        return (None, None)
    rng = random.Random(seed)
    ests = []
    for _ in range(n_boot):
        samp = [rng.choice(clusters) for _ in clusters]
        vals = [v for c in samp for v in by[c]]
        if vals:
            ests.append(sum(vals) / len(vals))
    ests.sort()
    return (round(ests[int(0.025 * (len(ests) - 1))], 6), round(ests[int(0.975 * (len(ests) - 1))], 6))


def _summary(label: str, rows: list[dict]) -> dict:
    if not rows:
        return {"group": label, "n": 0}
    nets = [e["net_excess"] for e in rows]
    lo, hi = _cluster_ci(rows)
    return {
        "group": label,
        "n": len(rows),
        "tickers": len({e["ticker"] for e in rows}),
        "mean_net_excess": round(mean(nets), 6),
        "ci": [lo, hi],
        "significant": bool(lo is not None and lo > 0),
        "hit_rate": round(sum(1 for v in nets if v > 0) / len(nets), 4),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="spx")
    ap.add_argument("--max-symbols", type=int, default=150)
    ap.add_argument("--horizon", type=int, default=10)
    ap.add_argument("--period", default="2y")
    ap.add_argument("--window", type=int, default=120)
    ap.add_argument("--top-quantile", type=float, default=0.70, help="pullback_hv top bucket cutoff")
    args = ap.parse_args()

    rt_cost = cost_model.equity_round_trip_cost()
    symbols = _universe(args.universe, args.max_symbols)
    print(f"universe={args.universe} symbols={len(symbols)} horizon={args.horizon} cost(rt)={rt_cost:.4f}", flush=True)

    events: list[dict] = []
    used = 0
    for sym in symbols:
        try:
            frame, _src = get_daily_history(sym, period=args.period)
        except Exception:
            continue
        if frame is None or frame.empty or "Close" not in frame or len(frame) < args.window + args.horizon + 30:
            continue
        evs = _collect_symbol_events(sym, frame, args.horizon, min_launch_score=0.0,
                                     min_tunnel_score=0.0, cooldown_days=5)
        if not evs:
            continue
        high = pd.to_numeric(frame["High"], errors="coerce")
        low = pd.to_numeric(frame["Low"], errors="coerce")
        close = pd.to_numeric(frame["Close"], errors="coerce")
        idx_map = {ts.strftime("%Y-%m-%d"): pos for pos, ts in enumerate(frame.index)}
        for e in evs:
            if e.get("pullback_hv_score") is None:
                continue
            i = idx_map.get(e["date"])
            if i is None:
                continue
            fib = _fib_flag(high, low, close, i, args.window)
            if not fib.get("ok"):
                continue
            e["in_fib"] = fib["in_fib"]
            e["trend_up"] = fib["trend_up"]
            e["net_excess"] = e["excess_return"] - rt_cost
            events.append(e)
        used += 1

    print(f"symbols_with_data={used} events={len(events)}", flush=True)
    if len(events) < 200:
        print("WARNING: too few events (cache history likely thin) -- result is illustrative only.", flush=True)

    # Top pullback_hv bucket (the validated edge lives here).
    scores = sorted(e["pullback_hv_score"] for e in events)
    cutoff = scores[int(args.top_quantile * (len(scores) - 1))] if scores else 0.0
    top = [e for e in events if e["pullback_hv_score"] >= cutoff]
    top_fib = [e for e in top if e["in_fib"]]
    top_nofib = [e for e in top if not e["in_fib"]]
    all_fib = [e for e in events if e["in_fib"]]
    all_nofib = [e for e in events if not e["in_fib"]]

    groups = [
        _summary("ALL events (baseline)", events),
        _summary(f"pullback_hv TOP {int((1-args.top_quantile)*100)}% (validated edge)", top),
        _summary("  TOP & in fib 38.2-61.8% zone", top_fib),
        _summary("  TOP & NOT in fib zone", top_nofib),
        _summary("ALL & in fib zone (fib standalone)", all_fib),
        _summary("ALL & NOT in fib zone", all_nofib),
    ]
    print("\n=== RESULTS (net-of-cost excess vs own baseline; CI = cluster bootstrap by ticker) ===", flush=True)
    for g in groups:
        if g["n"] == 0:
            print(f"  {g['group']:48} n=0", flush=True)
            continue
        ci = g["ci"]
        ci_s = f"[{ci[0]}, {ci[1]}]" if ci[0] is not None else "[n/a]"
        sig = "SIGNIFICANT(>0)" if g["significant"] else "not sig"
        print(f"  {g['group']:48} n={g['n']:<5} net={g['mean_net_excess']*100:+.3f}%  CI95={ci_s}  hit={g['hit_rate']:.0%}  {sig}", flush=True)

    # Verdict on the filter
    t = next((g for g in groups if g["group"].startswith("pullback_hv TOP")), None)
    tf = next((g for g in groups if "in fib" in g["group"] and g["group"].startswith("  TOP")), None)
    print("\n=== VERDICT ===", flush=True)
    if t and tf and t["n"] and tf["n"]:
        delta = tf["mean_net_excess"] - t["mean_net_excess"]
        print(f"  TOP fib-filtered net {tf['mean_net_excess']*100:+.3f}% vs TOP all {t['mean_net_excess']*100:+.3f}% "
              f"(Δ={delta*100:+.3f}%); fib-filtered significant={tf['significant']}", flush=True)
        if tf["significant"] and delta > 0:
            print("  -> fib filter IMPROVES the top bucket (and stays CI>0). Worth considering as a filter.", flush=True)
        elif tf["significant"]:
            print("  -> fib-filtered still CI>0 but NOT higher than unfiltered -> no added value, just fewer trades.", flush=True)
        else:
            print("  -> fib-filtered CI crosses 0 (or thin sample) -> does NOT hold up. Keep fib as display-only.", flush=True)
    else:
        print("  insufficient sample to judge -- keep fib as display-only.", flush=True)
    print("\nCaveats: in-sample, single period; universe via archive_only snapshot (may be survivorship-biased);"
          " cache-limited history. Not a clean OOS test.", flush=True)


if __name__ == "__main__":
    main()
