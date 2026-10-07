#!/usr/bin/env python3
"""Reconstruct point-in-time S&P 500 membership and archive dated snapshots.

R1 / contrarian calibration so far ran on the *current* S&P 500 constituents,
which carries survivor bias (we never see oversold names that were later
delisted).  This script rebuilds true membership for past dates from Wikipedia's
constituent-changes table, so ``build_signal_calibration.py --pit-grid`` can
replay with the names that were actually in the index at the time.

Reconstruction logic: start from today's constituents, then for every change
that took effect *after* the as-of date, undo it -- the "Added" ticker was not
yet a member, and the "Removed" ticker still was.

Honest residual: yfinance has no prices for delisted / acquired removed names,
so membership control is partial; the calibration report quantifies how many
reconstructed members are unfetchable.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_DIR = SCRIPT_DIR.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from scripts.screening_framework_v2_optimized import (  # noqa: E402
    CONFIG,
    archive_universe_snapshot,
    normalize_yfinance_symbol,
)

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def _fetch_tables() -> list[pd.DataFrame]:
    req = urllib.request.Request(WIKI_URL, headers={"User-Agent": "Mozilla/5.0 Vibe-Trading PIT reconstruction"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    return pd.read_html(io.StringIO(html))


def _flatten(col: Any) -> str:
    if isinstance(col, tuple):
        return " ".join(str(c) for c in col)
    return str(col)


def _current_members(table: pd.DataFrame) -> set[str]:
    symbol_col = next((c for c in table.columns if str(c).strip().lower() in {"symbol", "ticker"}), None)
    return {
        normalize_yfinance_symbol(str(x))
        for x in table[symbol_col].dropna().tolist()
        if str(x).strip() and str(x).strip().lower() != "nan"
    }


def _parse_changes(table: pd.DataFrame) -> list[dict[str, Any]]:
    cols = {_flatten(c).lower(): c for c in table.columns}
    date_col = next((orig for key, orig in cols.items() if "effective date" in key or key.strip() == "date"), None)
    added_col = next((orig for key, orig in cols.items() if "added" in key and "ticker" in key), None)
    removed_col = next((orig for key, orig in cols.items() if "removed" in key and "ticker" in key), None)
    if date_col is None or (added_col is None and removed_col is None):
        return []
    changes: list[dict[str, Any]] = []
    for _, row in table.iterrows():
        eff = pd.to_datetime(str(row[date_col]), errors="coerce")
        if pd.isna(eff):
            continue
        added = str(row[added_col]).strip() if added_col is not None else ""
        removed = str(row[removed_col]).strip() if removed_col is not None else ""
        changes.append({
            "date": eff.date(),
            "added": normalize_yfinance_symbol(added) if added and added.lower() != "nan" else "",
            "removed": normalize_yfinance_symbol(removed) if removed and removed.lower() != "nan" else "",
        })
    changes.sort(key=lambda c: c["date"], reverse=True)  # newest first for unwinding
    return changes


def membership_as_of(as_of: date, current: set[str], changes: list[dict[str, Any]]) -> set[str]:
    """Reconstruct the index membership as of ``as_of`` by unwinding later changes."""
    members = set(current)
    for change in changes:  # newest -> oldest
        if change["date"] <= as_of:
            break
        # Undo a change that happened after as_of.
        if change["added"]:
            members.discard(change["added"])
        if change["removed"]:
            members.add(change["removed"])
    return members


def _quarter_grid(quarters: int) -> list[date]:
    today = datetime.now(timezone.utc).date()
    anchor = date(today.year, ((today.month - 1) // 3) * 3 + 1, 1)  # start of current quarter
    grid: list[date] = []
    y, m = anchor.year, anchor.month
    for _ in range(quarters):
        m -= 3
        if m <= 0:
            m += 12
            y -= 1
        grid.append(date(y, m, 1))
    return sorted(grid)


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconstruct and archive point-in-time S&P 500 membership.")
    parser.add_argument("--quarters", type=int, default=8, help="Number of quarterly as-of snapshots to archive.")
    parser.add_argument("--dates", default="", help="Explicit comma-separated YYYY-MM-DD as-of dates (overrides --quarters).")
    parser.add_argument("--snapshot-dir", default=CONFIG["universe_snapshot_dir"])
    parser.add_argument("--output", default=str(AGENT_DIR / "runs" / "_pit_snapshots.json"))
    args = parser.parse_args()
    CONFIG["universe_snapshot_dir"] = args.snapshot_dir

    tables = _fetch_tables()
    current = _current_members(tables[0])
    changes = _parse_changes(tables[1]) if len(tables) > 1 else []
    if not changes:
        print("[WARN] no changes table parsed; PIT reconstruction unavailable.", flush=True)

    if args.dates.strip():
        grid = sorted(datetime.strptime(d.strip(), "%Y-%m-%d").date() for d in args.dates.split(",") if d.strip())
    else:
        grid = _quarter_grid(args.quarters)

    all_removed = sorted({c["removed"] for c in changes if c["removed"]})
    archived = []
    for as_of in grid:
        members = membership_as_of(as_of, current, changes)
        path = archive_universe_snapshot("spx", sorted(members), "wikipedia:pit_reconstruction", effective_date=as_of.isoformat())
        not_in_current = sorted(members - current)
        archived.append({
            "as_of": as_of.isoformat(),
            "members": len(members),
            "reconstructed_removed_in_set": len(not_in_current),
            "snapshot": str(path),
        })
        print(f"[pit] {as_of}: {len(members)} members ({len(not_in_current)} since-removed names re-included)", flush=True)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": WIKI_URL,
        "current_members": len(current),
        "changes_parsed": len(changes),
        "changes_date_range": [str(changes[-1]["date"]), str(changes[0]["date"])] if changes else None,
        "total_historically_removed_tickers": len(all_removed),
        "grid": [d.isoformat() for d in grid],
        "archived": archived,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nArchived {len(archived)} PIT snapshots. Report: {output}", flush=True)


if __name__ == "__main__":
    main()
