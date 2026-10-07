"""Orthogonality check: does VOLUME SURGE add edge ON TOP of deep-oversold?

deep-oversold (DOS) is already shipped as a filter. The volume-pressure backtest
found VSURGE>1.5 lifts the pullback_hv edge, but VSURGE and DOS both capture
"capitulation" and likely overlap. The real question for shipping VSURGE is
INCREMENTAL: inside the DOS-hit subset, do the surge events beat the no-surge
events -- and does the *difference* itself survive a cluster bootstrap CI>0?

DOS-hit matches the shipped _deep_oversold: z < -1.5 OR rsi2 < 10 (level in
{deep, oversold}). Same net-of-cost excess-vs-own-baseline, cluster-bootstrap
by ticker, in-sample caveats as the other backtests.
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


def _osc(close: pd.Series):
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(2).mean()
    loss = (-delta.clip(upper=0)).rolling(2).mean()
    rsi2 = 100.0 - 100.0 / (1.0 + gain / loss.replace(0.0, np.nan))
    ma = close.rolling(20).mean()
    sd = close.rolling(20).std()
    z = (close - ma) / sd.replace(0.0, np.nan)
    return rsi2, z


def _vsurge(frame: pd.DataFrame):
    vol = pd.to_numeric(frame.get("Volume"), errors="coerce")
    return vol / vol.rolling(20).mean().replace(0.0, np.nan)


def _by_ticker(rows: list[dict]) -> dict:
    by: dict[str, list[float]] = {}
    for e in rows:
        by.setdefault(e["ticker"], []).append(float(e["net_excess"]))
    return by


def _cluster_ci(rows: list[dict], n_boot: int = 3000, seed: int = 42) -> tuple:
    by = _by_ticker(rows)
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


def _diff_ci(rows_a: list[dict], rows_b: list[dict], n_boot: int = 3000, seed: int = 7) -> tuple:
    """Cluster-bootstrap CI of mean(A) - mean(B), resampling tickers jointly."""
    by_a = _by_ticker(rows_a)
    by_b = _by_ticker(rows_b)
    clusters = sorted(set(by_a) | set(by_b))
    if len(clusters) < 2:
        return (None, None, None)
    rng = random.Random(seed)
    ests = []
    for _ in range(n_boot):
        samp = [rng.choice(clusters) for _ in clusters]
        a = [v for c in samp for v in by_a.get(c, [])]
        b = [v for c in samp for v in by_b.get(c, [])]
        if a and b:
            ests.append(sum(a) / len(a) - sum(b) / len(b))
    if not ests:
        return (None, None, None)
    ests.sort()
    lo = ests[int(0.025 * (len(ests) - 1))]
    hi = ests[int(0.975 * (len(ests) - 1))]
    return (round(mean(ests), 6), round(lo, 6), round(hi, 6))


def _summary(label: str, rows: list[dict]) -> dict:
    if not rows:
        return {"group": label, "n": 0, "rows": rows}
    nets = [e["net_excess"] for e in rows]
    lo, hi = _cluster_ci(rows)
    return {"group": label, "n": len(rows), "tickers": len({e["ticker"] for e in rows}),
            "mean_net_excess": round(mean(nets), 6), "ci": [lo, hi],
            "significant": bool(lo is not None and lo > 0),
            "hit": round(sum(1 for v in nets if v > 0) / len(nets), 4), "rows": rows}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="spx")
    ap.add_argument("--max-symbols", type=int, default=150)
    ap.add_argument("--horizon", type=int, default=10)
    ap.add_argument("--period", default="2y")
    ap.add_argument("--top-quantile", type=float, default=0.70)
    ap.add_argument("--surge", type=float, default=1.5)
    args = ap.parse_args()

    rt_cost = cost_model.equity_round_trip_cost()
    symbols = _universe(args.universe, args.max_symbols)
    print(f"universe={args.universe} symbols={len(symbols)} horizon={args.horizon} cost(rt)={rt_cost:.4f} surge_th={args.surge}", flush=True)

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
        close = pd.to_numeric(frame["Close"], errors="coerce")
        rsi2, z = _osc(close)
        vs = _vsurge(frame)
        pos = {ts.strftime("%Y-%m-%d"): k for k, ts in enumerate(frame.index)}
        for e in evs:
            if e.get("pullback_hv_score") is None:
                continue
            i = pos.get(e["date"])
            if i is None or i < 21:
                continue
            e["rsi2"] = float(rsi2.iloc[i]) if pd.notna(rsi2.iloc[i]) else None
            e["z"] = float(z.iloc[i]) if pd.notna(z.iloc[i]) else None
            e["vsurge"] = float(vs.iloc[i]) if pd.notna(vs.iloc[i]) else None
            e["net_excess"] = e["excess_return"] - rt_cost
            events.append(e)
        used += 1

    print(f"symbols_with_data={used} events={len(events)}", flush=True)

    scores = sorted(e["pullback_hv_score"] for e in events)
    cutoff = scores[int(args.top_quantile * (len(scores) - 1))] if scores else 0.0
    top = [e for e in events if e["pullback_hv_score"] >= cutoff]

    def is_dos(e):
        return (e.get("z") is not None and e["z"] < -1.5) or (e.get("rsi2") is not None and e["rsi2"] < 10)

    def is_surge(e):
        return e.get("vsurge") is not None and e["vsurge"] > args.surge

    dos = [e for e in top if is_dos(e)]
    dos_surge = [e for e in dos if is_surge(e)]
    dos_nosurge = [e for e in dos if not is_surge(e)]
    notdos_surge = [e for e in top if not is_dos(e) and is_surge(e)]
    notdos_nosurge = [e for e in top if not is_dos(e) and not is_surge(e)]

    groups = [
        _summary("pullback_hv TOP (base)", top),
        _summary("DOS (shipped filter)", dos),
        _summary("  DOS & SURGE", dos_surge),
        _summary("  DOS & no-surge", dos_nosurge),
        _summary("NOT-DOS & SURGE", notdos_surge),
        _summary("NOT-DOS & no-surge", notdos_nosurge),
    ]
    print("\n=== GROUPS (net-of-cost excess vs own baseline; CI = cluster bootstrap by ticker) ===", flush=True)
    for g in groups:
        if g["n"] == 0:
            print(f"  {g['group']:26} n=0", flush=True)
            continue
        ci = g["ci"]
        ci_s = f"[{ci[0]}, {ci[1]}]" if ci[0] is not None else "[n/a]"
        sig = "SIG(>0)" if g["significant"] else "not-sig"
        print(f"  {g['group']:26} n={g['n']:<5} tk={g['tickers']:<3} net={g['mean_net_excess']*100:+.3f}%  CI95={ci_s}  hit={g['hit']:.0%}  {sig}", flush=True)

    print("\n=== INCREMENTAL TESTS (difference-of-means, cluster bootstrap CI) ===", flush=True)
    # 1) Inside DOS: does surge add over no-surge?
    d, lo, hi = _diff_ci(dos_surge, dos_nosurge)
    print(f"  [DOS&surge] - [DOS&no-surge]   diff={None if d is None else f'{d*100:+.3f}%'}  CI95={None if lo is None else f'[{lo}, {hi}]'}  -> {'INCREMENTAL (CI>0)' if (lo is not None and lo>0) else 'no incremental edge'}", flush=True)
    # 2) Inside non-DOS: does surge add there too (i.e. is surge a standalone effect)?
    d2, lo2, hi2 = _diff_ci(notdos_surge, notdos_nosurge)
    print(f"  [NOT-DOS&surge] - [NOT-DOS&no-surge]   diff={None if d2 is None else f'{d2*100:+.3f}%'}  CI95={None if lo2 is None else f'[{lo2}, {hi2}]'}  -> {'surge helps outside DOS too' if (lo2 is not None and lo2>0) else 'no edge outside DOS'}", flush=True)
    # 3) Is DOS&surge better than DOS baseline itself?
    d3, lo3, hi3 = _diff_ci(dos_surge, dos)
    print(f"  [DOS&surge] - [DOS all]   diff={None if d3 is None else f'{d3*100:+.3f}%'}  CI95={None if lo3 is None else f'[{lo3}, {hi3}]'}", flush=True)

    print("\n=== VERDICT ===", flush=True)
    inc = (lo is not None and lo > 0 and len(dos_surge) >= 50)
    if inc:
        print(f"  -> VSURGE adds INCREMENTAL edge on top of deep-oversold (+{d*100:.3f}%, CI>0, n={len(dos_surge)}).", flush=True)
        print("     Worth adding as a SECOND filter / score input within the DOS subset.", flush=True)
    else:
        reason = "thin sample" if len(dos_surge) < 50 else "difference CI includes 0 (overlaps DOS)"
        print(f"  -> VSURGE does NOT add a distinct increment over deep-oversold ({reason}).", flush=True)
        print("     It mostly captures the SAME capitulation events DOS already flags. Do NOT add as separate filter.", flush=True)
    print("\nCaveats: in-sample, single period; archive_only (survivorship); daily volume proxy.", flush=True)


if __name__ == "__main__":
    main()
