"""Factor screen: which OHLCV-computable factors improve the pullback_hv edge?

The repo references many factors (momentum/ROC, reversal, ATR, RSI, MACD,
Bollinger, MA-cycle/daily_tunnel, beta, gap/overnight, turnover, plus
options/PEAD ones we have no data for). pullback_hv is a mean-reversion (oversold
bounce) signal, so the factors most likely to ADD are regime/quality filters:

  TREND   : close > MA200 / MA50  (buy-the-dip-in-an-uptrend, classic combo)
  MOM     : 12-1 momentum (252d-21d return) positive / strong
  DD      : drawdown vs 252d high (shallow = near highs vs deep = falling knife)
  RS      : 20d relative strength vs SPY
  SEAS    : turn-of-month (last 3 / first 3 trading days)
  GAP     : entry-day overnight gap (open/prev_close)

Same discipline as deep-oversold/volume backtests: net-of-cost forward excess vs
each stock's OWN baseline; cluster-bootstrap CI by ticker; a filter only "works"
if CI excludes 0 AND it beats the unfiltered top bucket. In-sample caveats apply.
Multiple filters tested -> treat thin-n / barely-significant as CANDIDATES, and
re-check survivors for orthogonality vs the already-shipped deep-oversold filter.
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


def _spy_returns(period: str):
    try:
        f, _s = get_daily_history("SPY", period=period)
        c = pd.to_numeric(f["Close"], errors="coerce")
        idx = {ts.strftime("%Y-%m-%d"): k for k, ts in enumerate(f.index)}
        return c.reset_index(drop=True), idx
    except Exception:
        return None, {}


def _cluster_ci(rows, n_boot=2500, seed=42):
    by = {}
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


def _summary(label, rows):
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
    ap.add_argument("--period", default="3y")
    ap.add_argument("--top-quantile", type=float, default=0.70)
    args = ap.parse_args()

    rt_cost = cost_model.equity_round_trip_cost()
    symbols = _universe(args.universe, args.max_symbols)
    spy_close, spy_idx = _spy_returns(args.period)
    print(f"universe={args.universe} symbols={len(symbols)} horizon={args.horizon} cost(rt)={rt_cost:.4f} spy={'ok' if spy_close is not None else 'NA'}", flush=True)

    events = []
    used = 0
    for sym in symbols:
        try:
            frame, _src = get_daily_history(sym, period=args.period)
        except Exception:
            continue
        if frame is None or frame.empty or "Close" not in frame or len(frame) < 230 + args.horizon:
            continue
        evs = _collect_symbol_events(sym, frame, args.horizon, min_launch_score=0.0, min_tunnel_score=0.0, cooldown_days=5)
        if not evs:
            continue
        close = pd.to_numeric(frame["Close"], errors="coerce").reset_index(drop=True)
        openp = pd.to_numeric(frame.get("Open"), errors="coerce").reset_index(drop=True)
        ma50 = close.rolling(50).mean()
        ma200 = close.rolling(200).mean()
        hi252 = close.rolling(252).max()
        mom = close / close.shift(252) - 1.0          # 12m
        mom_recent = close / close.shift(21) - 1.0     # 1m
        mom_12_1 = mom - mom_recent                     # 12-1 momentum
        ret20 = close / close.shift(20) - 1.0
        prev_close = close.shift(1)
        gap = openp / prev_close - 1.0
        dates = [ts.strftime("%Y-%m-%d") for ts in frame.index]
        months = [ts.month for ts in frame.index]
        pos = {d: k for k, d in enumerate(dates)}
        n = len(close)
        for e in evs:
            if e.get("pullback_hv_score") is None:
                continue
            i = pos.get(e["date"])
            if i is None or i < 210:
                continue
            def g(s):
                v = s.iloc[i]
                return float(v) if pd.notna(v) else None
            e["above_ma200"] = (g(ma200) is not None and float(close.iloc[i]) > ma200.iloc[i])
            e["above_ma50"] = (g(ma50) is not None and float(close.iloc[i]) > ma50.iloc[i])
            e["mom_12_1"] = g(mom_12_1)
            e["dd_from_high"] = (float(close.iloc[i]) / hi252.iloc[i]) if pd.notna(hi252.iloc[i]) and hi252.iloc[i] else None
            e["gap"] = g(gap)
            # turn-of-month: within last 3 or first 3 trading days of the month
            tom = False
            mo = months[i]
            # first 3 of month
            first3 = [k for k in range(max(0, i - 6), i + 1) if months[k] == mo][:3]
            # last 3: look ahead
            same_ahead = [k for k in range(i, min(n, i + 7)) if months[k] == mo]
            last3 = same_ahead[-3:] if same_ahead else []
            if i in first3 or i in last3:
                tom = True
            e["tom"] = tom
            # relative strength vs SPY (20d)
            rs = None
            if spy_close is not None:
                j = spy_idx.get(e["date"])
                if j is not None and j >= 20 and i >= 20:
                    spy_ret = float(spy_close.iloc[j] / spy_close.iloc[j - 20] - 1.0)
                    if pd.notna(ret20.iloc[i]):
                        rs = float(ret20.iloc[i]) - spy_ret
            e["rs20"] = rs
            e["net_excess"] = e["excess_return"] - rt_cost
            events.append(e)
        used += 1

    print(f"symbols_with_data={used} events={len(events)}", flush=True)
    scores = sorted(e["pullback_hv_score"] for e in events)
    cutoff = scores[int(args.top_quantile * (len(scores) - 1))] if scores else 0.0
    top = [e for e in events if e["pullback_hv_score"] >= cutoff]

    def F(key, pred):
        return [e for e in top if e.get(key) is not None and pred(e[key])]

    groups = [
        _summary("pullback_hv TOP (baseline)", top),
        _summary("  + above MA200 (uptrend)", [e for e in top if e.get("above_ma200")]),
        _summary("  + below MA200 (downtrend)", [e for e in top if e.get("above_ma200") is False]),
        _summary("  + above MA50", [e for e in top if e.get("above_ma50")]),
        _summary("  + mom_12_1 > 0", F("mom_12_1", lambda v: v > 0)),
        _summary("  + mom_12_1 > 0.10", F("mom_12_1", lambda v: v > 0.10)),
        _summary("  + mom_12_1 < 0", F("mom_12_1", lambda v: v < 0)),
        _summary("  + DD shallow (>0.90 of high)", F("dd_from_high", lambda v: v > 0.90)),
        _summary("  + DD deep (<0.75 of high)", F("dd_from_high", lambda v: v < 0.75)),
        _summary("  + RS20 > 0 (beat SPY)", F("rs20", lambda v: v > 0)),
        _summary("  + RS20 < 0 (lag SPY)", F("rs20", lambda v: v < 0)),
        _summary("  + entry gap-down <-1%", F("gap", lambda v: v < -0.01)),
        _summary("  + turn-of-month", [e for e in top if e.get("tom")]),
        _summary("  + uptrend(MA200) AND mom>0", [e for e in top if e.get("above_ma200") and (e.get("mom_12_1") or -1) > 0]),
    ]
    base = groups[0]
    print("\n=== RESULTS (net-of-cost excess vs own baseline; CI = cluster bootstrap by ticker) ===", flush=True)
    for g in groups:
        if g["n"] == 0:
            print(f"  {g['group']:34} n=0", flush=True)
            continue
        ci = g["ci"]
        ci_s = f"[{ci[0]}, {ci[1]}]" if ci[0] is not None else "[n/a]"
        better = "" if g is base else (" BETTER" if g["mean_net_excess"] > base["mean_net_excess"] else " worse")
        sig = "SIG(>0)" if g["significant"] else "not-sig"
        print(f"  {g['group']:34} n={g['n']:<5} net={g['mean_net_excess']*100:+.3f}%  CI95={ci_s}  hit={g['hit']:.0%}  {sig}{better}", flush=True)

    print("\n=== VERDICT (vs baseline; survivors still need DOS-orthogonality check) ===", flush=True)
    winners = [g for g in groups[1:] if g["n"] >= 80 and g["significant"] and g["mean_net_excess"] > base["mean_net_excess"]]
    if winners:
        for w in winners:
            print(f"  -> {w['group'].strip()}: net {w['mean_net_excess']*100:+.3f}% vs base {base['mean_net_excess']*100:+.3f}% (n={w['n']}), CI>0 -> CANDIDATE.", flush=True)
    else:
        print("  -> no factor filter beats the top bucket with CI>0 at n>=80. Nothing to pursue.", flush=True)
    print("\nCaveats: in-sample, single period; archive_only (survivorship); multiple filters tested (FDR risk).", flush=True)


if __name__ == "__main__":
    main()
