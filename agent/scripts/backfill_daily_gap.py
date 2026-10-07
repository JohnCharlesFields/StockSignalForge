#!/usr/bin/env python3
"""Audit and fill missing equity daily bars in the existing OHLCV cache.

Uses Databento EQUS.SUMMARY consolidated daily bars. Existing bars are never
replaced; only missing dates are merged. Run without --execute to estimate the
request cost and inspect the date coverage before making a billed request.
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

AGENT_DIR = Path(__file__).resolve().parents[1]
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import market_data_service as market  # noqa: E402


DATASET = "EQUS.SUMMARY"
SCHEMA = "ohlcv-1d"


def _cache_files() -> list[Path]:
    return sorted((market._CACHE_ROOT / "ohlcv_daily").glob("*.csv"))


def _provider_symbol(cache_symbol: str) -> str:
    return cache_symbol.replace("-", ".")


def _provider_frame(frame: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    dates = pd.DatetimeIndex(frame.index)
    if dates.tz is not None:
        dates = dates.tz_convert(None)
    result = frame.copy()
    result.index = dates.normalize()
    result.index.name = "Date"
    result = result[(result.index >= pd.Timestamp(start)) & (result.index < pd.Timestamp(end))]
    cols = {name: pd.to_numeric(result[name], errors="coerce") for name in ("open", "high", "low", "close", "volume")}
    result = pd.DataFrame({name.title(): values for name, values in cols.items()}, index=result.index)
    valid = (
        (result[["Open", "High", "Low", "Close"]] > 0).all(axis=1)
        & (result["Volume"] >= 0)
        & (result["High"] >= result[["Open", "Close", "Low"]].max(axis=1))
        & (result["Low"] <= result[["Open", "Close", "High"]].min(axis=1))
    )
    return result.loc[valid]


def backfill(start: str, end: str, execute: bool, max_cost_usd: float, only_symbols: set[str] | None = None) -> dict:
    import databento as db

    files = _cache_files()
    cache_symbols = {path.stem for path in files}
    if only_symbols is not None:
        missing = only_symbols - cache_symbols
        if missing:
            raise ValueError(f"Symbols not in existing cache: {', '.join(sorted(missing))}")
        cache_symbols &= only_symbols
    provider_to_cache = {_provider_symbol(symbol): symbol for symbol in cache_symbols}
    symbols = sorted(provider_to_cache)
    if not symbols:
        raise RuntimeError("No existing OHLCV cache files were found")
    client = db.Historical()
    cost = client.metadata.get_cost(dataset=DATASET, symbols=symbols, schema=SCHEMA, start=start, end=end)
    report: dict = {
        "dataset": DATASET, "schema": SCHEMA, "start": start, "end_exclusive": end,
        "cache_symbols": len(symbols), "estimated_cost_usd": cost,
        "generated_at": datetime.now(timezone.utc).isoformat(), "executed": execute,
    }
    if not execute:
        return report
    if cost > max_cost_usd:
        raise RuntimeError(f"Estimated cost ${cost:.4f} exceeds cap ${max_cost_usd:.4f}")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        data = client.timeseries.get_range(
            dataset=DATASET, symbols=symbols, schema=SCHEMA, start=start, end=end,
        ).to_df()
    report["provider_warnings"] = [str(item.message)[:500] for item in caught]
    if data.empty or "symbol" not in data:
        raise RuntimeError("Provider returned no daily bars; cache was not changed")

    provider_symbols: set[str] = set()
    records_added = 0
    symbols_filled = 0
    mismatched: list[str] = []
    rejected: list[str] = []
    for raw_symbol, group in data.groupby("symbol", sort=False):
        provider_symbol = str(raw_symbol).upper()
        if provider_symbol not in provider_to_cache:
            continue
        provider_symbols.add(provider_symbol)
        symbol = provider_to_cache[provider_symbol]
        fresh = _provider_frame(group, start, end)
        if fresh.empty:
            rejected.append(symbol)
            continue
        old = market._read_daily_cache(symbol)
        overlap = old.index.intersection(fresh.index) if not old.empty else pd.DatetimeIndex([])
        if len(overlap):
            deviation = (fresh.loc[overlap, "Close"] / old.loc[overlap, "Close"] - 1).abs()
            if bool((deviation > 0.15).any()):
                mismatched.append(symbol)
                continue
        missing = fresh.loc[~fresh.index.isin(old.index)] if not old.empty else fresh
        if missing.empty:
            continue
        merged = market._merge_daily(old, missing)
        added = len(merged.index.difference(old.index)) if not old.empty else len(merged)
        if not added:
            rejected.append(symbol)
            continue
        market._write_daily_cache(symbol, merged)
        records_added += added
        symbols_filled += 1

    report.update({
        "provider_symbols": len(provider_symbols),
        "symbols_filled": symbols_filled,
        "records_added": records_added,
        "symbols_without_provider_data": sorted(set(symbols) - provider_symbols),
        "price_mismatch_skipped": mismatched,
        "invalid_or_filtered_skipped": rejected,
    })
    path = market._CACHE_ROOT / "backfill_reports" / f"daily_{start}_{end}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2026-08-04", help="Inclusive US session date")
    parser.add_argument("--end", default="2026-09-23", help="Exclusive US session date")
    parser.add_argument("--execute", action="store_true", help="Download and merge missing bars")
    parser.add_argument("--max-cost-usd", type=float, default=0.25)
    parser.add_argument("--symbols", nargs="*", help="Optional existing cache symbols to repair")
    args = parser.parse_args()
    if pd.Timestamp(args.start) >= pd.Timestamp(args.end):
        parser.error("--end must be after --start")
    selected = {symbol.strip().upper() for symbol in args.symbols} if args.symbols is not None else None
    print(json.dumps(backfill(args.start, args.end, args.execute, args.max_cost_usd, selected), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
