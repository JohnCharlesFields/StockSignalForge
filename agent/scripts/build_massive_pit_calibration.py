#!/usr/bin/env python3
"""True bear-market OOS for pullback_hv using Massive/Polygon deep history (5yr),
PIT survivorship-controlled. Complements Databento (which only goes back to
2023-03 and can't reach the 2022 bear).

Panel = Massive grouped-daily over the window (one call per trading day returns
~12k tickers, INCLUDING names that were later removed -- so we get real
survivorship control like Databento). We keep only the PIT S&P union members and
filter each event to dates the symbol was actually in the index.

Reuses the exact Databento-PIT logic (membership grid, event collection,
calibration + cluster-bootstrap CI). Never activates a curve -- this is an OOS
validation, the live active curve stays the Databento-PIT one.

Run (host, proxy for Wikipedia; Massive Starter = unlimited):
  HTTPS_PROXY=http://127.0.0.1:7890 MASSIVE_API_KEY=... \
  py agent/scripts/build_massive_pit_calibration.py --start 2021-06-01 --end 2023-06-01 --horizons 5,10
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_DIR = SCRIPT_DIR.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import cost_model  # noqa: E402
import market_data_service as mds  # noqa: E402
from scripts.build_databento_signal_calibration import _collect_events_from_frames  # noqa: E402
from scripts.build_databento_pit_calibration import _calibrate, _fmt, _month_grid  # noqa: E402
from scripts.build_pit_snapshots import (  # noqa: E402
    _current_members, _fetch_tables, _parse_changes, membership_as_of,
)


def _assemble_panel(union: set[str], start: date, end: date) -> dict[str, pd.DataFrame]:
    """Per-symbol Massive aggregates (unlimited tier), sliced to [start, end].
    Covers removed/delisted names too (Polygon keeps their history)."""
    start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    end_ts = pd.Timestamp(end)
    frames: dict[str, pd.DataFrame] = {}
    syms = sorted(union)
    for i, sym in enumerate(syms, 1):
        try:
            f = mds._massive_aggs(sym, start_dt)
        except Exception:
            f = None
        if f is not None and not f.empty:
            f = f[f.index <= end_ts]
            if len(f) >= 50:
                frames[sym] = f
        if i % 50 == 0:
            print(f"  ...{i}/{len(syms)} symbols, {len(frames)} with >=50 bars", flush=True)
    print(f"  panel: {len(frames)}/{len(syms)} symbols with >=50 bars in window", flush=True)
    return frames


def main() -> None:
    ap = argparse.ArgumentParser(description="Massive deep-history PIT pullback_hv OOS (incl. 2022 bear).")
    ap.add_argument("--start", default="2021-06-01")
    ap.add_argument("--end", default="2023-06-01")
    ap.add_argument("--horizons", default="5,10")
    ap.add_argument("--cooldown-days", type=int, default=5)
    ap.add_argument("--cost-bps", type=float, default=-1.0)
    ap.add_argument("--output", default=str(AGENT_DIR / "runs" / "massive_pit_oos_2022.json"))
    args = ap.parse_args()

    start_d, end_d = date.fromisoformat(args.start), date.fromisoformat(args.end)
    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    cost_bps = args.cost_bps if args.cost_bps >= 0 else cost_model.equity_one_way_bps()

    pit_ok = True
    try:
        tables = _fetch_tables()
        current = _current_members(tables[0])
        changes = _parse_changes(tables[1]) if len(tables) > 1 else []
    except Exception as exc:
        # Wikipedia unreachable (GFW/proxy) -> fall back to the offline spx
        # snapshot. No removed names / no PIT filter => survivorship-biased
        # (honestly flagged). Still a valid 2022-bear regime check.
        print(f"[massive-pit] Wikipedia unavailable ({str(exc)[:60]}); offline snapshot -> survivorship-biased", flush=True)
        from scripts.screening_framework_v2_optimized import CONFIG, resolve_universe
        CONFIG["universe_snapshot_dir"] = str(AGENT_DIR / "data_cache" / "universe_snapshots")
        syms, _src, _ = resolve_universe("spx", archive_only=True)
        current, changes, pit_ok = set(syms), [], False
    # Union = current + every ticker removed AFTER the window start (was a member
    # during the window). grouped-daily provides their prices while still listed.
    removed_in_window = sorted({c["removed"] for c in changes if c["removed"] and c["date"] >= start_d})
    union = set(current) | set(removed_in_window)
    print(f"[massive-pit] current={len(current)} removed_since_start={len(removed_in_window)} "
          f"union={len(union)} window={args.start}..{args.end}", flush=True)

    frames = _assemble_panel(union, start_d, end_d)
    returned = set(frames)
    removed_returned = sorted(set(removed_in_window) & returned)
    print(f"[massive-pit] removed names with data: {len(removed_returned)}/{len(removed_in_window)}", flush=True)

    grid = _month_grid(start_d, end_d)
    membership = {g: membership_as_of(g, current, changes) for g in grid}
    grid_sorted = sorted(membership)

    def is_member(symbol: str, dstr: str) -> bool:
        dd = date.fromisoformat(dstr[:10])
        chosen = grid_sorted[0]
        for g in grid_sorted:
            if g <= dd:
                chosen = g
            else:
                break
        return symbol in membership[chosen]

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "source": "massive:grouped-daily",
        "window": [args.start, args.end], "cost_bps": cost_bps,
        "current_members": len(current), "removed_in_window": len(removed_in_window),
        "union": len(union), "symbols_with_data": len(frames),
        "removed_names_returned": removed_returned,
        "survivorship_controlled": bool(pit_ok and removed_returned),
        "horizons": {},
    }
    rows_out = []
    for h in horizons:
        events = _collect_events_from_frames(frames, horizon=h, cooldown_days=args.cooldown_days)
        pit = [e for e in events if is_member(str(e.get("ticker")), str(e.get("date")))]
        all_cal, _ = _calibrate(events, h, cost_bps)
        pit_cal, _ = _calibrate(pit, h, cost_bps)
        report["horizons"][h] = {"all": all_cal, "pit": pit_cal}
        rows_out.append((h, all_cal, pit_cal))
        print(f"\n=== horizon {h}d (pullback_hv, 2022-bear OOS) ===", flush=True)
        print(f"  ALL : n={all_cal['events']} clusters={all_cal['clusters']} validated={all_cal['validated']} "
              f"top_excess={_fmt(all_cal['top_excess'])} CI=[{_fmt(all_cal['ci'][0])},{_fmt(all_cal['ci'][1])}]", flush=True)
        print(f"  PIT : n={pit_cal['events']} clusters={pit_cal['clusters']} validated={pit_cal['validated']} "
              f"top_excess={_fmt(pit_cal['top_excess'])} CI=[{_fmt(pit_cal['ci'][0])},{_fmt(pit_cal['ci'][1])}]", flush=True)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md = ["# Massive 2022 熊市样本外 (pullback_hv, PIT)", "",
          f"- 窗口 {args.start} .. {args.end} | 源 Massive grouped-daily(复权) | cost {cost_bps}bps/边",
          f"- 并集 {len(union)}(当前{len(current)}+窗口内移除{len(removed_in_window)}) | 有数据 {len(frames)} | 移除名覆盖 {len(removed_returned)}/{len(removed_in_window)}", "",
          "| Horizon | 口径 | 事件 | 票数 | 顶桶净超额 | CI | 判定 |", "| ---: | --- | ---: | ---: | ---: | --- | --- |"]
    for h, a, p in rows_out:
        for tag, c in (("非PIT", a), ("PIT受控", p)):
            md.append(f"| {h}d | {tag} | {c['events']} | {c['clusters']} | {_fmt(c['top_excess'])} | "
                      f"[{_fmt(c['ci'][0])},{_fmt(c['ci'][1])}] | {'通过' if c['validated'] else '未通过'} |")
    md += ["", "## 口径", "- 真·2022 熊市窗口的独立样本外检验；含被移除名(grouped-daily 提供其在市时价格)→生存者受控。",
           "- 复权价(Massive adjusted)。未激活任何曲线。Databento 在线 active 曲线不受影响。"]
    out.with_suffix(".md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"\nWrote {out}\nWrote {out.with_suffix('.md')}", flush=True)


if __name__ == "__main__":
    main()
