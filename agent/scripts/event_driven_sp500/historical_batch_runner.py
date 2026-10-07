"""Batch and resume historical GDELT event collection.

This wrapper calls ``gdelt_historical_collector.py`` in small ticker batches,
keeps state on disk, merges successful batch CSVs, and optionally runs the
event-impact validator on the merged historical event set.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional

import pandas as pd

from gdelt_historical_collector import main as gdelt_main
from sp500_event_scanner import load_universe, parse_ticker_list


def chunked(items: List[str], size: int) -> List[List[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def state_path(output_dir: Path) -> Path:
    return output_dir / "artifacts" / "historical_batch_state.json"


def load_state(output_dir: Path) -> dict:
    path = state_path(output_dir)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"batches": []}


def write_state(output_dir: Path, state: dict) -> None:
    path = state_path(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def batch_done(state: dict, batch_key: str) -> bool:
    for item in state.get("batches", []):
        if item.get("batch_key") == batch_key and item.get("status") == "success":
            return True
    return False


def upsert_batch(state: dict, record: dict) -> None:
    batches = state.setdefault("batches", [])
    for i, item in enumerate(batches):
        if item.get("batch_key") == record.get("batch_key"):
            batches[i] = record
            return
    batches.append(record)


def load_tickers(args: argparse.Namespace, output_dir: Path) -> List[str]:
    if args.tickers:
        return parse_ticker_list(args.tickers)
    universe_args = SimpleNamespace(tickers="", universe_csv=args.universe_csv, limit=args.limit)
    universe = load_universe(universe_args, cache_dir=output_dir / "cache")
    return [str(x).upper() for x in universe["symbol"].tolist()]


def run_batch(args: argparse.Namespace, output_dir: Path, batch_id: int, tickers: List[str]) -> dict:
    batch_dir = output_dir / "batches" / f"batch_{batch_id:04d}"
    batch_dir.mkdir(parents=True, exist_ok=True)
    argv = [
        "--tickers",
        ",".join(tickers),
        "--start-date",
        args.start_date,
        "--end-date",
        args.end_date,
        "--output-dir",
        str(batch_dir),
        "--max-records-per-ticker",
        str(args.max_records_per_ticker),
        "--min-relevance",
        str(args.min_relevance),
        "--workers",
        str(args.workers),
        "--sleep",
        str(args.sleep),
        "--cache-dir",
        str(output_dir / "cache" / "gdelt"),
        "--cache-ttl-minutes",
        str(args.cache_ttl_minutes),
    ]
    started = time.time()
    status = "success"
    error = ""
    events = 0
    try:
        code = gdelt_main(argv)
        if code not in (0, None):
            status = "failed"
            error = f"collector returned {code}"
    except SystemExit as exc:
        if exc.code not in (0, None):
            status = "failed"
            error = f"collector exited {exc.code}"
    except Exception as exc:
        status = "failed"
        error = str(exc)

    csv_path = batch_dir / "data" / "historical_events.csv"
    if csv_path.exists():
        try:
            events = max(0, len(pd.read_csv(csv_path)))
        except Exception:
            events = 0
    if events == 0 and status == "success":
        status = "empty"
    return {
        "batch_key": f"{batch_id:04d}:{','.join(tickers)}",
        "batch_id": batch_id,
        "tickers": tickers,
        "status": status,
        "events": events,
        "error": error,
        "csv": str(csv_path),
        "elapsed_seconds": round(time.time() - started, 3),
        "updated_at": utc_now(),
    }


def merge_batches(output_dir: Path, state: dict) -> Path:
    frames = []
    for item in state.get("batches", []):
        if item.get("status") not in ("success", "empty"):
            continue
        path = Path(str(item.get("csv", "")))
        if not path.exists() or path.stat().st_size <= 1:
            continue
        try:
            frames.append(pd.read_csv(path))
        except Exception:
            continue
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    merged_path = data_dir / "historical_events.csv"
    if frames:
        merged = pd.concat(frames, ignore_index=True)
        if "event_id" in merged.columns:
            merged = merged.drop_duplicates(subset=["event_id"])
        merged.to_csv(merged_path, index=False, encoding="utf-8")
    else:
        pd.DataFrame().to_csv(merged_path, index=False, encoding="utf-8")
    return merged_path


def run_validation(args: argparse.Namespace, output_dir: Path, events_csv: Path) -> None:
    if not args.validate or not events_csv.exists() or events_csv.stat().st_size <= 1:
        return
    validator = Path(__file__).resolve().parent / "event_impact_validator.py"
    cmd = [
        sys.executable,
        str(validator),
        "--events-csv",
        str(events_csv),
        "--output-dir",
        str(output_dir / "artifacts"),
        "--benchmark",
        args.benchmark,
        "--horizons",
        args.horizons,
    ]
    subprocess.run(cmd, cwd=str(Path(__file__).resolve().parent), check=False)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--tickers", default="")
    parser.add_argument("--universe-csv", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--max-batches", type=int, default=0)
    parser.add_argument("--max-records-per-ticker", type=int, default=20)
    parser.add_argument("--min-relevance", type=float, default=0.45)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--sleep", type=float, default=1.0)
    parser.add_argument("--batch-sleep", type=float, default=10.0)
    parser.add_argument("--cache-ttl-minutes", type=int, default=10080)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--benchmark", default="SPY")
    parser.add_argument("--horizons", default="1,3,5,10")
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    state = load_state(output_dir) if args.resume else {"batches": []}
    tickers = load_tickers(args, output_dir)
    batches = chunked(tickers, max(1, args.batch_size))
    processed = 0

    for batch_id, tickers_one in enumerate(batches, start=1):
        batch_key = f"{batch_id:04d}:{','.join(tickers_one)}"
        if args.resume and batch_done(state, batch_key):
            continue
        record = run_batch(args, output_dir, batch_id, tickers_one)
        upsert_batch(state, record)
        write_state(output_dir, state)
        processed += 1
        if args.max_batches and processed >= args.max_batches:
            break
        if args.batch_sleep > 0:
            time.sleep(args.batch_sleep)

    merged_path = merge_batches(output_dir, state)
    run_validation(args, output_dir, merged_path)
    total_events = 0
    if merged_path.exists() and merged_path.stat().st_size > 1:
        try:
            total_events = len(pd.read_csv(merged_path))
        except Exception:
            total_events = 0
    summary = {
        "tickers": len(tickers),
        "batches": len(batches),
        "processed_this_run": processed,
        "successful_batches": sum(1 for b in state.get("batches", []) if b.get("status") == "success"),
        "failed_batches": sum(1 for b in state.get("batches", []) if b.get("status") == "failed"),
        "empty_batches": sum(1 for b in state.get("batches", []) if b.get("status") == "empty"),
        "events": total_events,
        "output": str(merged_path),
        "state": str(state_path(output_dir)),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
