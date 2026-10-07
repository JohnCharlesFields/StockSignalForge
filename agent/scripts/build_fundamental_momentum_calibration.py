#!/usr/bin/env python3
"""Validate a FUNDAMENTAL-MOMENTUM signal (earnings actuals) on the full universe,
using Massive/Polygon free data (universe-wide financials + per-symbol daily aggs)
and the SAME calibration + cluster-bootstrap CI gate as R1.

Hypothesis: strong year-over-year diluted-EPS growth at a quarterly report predicts
positive net-of-cost forward excess return over the next ``horizon`` days (a
documented fundamental-momentum / earnings-drift effect). If the top-growth
bucket's excess CI excludes 0 -> "validated"; otherwise it is NOT shipped.

Why this is feasible now (vs FMP free which 429'd, Tiingo which gates fundamentals
to DOW30): Massive free returns quarterly income statements for ALL tickers, and
per-symbol daily aggregates, direct (no proxy). Only constraint is 5 calls/min, so
this spaces requests; ~2 calls/symbol.

Event = one quarterly report with a year-ago comparable:
  * signal    = 0.5 + 0.5*tanh(eps_yoy_growth)   in (0,1)
  * entered   = first session AFTER filing_date (info is public -> drift window)
  * forward   = close-to-close return over ``horizon`` bars
  * excess    = forward - symbol's unconditional mean horizon-forward return

Run: python agent/scripts/build_fundamental_momentum_calibration.py --max-symbols 50 --horizons 20,60 --sleep 26
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_DIR = SCRIPT_DIR.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import cost_model  # noqa: E402
from market_data_service import _massive_aggs, get_financials  # noqa: E402
from signal_calibration import build_calibration  # noqa: E402
from scripts.screening_framework_v2_optimized import resolve_universe  # noqa: E402


def _score(growth: float) -> float:
    return 0.5 + 0.5 * math.tanh(growth)


def _baseline_forward_returns(close: pd.Series, horizon: int) -> list[float]:
    out: list[float] = []
    for i in range(len(close) - horizon):
        a = float(close.iloc[i])
        b = float(close.iloc[i + horizon])
        if a > 0:
            out.append(b / a - 1.0)
    return out


def _collect_symbol_events(symbol: str, horizons: list[int]) -> dict[int, list[dict[str, Any]]]:
    events: dict[int, list[dict[str, Any]]] = {h: [] for h in horizons}
    fins = get_financials(symbol, quarters=8)  # newest-first
    if len(fins) < 5:
        return events
    frame = _massive_aggs(symbol, pd.Timestamp.utcnow().to_pydatetime().replace(year=pd.Timestamp.utcnow().year - 3))
    if frame is None or frame.empty or "Close" not in frame:
        return events
    close = pd.to_numeric(frame["Close"], errors="coerce").dropna()
    if len(close) < max(horizons) + 5:
        return events
    # Year-over-year EPS growth: quarter i vs quarter i+4 (4 quarters earlier).
    for i in range(len(fins) - 4):
        cur, prior = fins[i], fins[i + 4]
        eps_now, eps_prev = cur.get("diluted_eps"), prior.get("diluted_eps")
        fdate = cur.get("filing_date")
        if eps_now is None or eps_prev in (None, 0) or not fdate:
            continue
        try:
            growth = (float(eps_now) - float(eps_prev)) / abs(float(eps_prev))
            as_of = pd.Timestamp(str(fdate)[:10])
        except (TypeError, ValueError):
            continue
        for h in horizons:
            baseline_samples = _baseline_forward_returns(close, h)
            baseline = sum(baseline_samples) / len(baseline_samples) if baseline_samples else 0.0
            entry_pos = [p for p, ts in enumerate(close.index) if ts > as_of]
            if not entry_pos:
                continue
            ei = entry_pos[0]
            xi = ei + h
            if xi >= len(close):
                continue
            entry, exit_ = float(close.iloc[ei]), float(close.iloc[xi])
            if entry <= 0:
                continue
            fwd = exit_ / entry - 1.0
            events[h].append({
                "ticker": symbol, "score": _score(growth), "growth": growth,
                "forward_return": fwd, "excess_return": fwd - baseline,
            })
    return events


def main() -> None:
    ap = argparse.ArgumentParser(description="Validate fundamental-momentum (YoY EPS growth) signal.")
    ap.add_argument("--universe", default="spx")
    ap.add_argument("--max-symbols", type=int, default=50)
    ap.add_argument("--horizons", default="20,60")
    ap.add_argument("--cost-bps", type=float, default=None)
    ap.add_argument("--sleep", type=float, default=26.0, help="Seconds between symbols (~2 Massive calls each, 5/min cap).")
    args = ap.parse_args()

    horizons = [int(h) for h in str(args.horizons).split(",") if h.strip()]
    cost_bps = args.cost_bps if args.cost_bps is not None else cost_model.equity_one_way_bps()
    tickers, source, _ = resolve_universe(args.universe)
    tickers = tickers[: args.max_symbols]
    print(f"universe={args.universe} symbols={len(tickers)} horizons={horizons} cost_bps={cost_bps:.2f}", flush=True)

    pooled: dict[int, list[dict[str, Any]]] = {h: [] for h in horizons}
    n_with = 0
    for i, sym in enumerate(tickers, 1):
        try:
            ev = _collect_symbol_events(sym, horizons)
        except Exception as exc:
            print(f"  {sym} err {str(exc)[:60]}", flush=True)
            ev = {h: [] for h in horizons}
        if sum(len(v) for v in ev.values()):
            n_with += 1
        for h in horizons:
            pooled[h].extend(ev[h])
        if i % 5 == 0:
            print(f"  ...{i}/{len(tickers)}  symbols_with_data={n_with}  pooled={sum(len(v) for v in pooled.values())}", flush=True)
        if args.sleep > 0 and i < len(tickers):
            time.sleep(args.sleep)

    report: dict[str, Any] = {"universe": args.universe, "symbols": len(tickers),
                              "symbols_with_data": n_with, "cost_bps": cost_bps, "horizons": {}}
    rt = 2.0 * cost_bps / 10000.0
    for h in horizons:
        events = pooled[h]
        clusters = len({e["ticker"] for e in events})
        print(f"\n=== horizon {h}d: {len(events)} events / {clusters} tickers ===", flush=True)
        if len(events) < 20:
            print("  too few events to calibrate.", flush=True)
            report["horizons"][h] = {"events": len(events), "clusters": clusters, "verdict": "insufficient"}
            continue
        curve = build_calibration(events, signal_type="fundamental_momentum", horizon=h, cost_bps=cost_bps)
        edge = ((curve.get("global") or {}).get("edge") or {})
        top = edge.get("top_bucket") or {}
        hi = [e for e in events if e["growth"] > 0]
        lo = [e for e in events if e["growth"] <= 0]
        def mexc(rows: list[dict[str, Any]]) -> float:
            return (sum(e["excess_return"] for e in rows) / len(rows) - rt) if rows else 0.0
        print(f"  VALIDATED: {edge.get('validated')} | top-bucket net excess {top.get('mean_excess_net')} CI {top.get('excess_ci')}", flush=True)
        print(f"  EPS-growth>0 (n={len(hi)}) net excess {mexc(hi)*100:+.2f}% | growth<=0 (n={len(lo)}) {mexc(lo)*100:+.2f}%", flush=True)
        for b in (curve.get("buckets") or []):
            print(f"    score[{b.get('lo'):.2f},{b.get('hi'):.2f}] n={b.get('n')} hit={b.get('hit_rate')} "
                  f"excess_net={b.get('mean_excess_net')} CI=[{b.get('excess_ci_low')},{b.get('excess_ci_high')}]", flush=True)
        report["horizons"][h] = {"events": len(events), "clusters": clusters, "edge": edge,
                                 "growth_pos_net_excess": mexc(hi), "growth_neg_net_excess": mexc(lo),
                                 "n_pos": len(hi), "n_neg": len(lo)}

    out = AGENT_DIR / "runs" / "fundamental_momentum_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nreport -> {out}", flush=True)


if __name__ == "__main__":
    main()
