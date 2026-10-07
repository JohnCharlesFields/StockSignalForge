#!/usr/bin/env python3
"""Persist event-level sector ETF enrichment for playbook calibration.

The previous three-stage experiment only saved group summaries. This script
adds sector-ETF and stock-vs-sector fields to every historical event so later
calibration can test those features out-of-sample.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

import pandas as pd

HERE = Path(__file__).resolve().parent
AGENT = HERE.parent
os.environ.setdefault("VIBE_MARKET_DATA_CACHE_DIR", str(AGENT / "data_cache" / "market_data"))
for path in (AGENT, HERE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from backtest_playbook_extended_experiments import _align, _ret, _sector_etf  # noqa: E402
from market_data_service import get_daily_history  # noqa: E402


RUNS_DIR = AGENT / "runs"
DEFAULT_ETFS = {
    "SPY", "QQQ", "SOXX", "XLK", "XLF", "XLV", "XLY", "XLP", "XLI",
    "XLE", "XLB", "XLU", "XLRE", "XLC", "XBI",
}


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _history(symbol: str, memo: dict[str, pd.DataFrame]) -> pd.DataFrame:
    sym = str(symbol or "").upper()
    if not sym:
        return pd.DataFrame()
    if sym not in memo:
        try:
            frame, _ = get_daily_history(sym, period="5y")
        except Exception:
            frame = pd.DataFrame()
        memo[sym] = frame if frame is not None else pd.DataFrame()
    return memo[sym]


def _precompute_symbol(
    sym: str,
    fallback: str,
    histories: dict[str, pd.DataFrame],
    stats: dict[str, int],
) -> tuple[str, str, dict[str, dict[str, Any]]]:
    etf, desc = _sector_etf(sym, fallback=fallback)
    stock = _history(sym, histories)
    sector = _history(etf, histories)
    spy = _history("SPY", histories)
    if stock.empty or sector.empty or spy.empty:
        stats["missing_history_symbols"] = stats.get("missing_history_symbols", 0) + 1
        return etf, desc, {}

    stock_s, sector_s = _align(stock, sector)
    stock_s, spy_s = _align(stock_s, spy)
    sclose = pd.to_numeric(stock_s["Close"], errors="coerce")
    eclose = pd.to_numeric(sector_s["Close"], errors="coerce")
    spyc = pd.to_numeric(spy_s["Close"], errors="coerce")
    stock_3m = sclose / sclose.shift(63) - 1.0
    stock_6m = sclose / sclose.shift(126) - 1.0
    sector_3m = eclose / eclose.shift(63) - 1.0
    sector_6m = eclose / eclose.shift(126) - 1.0
    spy_3m = spyc / spyc.shift(63) - 1.0
    spy_6m = spyc / spyc.shift(126) - 1.0

    by_date: dict[str, dict[str, Any]] = {}
    for day in stock_s.index:
        vals = [stock_3m.get(day), stock_6m.get(day), sector_3m.get(day), sector_6m.get(day), spy_3m.get(day), spy_6m.get(day)]
        if any(v is None or not math.isfinite(float(v)) for v in vals):
            continue
        s3, s6, e3, e6, p3, p6 = [float(v) for v in vals]
        by_date[pd.Timestamp(day).strftime("%Y-%m-%d")] = {
            "sector_enriched": True,
            "stock_3m": round(s3, 6),
            "stock_6m": round(s6, 6),
            "sector_3m": round(e3, 6),
            "sector_6m": round(e6, 6),
            "spy_3m": round(p3, 6),
            "spy_6m": round(p6, 6),
            "sector_3m_vs_spy": round(e3 - p3, 6),
            "sector_6m_vs_spy": round(e6 - p6, 6),
            "stock_3m_vs_sector": round(s3 - e3, 6),
            "stock_6m_vs_sector": round(s6 - e6, 6),
            "sector_strong": bool(e3 > p3 and e6 > p6),
            "stock_vs_sector_strong": bool(s3 > e3 and s6 > e6 * 0.85),
        }
    return etf, desc, by_date


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="agent/runs/pullback_playbook_spx_ndx_sox_260625.json")
    ap.add_argument("--output", default="agent/runs/pullback_playbook_sector_enriched_260625.json")
    args = ap.parse_args()

    source = Path(args.input)
    payload = json.loads(source.read_text(encoding="utf-8"))
    events = list(payload.get("events") or [])
    symbols = sorted({str(r.get("ticker") or r.get("symbol") or "").upper() for r in events if r.get("ticker") or r.get("symbol")})
    histories: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        _history(sym, histories)
    for etf in DEFAULT_ETFS:
        _history(etf, histories)

    fallback_by_symbol: dict[str, str] = {}
    for row in events:
        sym = str(row.get("ticker") or row.get("symbol") or "").upper()
        if sym and sym not in fallback_by_symbol:
            fallback_by_symbol[sym] = str(row.get("benchmark") or "SPY").upper()

    stats: dict[str, int] = {"total": len(events), "enriched": 0}
    feature_by_symbol: dict[str, tuple[str, str, dict[str, dict[str, Any]]]] = {}
    for idx, sym in enumerate(symbols, start=1):
        if idx % 25 == 0:
            print(f"precomputed {idx}/{len(symbols)} symbols", flush=True)
        feature_by_symbol[sym] = _precompute_symbol(sym, fallback_by_symbol.get(sym, "SPY"), histories, stats)

    enriched = []
    for row in events:
        sym = str(row.get("ticker") or row.get("symbol") or "").upper()
        etf, desc, by_date = feature_by_symbol.get(sym, ("SPY", "fallback", {}))
        out = dict(row)
        out["sector_etf"] = etf
        out["sector_desc"] = desc
        fields = by_date.get(str(row.get("date")))
        if fields:
            out.update(fields)
            stats["enriched"] = stats.get("enriched", 0) + 1
        else:
            out["sector_enriched"] = False
            stats["missing_event_features"] = stats.get("missing_event_features", 0) + 1
        enriched.append(out)
    out = {
        **{k: v for k, v in payload.items() if k != "events"},
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "method": "playbook_events_sector_etf_enriched",
        "input_json": str(source),
        "enrichment_stats": stats,
        "events": enriched,
    }
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(target), "stats": stats}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
