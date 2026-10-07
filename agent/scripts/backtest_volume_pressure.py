"""Backtest: does daily VOLUME/MONEY-FLOW pressure improve the pullback_hv edge?

The user's trader folk-wisdom ("price drifts toward the side of the book with the
bigger resting-order spikes") is an order-book/L2 imbalance idea. The actual L2
depth data is NOT available on Massive/Polygon Starter (NBBO + trades + depth all
403/404). So we test the only thing the daily aggregates let us proxy: where the
*executed* buy/sell pressure has been concentrating, at the day scale.

Inside the validated pullback_hv top bucket we layer classic accumulation/pressure
filters computed from OHLCV only:
  - UDVR    : up-day vol / down-day vol over trailing 10d (buying-pressure ratio)
  - OBV     : 20d OBV slope > 0 (net accumulation trend)
  - CMF     : Chaikin Money Flow 20d (close position in range * volume)
  - VSURGE  : entry-day volume / 20d avg (放量 capitulation flush)
  - combos  : pressure + surge

Honesty: net-of-cost forward excess vs each stock's OWN baseline; cluster-bootstrap
CI by ticker; in-sample / current-membership caveats. A filter only "works" if its
CI excludes 0 AND it beats the unfiltered top bucket. Same bar as deep-oversold.
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


def _vol_features(frame: pd.DataFrame):
    """Return aligned series: udvr10, obv_slope20, cmf20, vsurge."""
    close = pd.to_numeric(frame["Close"], errors="coerce")
    high = pd.to_numeric(frame.get("High"), errors="coerce")
    low = pd.to_numeric(frame.get("Low"), errors="coerce")
    vol = pd.to_numeric(frame.get("Volume"), errors="coerce")

    ret = close.diff()
    up_vol = vol.where(ret > 0, 0.0)
    dn_vol = vol.where(ret < 0, 0.0)
    udvr = up_vol.rolling(10).sum() / dn_vol.rolling(10).sum().replace(0.0, np.nan)

    # OBV then 20d slope (normalized by 20d avg volume so it's comparable across names)
    sign = np.sign(ret).fillna(0.0)
    obv = (sign * vol).fillna(0.0).cumsum()
    avgvol = vol.rolling(20).mean()
    obv_slope = (obv - obv.shift(20)) / (20.0 * avgvol.replace(0.0, np.nan))

    # Chaikin Money Flow 20d
    rng = (high - low).replace(0.0, np.nan)
    mfm = ((close - low) - (high - close)) / rng
    mfv = mfm * vol
    cmf = mfv.rolling(20).sum() / vol.rolling(20).sum().replace(0.0, np.nan)

    vsurge = vol / avgvol.replace(0.0, np.nan)
    return udvr, obv_slope, cmf, vsurge


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
        if frame is None or frame.empty or "Close" not in frame or "Volume" not in frame or len(frame) < 60 + args.horizon:
            continue
        evs = _collect_symbol_events(sym, frame, args.horizon, min_launch_score=0.0, min_tunnel_score=0.0, cooldown_days=5)
        if not evs:
            continue
        udvr, obv_slope, cmf, vsurge = _vol_features(frame)
        pos = {ts.strftime("%Y-%m-%d"): k for k, ts in enumerate(frame.index)}
        for e in evs:
            if e.get("pullback_hv_score") is None:
                continue
            i = pos.get(e["date"])
            if i is None or i < 21:
                continue
            def _g(s):
                v = s.iloc[i]
                return float(v) if pd.notna(v) else None
            e["udvr"] = _g(udvr)
            e["obv_slope"] = _g(obv_slope)
            e["cmf"] = _g(cmf)
            e["vsurge"] = _g(vsurge)
            e["net_excess"] = e["excess_return"] - rt_cost
            events.append(e)
        used += 1

    print(f"symbols_with_data={used} events={len(events)}", flush=True)
    if len(events) < 200:
        print("WARNING: thin sample -- illustrative only.", flush=True)

    scores = sorted(e["pullback_hv_score"] for e in events)
    cutoff = scores[int(args.top_quantile * (len(scores) - 1))] if scores else 0.0
    top = [e for e in events if e["pullback_hv_score"] >= cutoff]

    def F(key, pred):
        return [e for e in top if e.get(key) is not None and pred(e[key])]

    groups = [
        _summary("pullback_hv TOP (baseline)", top),
        _summary("  + UDVR > 1.2 (buy-pressure)", F("udvr", lambda v: v > 1.2)),
        _summary("  + UDVR > 1.5 (strong buy)", F("udvr", lambda v: v > 1.5)),
        _summary("  + UDVR < 0.8 (sell-pressure)", F("udvr", lambda v: v < 0.8)),
        _summary("  + OBV slope > 0 (accum)", F("obv_slope", lambda v: v > 0)),
        _summary("  + CMF > 0 (money in)", F("cmf", lambda v: v > 0)),
        _summary("  + CMF > 0.05 (strong in)", F("cmf", lambda v: v > 0.05)),
        _summary("  + CMF < -0.05 (money out)", F("cmf", lambda v: v < -0.05)),
        _summary("  + VSURGE > 1.5 (放量 flush)", F("vsurge", lambda v: v > 1.5)),
        _summary("  + UDVR>1.2 AND VSURGE>1.5", [e for e in top if (e.get("udvr") or 0) > 1.2 and (e.get("vsurge") or 0) > 1.5]),
        _summary("  + CMF>0 AND VSURGE>1.5", [e for e in top if (e.get("cmf") is not None and e["cmf"] > 0) and (e.get("vsurge") or 0) > 1.5]),
    ]
    base = groups[0]
    print("\n=== RESULTS (net-of-cost excess vs own baseline; CI = cluster bootstrap by ticker) ===", flush=True)
    for g in groups:
        if g["n"] == 0:
            print(f"  {g['group']:38} n=0", flush=True)
            continue
        ci = g["ci"]
        ci_s = f"[{ci[0]}, {ci[1]}]" if ci[0] is not None else "[n/a]"
        better = "" if g is base else (" BETTER" if g["mean_net_excess"] > base["mean_net_excess"] else " worse")
        sig = "SIG(>0)" if g["significant"] else "not-sig"
        print(f"  {g['group']:38} n={g['n']:<5} net={g['mean_net_excess']*100:+.3f}%  CI95={ci_s}  hit={g['hit']:.0%}  {sig}{better}", flush=True)

    print("\n=== VERDICT ===", flush=True)
    winners = [g for g in groups[1:] if g["n"] >= 50 and g["significant"] and g["mean_net_excess"] > base["mean_net_excess"]]
    if winners:
        for w in winners:
            print(f"  -> {w['group'].strip()}: net {w['mean_net_excess']*100:+.3f}% vs base {base['mean_net_excess']*100:+.3f}% (n={w['n']}), CI>0 -> WORTH ADDING.", flush=True)
    else:
        print("  -> no volume-pressure filter both beats the top bucket AND keeps CI>0 (or sample too thin). Do NOT add.", flush=True)
    print("\nCaveats: in-sample, single period; archive_only universe (possible survivorship bias);", flush=True)
    print("daily executed-volume proxy, NOT the L2 order-book imbalance the folk-wisdom refers to.", flush=True)


if __name__ == "__main__":
    main()
