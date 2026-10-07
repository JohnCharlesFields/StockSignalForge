#!/usr/bin/env python3
"""Validate the post-earnings-announcement-drift (PEAD) hypothesis on a small,
free-tier-affordable sample, using the SAME calibration + cluster-bootstrap CI
gate as R1 (so the verdict is honest and comparable).

Hypothesis: a positive EPS surprise predicts a positive *net-of-cost forward
excess* return vs the stock's own baseline over the next ``horizon`` days. If the
top surprise bucket's excess CI excludes 0, the signal is "validated"; otherwise
we do NOT ship it (same discipline that rejected the A-share day-trade filter).

Event = one past earnings report:
  * score          = sigmoid(k * surprise)  in (0,1)
  * forward_return = close-to-close return over ``horizon`` bars, entered the
                     first session AFTER the announcement date (drift window)
  * baseline       = the symbol's unconditional mean horizon-forward return
  * excess_return  = forward_return - baseline   (gross; build_calibration nets cost)

FREE-TIER NOTE: FMP free caps earnings ``limit`` at 4, so each symbol yields only
~2-3 past events -> small N, wide CIs. This is a first read, not a final verdict;
a real test needs PREMIUM (bulk historical earnings). Cost is one FMP call/symbol.

Run:  python agent/scripts/build_pead_calibration.py --universe spx --max-symbols 60 --horizons 10,20
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
from market_data_service import get_daily_history, get_earnings_events  # noqa: E402
from signal_calibration import build_calibration  # noqa: E402
from scripts.screening_framework_v2_optimized import resolve_universe  # noqa: E402


def _sigmoid(x: float, k: float = 6.0) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-k * x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


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
    earnings = get_earnings_events(symbol)
    if not earnings:
        return events
    try:
        frame, _src = get_daily_history(symbol, period="3y")
    except Exception:
        return events
    if frame is None or frame.empty or "Close" not in frame:
        return events
    close = pd.to_numeric(frame["Close"], errors="coerce").dropna()
    if len(close) < max(horizons) + 5:
        return events
    for h in horizons:
        baseline_samples = _baseline_forward_returns(close, h)
        baseline = sum(baseline_samples) / len(baseline_samples) if baseline_samples else 0.0
        for ev in earnings:
            try:
                as_of = pd.Timestamp(ev["date"])
            except Exception:
                continue
            # Enter the first session strictly AFTER the announcement date (the
            # drift window; avoids the announcement-day gap itself).
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
                "ticker": symbol,
                "score": _sigmoid(float(ev["surprise"])),
                "surprise": float(ev["surprise"]),
                "forward_return": fwd,
                "excess_return": fwd - baseline,
            })
    return events


def main() -> None:
    ap = argparse.ArgumentParser(description="Validate PEAD (earnings surprise) signal on a small sample.")
    ap.add_argument("--universe", default="spx")
    ap.add_argument("--max-symbols", type=int, default=60)
    ap.add_argument("--horizons", default="10,20")
    ap.add_argument("--cost-bps", type=float, default=None)
    ap.add_argument("--sleep", type=float, default=3.3, help="Seconds between symbols to respect FMP per-minute cap.")
    args = ap.parse_args()

    horizons = [int(h) for h in str(args.horizons).split(",") if h.strip()]
    cost_bps = args.cost_bps if args.cost_bps is not None else cost_model.equity_one_way_bps()

    tickers, source, _ = resolve_universe(args.universe)
    tickers = tickers[: args.max_symbols]
    print(f"universe={args.universe} source={source} symbols={len(tickers)} horizons={horizons} cost_bps={cost_bps:.2f}", flush=True)

    pooled: dict[int, list[dict[str, Any]]] = {h: [] for h in horizons}
    n_with_earnings = 0
    for i, sym in enumerate(tickers, 1):
        ev = _collect_symbol_events(sym, horizons)
        got = sum(len(v) for v in ev.values())
        if got:
            n_with_earnings += 1
        for h in horizons:
            pooled[h].extend(ev[h])
        if i % 10 == 0:
            print(f"  ...{i}/{len(tickers)} symbols, pooled events {sum(len(v) for v in pooled.values())}", flush=True)
        if args.sleep > 0 and i < len(tickers):
            time.sleep(args.sleep)  # respect FMP per-minute cap so all symbols fetch

    report: dict[str, Any] = {"universe": args.universe, "symbols": len(tickers),
                              "symbols_with_earnings": n_with_earnings, "cost_bps": cost_bps, "horizons": {}}
    for h in horizons:
        events = pooled[h]
        clusters = len({e["ticker"] for e in events})
        print(f"\n=== horizon {h}d: {len(events)} events / {clusters} tickers ===", flush=True)
        if len(events) < 20:
            print("  too few events to calibrate (free-tier limit).", flush=True)
            report["horizons"][h] = {"events": len(events), "clusters": clusters, "verdict": "insufficient"}
            continue
        curve = build_calibration(events, signal_type="pead", horizon=h, cost_bps=cost_bps)
        g = curve.get("global") or {}
        edge = g.get("edge") or {}
        buckets = curve.get("buckets") or []
        # Simple beats-vs-misses view (sign of surprise).
        beats = [e for e in events if e["surprise"] > 0]
        misses = [e for e in events if e["surprise"] <= 0]
        def _mean_excess_net(rows: list[dict[str, Any]]) -> float:
            rt = 2.0 * cost_bps / 10000.0
            return (sum(e["excess_return"] for e in rows) / len(rows) - rt) if rows else 0.0
        top = edge.get("top_bucket") or {}
        print(f"  top-bucket net excess: {top.get('mean_excess_net')}", flush=True)
        print(f"  top-bucket excess CI : {top.get('excess_ci')}", flush=True)
        print(f"  VALIDATED: {edge.get('validated')}", flush=True)
        print(f"  beats(n={len(beats)}) net excess {_mean_excess_net(beats)*100:+.2f}% | "
              f"misses(n={len(misses)}) net excess {_mean_excess_net(misses)*100:+.2f}%", flush=True)
        for b in buckets:
            print(f"    bucket score[{b.get('lo'):.2f},{b.get('hi'):.2f}] "
                  f"n={b.get('n')} hit={b.get('hit_rate')} excess_net={b.get('mean_excess_net')} "
                  f"CI=[{b.get('excess_ci_low')},{b.get('excess_ci_high')}]", flush=True)
        report["horizons"][h] = {
            "events": len(events), "clusters": clusters, "edge": edge,
            "beats_net_excess": _mean_excess_net(beats), "misses_net_excess": _mean_excess_net(misses),
            "beats_n": len(beats), "misses_n": len(misses),
        }

    out_path = AGENT_DIR / "runs" / "pead_calibration_report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nreport -> {out_path}", flush=True)


if __name__ == "__main__":
    main()
