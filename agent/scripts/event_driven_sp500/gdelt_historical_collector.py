"""Collect historical equity news events from GDELT Doc API.

GDELT is a no-key global news index. The output schema matches
``sp500_event_scanner.py`` so the same event-impact validator can quantify
forward-return effects.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import time
import urllib.parse
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional, Tuple

import pandas as pd

from sp500_event_scanner import (
    EventRow,
    dedupe_events,
    load_universe,
    make_event,
    utc_now_iso,
    cached_http_get,
)


GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"


def compact_datetime(value: str, end: bool = False) -> str:
    dt = pd.to_datetime(value, errors="raise")
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.tz_localize("UTC")
    dt = dt.tz_convert("UTC")
    if end and len(value) <= 10:
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt.strftime("%Y%m%d%H%M%S")


def gdelt_query(company: str, symbol: str) -> str:
    cleaned = str(company).replace("&", " ").replace(",", " ")
    core = " ".join(part for part in cleaned.split()[:4] if part)
    if len(core) < 3:
        core = symbol
    return f"{core} stock"


def gdelt_items(
    symbol: str,
    company: str,
    start_dt: str,
    end_dt: str,
    max_records: int,
    cache_dir: Optional[Path],
    cache_ttl_minutes: int,
) -> List[dict]:
    params = {
        "query": gdelt_query(company, symbol),
        "mode": "ArtList",
        "format": "json",
        "maxrecords": str(max_records),
        "sort": "HybridRel",
        "startdatetime": start_dt,
        "enddatetime": end_dt,
    }
    url = f"{GDELT_DOC_URL}?{urllib.parse.urlencode(params)}"
    last_error: Optional[Exception] = None
    for attempt in range(4):
        try:
            text = cached_http_get(url, cache_dir, cache_ttl_minutes)
            break
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code != 429:
                raise
            time.sleep(2.5 * (attempt + 1))
        except Exception as exc:
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    else:
        raise RuntimeError(f"GDELT request failed after retries: {last_error}")
    payload = json.loads(text)
    out = []
    for item in payload.get("articles", []) or []:
        title = str(item.get("title") or "").strip()
        url_value = str(item.get("url") or "").strip()
        seen = str(item.get("seendate") or "").strip()
        domain = str(item.get("domain") or "gdelt").strip()
        if not title or not url_value:
            continue
        out.append(
            {
                "title": title,
                "summary": str(item.get("socialimage") or ""),
                "url": url_value,
                "published_at": seen,
                "source": f"gdelt:{domain}",
            }
        )
    return out


def collect_symbol(
    row_obj: object,
    args: argparse.Namespace,
    cache_dir: Optional[Path],
    collected_at: str,
    start_dt: str,
    end_dt: str,
) -> Tuple[List[EventRow], Optional[str]]:
    local: List[EventRow] = []
    try:
        items = gdelt_items(
            row_obj.symbol,
            row_obj.company,
            start_dt,
            end_dt,
            args.max_records_per_ticker,
            cache_dir,
            args.cache_ttl_minutes,
        )
    except Exception as exc:
        return [], f"gdelt fetch failed for {row_obj.symbol}: {exc}"

    for item in items:
        event = make_event(row_obj, item, collected_at)
        if event.relevance_score < args.min_relevance:
            continue
        local.append(event)
    if args.sleep > 0:
        time.sleep(args.sleep)
    return local, None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--tickers", default="")
    parser.add_argument("--universe-csv", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--max-records-per-ticker", type=int, default=20)
    parser.add_argument("--min-relevance", type=float, default=0.45)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--sleep", type=float, default=1.0)
    parser.add_argument("--cache-dir", default="")
    parser.add_argument("--cache-ttl-minutes", type=int, default=1440)
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir)
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(args.cache_dir) if args.cache_dir else output_dir / "cache" / "gdelt"

    universe = load_universe(args, cache_dir=cache_dir.parent)
    rows = [SimpleNamespace(**row._asdict()) for row in universe.itertuples(index=False)]
    start_dt = compact_datetime(args.start_date)
    end_dt = compact_datetime(args.end_date, end=True)
    collected_at = utc_now_iso()

    events: List[EventRow] = []
    failures = 0
    workers = max(1, min(args.workers, 16))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(collect_symbol, row, args, cache_dir, collected_at, start_dt, end_dt): row.symbol
            for row in rows
        }
        for future in concurrent.futures.as_completed(future_map):
            local, error = future.result()
            if error:
                failures += 1
                print(error, file=sys.stderr)
            events.extend(local)

    events = dedupe_events(events)
    df = pd.DataFrame([event.__dict__ for event in events])
    events_path = data_dir / "historical_events.csv"
    df.to_csv(events_path, index=False, encoding="utf-8")
    print(json.dumps({"events": len(df), "failures": failures, "output": str(events_path)}, ensure_ascii=False))
    if len(df) == 0 and failures >= len(rows):
        raise SystemExit(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
