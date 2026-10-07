"""Backtest: does a DEEPER-oversold filter improve the validated pullback_hv edge?

Follows the lead from the fib backtest (where "not in the shallow 38.2-61.8%
retracement" -> deeper pullback -> significantly better). Here we test, inside
the pullback_hv top bucket, layering classic deep-oversold filters:
  - RSI(2) < 10        (Connors-style extreme oversold)
  - Z-score < -1.5     (price >1.5sigma below its 20d mean)
  - Bollinger %B < 0   (closed below the lower band = z < -2)
  - combo: RSI(2)<10 AND %B<0

Honesty: net-of-cost forward excess vs each stock's OWN baseline; cluster-
bootstrap CI by ticker; in-sample/current-membership caveats. A filter only
"works" if its CI excludes 0 AND it beats the unfiltered top bucket.
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
        syms, _s, _m = resolve_universe(name, archive_only=True)
    except Exception:
        syms = []
    return [str(s).upper() for s in syms][:max_symbols]


def _osc_series(close: pd.Series):
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(2).mean()
    loss = (-delta.clip(upper=0)).rolling(2).mean()
    rsi2 = 100.0 - 100.0 / (1.0 + gain / loss.replace(0.0, np.nan))
    ma = close.rolling(20).mean()
    sd = close.rolling(20).std()
    z = (close - ma) / sd.replace(0.0, np.nan)
    pctb = (z + 2.0) / 4.0  # %B when bands = ma +/- 2 sd
    return rsi2, z, pctb


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
    return {"group": label, "n": len(rows), "tickers": len({e["ticker"] for e in rows}),
            "mean_net_excess": round(mean(nets), 6), "ci": [lo, hi],
            "significant": bool(lo is not None and lo > 0),
            "hit": round(sum(1 for v in nets if v > 0) / len(nets), 4)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="spx")
    ap.add_argument("--max-symbols", type=int, default=150)
    ap.add_argument("--horizon", type=int, default=10)
    ap.add_argument("--period", default="2y")
    ap.add_argument("--top-quantile", type=float, default=0.70)
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
        if frame is None or frame.empty or "Close" not in frame or len(frame) < 60 + args.horizon:
            continue
        evs = _collect_symbol_events(sym, frame, args.horizon, min_launch_score=0.0, min_tunnel_score=0.0, cooldown_days=5)
        if not evs:
            continue
        close = pd.to_numeric(frame["Close"], errors="coerce").dropna()
        rsi2, z, pctb = _osc_series(close)
        pos = {ts.strftime("%Y-%m-%d"): k for k, ts in enumerate(close.index)}
        for e in evs:
            if e.get("pullback_hv_score") is None:
                continue
            i = pos.get(e["date"])
            if i is None or i < 21:
                continue
            e["rsi2"] = float(rsi2.iloc[i]) if pd.notna(rsi2.iloc[i]) else None
            e["z"] = float(z.iloc[i]) if pd.notna(z.iloc[i]) else None
            e["pctb"] = float(pctb.iloc[i]) if pd.notna(pctb.iloc[i]) else None
            e["net_excess"] = e["excess_return"] - rt_cost
            events.append(e)
        used += 1

    print(f"symbols_with_data={used} events={len(events)}", flush=True)
    if len(events) < 200:
        print("WARNING: thin sample -- illustrative only.", flush=True)

    scores = sorted(e["pullback_hv_score"] for e in events)
    cutoff = scores[int(args.top_quantile * (len(scores) - 1))] if scores else 0.0
    top = [e for e in events if e["pullback_hv_score"] >= cutoff]

    groups = [
        _summary("pullback_hv TOP (baseline)", top),
        _summary("  + RSI(2) < 10", [e for e in top if e["rsi2"] is not None and e["rsi2"] < 10]),
        _summary("  + RSI(2) < 5", [e for e in top if e["rsi2"] is not None and e["rsi2"] < 5]),
        _summary("  + Z-score < -1.5", [e for e in top if e["z"] is not None and e["z"] < -1.5]),
        _summary("  + Z-score < -2.0", [e for e in top if e["z"] is not None and e["z"] < -2.0]),
        _summary("  + Bollinger %B < 0 (below lower band)", [e for e in top if e["pctb"] is not None and e["pctb"] < 0]),
        _summary("  + RSI(2)<10 AND %B<0 (combo)", [e for e in top if e["rsi2"] is not None and e["rsi2"] < 10 and e["pctb"] is not None and e["pctb"] < 0]),
    ]
    base = groups[0]
    print("\n=== RESULTS (net-of-cost excess vs own baseline; CI = cluster bootstrap by ticker) ===", flush=True)
    for g in groups:
        if g["n"] == 0:
            print(f"  {g['group']:42} n=0", flush=True)
            continue
        ci = g["ci"]
        ci_s = f"[{ci[0]}, {ci[1]}]" if ci[0] is not None else "[n/a]"
        better = "" if g is base else (" BETTER" if g["mean_net_excess"] > base["mean_net_excess"] else " worse")
        sig = "SIG(>0)" if g["significant"] else "not-sig"
        print(f"  {g['group']:42} n={g['n']:<5} net={g['mean_net_excess']*100:+.3f}%  CI95={ci_s}  hit={g['hit']:.0%}  {sig}{better}", flush=True)

    print("\n=== VERDICT ===", flush=True)
    winners = [g for g in groups[1:] if g["n"] >= 50 and g["significant"] and g["mean_net_excess"] > base["mean_net_excess"]]
    if winners:
        for w in winners:
            print(f"  -> {w['group'].strip()}: net {w['mean_net_excess']*100:+.3f}% vs base {base['mean_net_excess']*100:+.3f}% (n={w['n']}), CI>0 -> WORTH ADDING as a filter.", flush=True)
    else:
        print("  -> no deep-oversold filter both beats the top bucket AND keeps CI>0 (or sample too thin). Do NOT add.", flush=True)
    print("\nCaveats: in-sample, single period; archive_only universe (possible survivorship bias); cache-limited history.", flush=True)


if __name__ == "__main__":
    main()
