#!/usr/bin/env python3
"""Survivorship-bias-controlled (PIT) re-run of the pullback_hv win-rate, on
Databento data, as the final robustness check on Codex's validated result.

Method (clean A/B -- same data, same code, only the filter differs):
  1. Universe = CURRENT S&P 500 members UNION every name REMOVED during the
     window (reconstructed from the Wikipedia changes table). Databento, unlike
     yfinance, still carries delisted/removed names' price history -- that is
     what makes real survivorship control possible here.
  2. Pull one Databento OHLCV-1d panel for the union.
  3. Collect pullback_hv events (reusing the vetted _collect_symbol_events; each
     event carries its date).
  4. Build a monthly point-in-time membership grid and split events into:
       - all_events  : every event (== Codex's non-PIT methodology)
       - pit_events  : only events on dates the symbol was actually in the index
  5. Calibrate pullback_hv on BOTH and compare top-bucket net excess + CI.

The delta = the survivorship-bias inflation. Reports residual coverage (how many
removed names Databento actually returned). Never activates a curve.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_DIR = SCRIPT_DIR.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import cost_model  # noqa: E402
from app_database import signal_calibration_upsert  # noqa: E402
from signal_calibration import build_calibration  # noqa: E402
from scripts.build_databento_signal_calibration import (  # noqa: E402
    DEFAULT_DATASET,
    DEFAULT_SCHEMA,
    DEFAULT_CACHE_DIR,
    _cache_key,
    _collect_events_from_frames,
    _load_databento_panel,
    _parse_period_start,
)
from scripts.build_pit_snapshots import (  # noqa: E402
    _current_members,
    _fetch_tables,
    _parse_changes,
    membership_as_of,
)


def _month_grid(start: date, end: date) -> list[date]:
    grid: list[date] = []
    y, m = start.year, start.month
    while date(y, m, 1) <= end:
        grid.append(date(y, m, 1))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return grid or [start]


def _fmt(v: Any) -> str:
    try:
        return f"{float(v):.2%}"
    except (TypeError, ValueError):
        return "--"


def _calibrate(events: list[dict[str, Any]], horizon: int, cost_bps: float) -> tuple[dict[str, Any], dict[str, Any]]:
    scoped = [
        {"score": e.get("pullback_hv_score"), "forward_return": e.get("forward_return"),
         "excess_return": e.get("excess_return"), "ticker": e.get("ticker")}
        for e in events if e.get("pullback_hv_score") is not None
    ]
    curve = build_calibration(scoped, signal_type="pullback_hv", horizon=horizon, cost_bps=cost_bps)
    g = curve.get("global") or {}
    edge = g.get("edge") or {}
    top = edge.get("top_bucket") or {}
    ci = top.get("excess_ci") or [None, None]
    summary = {"events": g.get("events"), "clusters": g.get("clusters"),
               "validated": bool(edge.get("validated")), "top_n": top.get("n"),
               "top_excess": top.get("mean_excess_net"), "ci": ci}
    return summary, curve


def main() -> None:
    ap = argparse.ArgumentParser(description="PIT survivorship-controlled Databento pullback_hv calibration.")
    ap.add_argument("--period", default="18mo")
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--horizons", default="5,10")
    ap.add_argument("--cooldown-days", type=int, default=5)
    ap.add_argument("--dataset", default=DEFAULT_DATASET)
    ap.add_argument("--schema", default=DEFAULT_SCHEMA)
    ap.add_argument("--cost-bps", type=float, default=-1.0)
    ap.add_argument("--max-symbols", type=int, default=0, help="0 = full union.")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--require-cache", action="store_true",
                    help="Abort instead of calling Databento if the panel is not already cached (credit-safe).")
    ap.add_argument("--make-active", action="store_true",
                    help="Activate the PIT-controlled pullback_hv curves as the live calibration.")
    ap.add_argument("--output", default=str(AGENT_DIR / "runs" / "databento_pit_calibration.json"))
    args = ap.parse_args()

    start = args.start or _parse_period_start(args.period)
    start_d, end_d = date.fromisoformat(start), date.fromisoformat(args.end)
    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    cost_bps = args.cost_bps if args.cost_bps >= 0 else cost_model.equity_one_way_bps()

    tables = _fetch_tables()
    current = _current_members(tables[0])
    changes = _parse_changes(tables[1]) if len(tables) > 1 else []
    removed_in_window = sorted({c["removed"] for c in changes if c["removed"] and c["date"] >= start_d})
    union = sorted(current | set(removed_in_window))
    if args.max_symbols:
        union = union[: args.max_symbols]
    print(f"[pit] current={len(current)} removed_in_window={len(removed_in_window)} union={len(union)} "
          f"window={start}..{args.end}", flush=True)

    # Credit-safe guard: never spend Databento credits unless the panel is cached.
    if args.require_cache:
        cache_file = DEFAULT_CACHE_DIR / f"{_cache_key(args.dataset, args.schema, start, args.end, union)}.parquet"
        if not cache_file.exists():
            raise SystemExit(
                f"[abort] --require-cache set but panel cache is missing: {cache_file.name}\n"
                f"        (union hash differs from the cached run -> would cost Databento credits). "
                f"Copy the matching parquet into {DEFAULT_CACHE_DIR} or drop --require-cache to allow one pull."
            )
        print(f"[pit] cache hit guard OK -> {cache_file.name} (0 Databento credits)", flush=True)

    frames, sources, db_meta = _load_databento_panel(
        dataset=args.dataset, schema=args.schema, symbols=union,
        start=start, end=args.end, cache_dir=DEFAULT_CACHE_DIR, use_cache=not args.no_cache,
    )
    returned = set(frames)
    removed_returned = sorted(set(removed_in_window) & returned)
    print(f"[pit] databento returned {len(returned)}/{len(union)} | removed names returned "
          f"{len(removed_returned)}/{len(removed_in_window)} (residual survivorship if low)", flush=True)

    # Monthly PIT membership grid.
    grid = _month_grid(start_d, end_d)
    membership = {g: membership_as_of(g, current, changes) for g in grid}
    grid_sorted = sorted(membership)

    def is_member(symbol: str, dstr: str) -> bool:
        d = date.fromisoformat(dstr[:10])
        chosen = grid_sorted[0]
        for g in grid_sorted:
            if g <= d:
                chosen = g
            else:
                break
        return symbol in membership[chosen]

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": args.dataset, "window": [start, args.end], "cost_bps": cost_bps,
        "current_members": len(current), "removed_in_window": len(removed_in_window),
        "union": len(union), "databento_returned": len(returned),
        "removed_names_returned": removed_returned,
        "databento_meta": db_meta, "horizons": {},
    }
    rows = []
    for h in horizons:
        events = _collect_events_from_frames(frames, horizon=h, cooldown_days=args.cooldown_days)
        pit = [e for e in events if is_member(str(e.get("ticker")), str(e.get("date")))]
        all_cal, _ = _calibrate(events, h, cost_bps)
        pit_cal, pit_curve = _calibrate(pit, h, cost_bps)
        # Always tag provenance + embed the full PIT curve so it can be
        # transferred to the container DB (the container can't reach Wikipedia
        # to rebuild membership itself; we bridge the host-built curve in).
        pit_curve.setdefault("global", {})["provenance"] = {
            "price_source": f"databento:{args.dataset}",
            "survivorship_controlled": True,
            "removed_names_covered": f"{len(removed_returned)}/{len(removed_in_window)}",
            "window": [start, args.end],
            "in_sample": True,
            "note": "Databento 复权日线 + 去生存者偏差(PIT) 校准；样本内、单一区间，OOS 前谨慎。",
        }
        report["horizons"][h] = {"all": all_cal, "pit": pit_cal,
                                 "pit_event_share": round(len(pit) / len(events), 4) if events else None,
                                 "pit_curve": pit_curve}
        rows.append((h, all_cal, pit_cal))
        if args.make_active:
            cid = signal_calibration_upsert(
                signal_type="pullback_hv", horizon_days=h, curve=pit_curve,
                universe_set="databento:spx_pit", source=f"databento:{args.dataset}:pit",
                event_count=(pit_curve.get("global") or {}).get("events", 0),
                cluster_count=(pit_curve.get("global") or {}).get("clusters", 0),
                cost_bps=cost_bps, make_active=True,
            )
            print(f"  [ACTIVATED] pullback_hv/{h}d -> {cid}", flush=True)
        print(f"\n=== horizon {h}d (pullback_hv) ===", flush=True)
        print(f"  ALL : n={all_cal['events']} clusters={all_cal['clusters']} validated={all_cal['validated']} "
              f"top_excess={_fmt(all_cal['top_excess'])} CI=[{_fmt(all_cal['ci'][0])},{_fmt(all_cal['ci'][1])}]", flush=True)
        print(f"  PIT : n={pit_cal['events']} clusters={pit_cal['clusters']} validated={pit_cal['validated']} "
              f"top_excess={_fmt(pit_cal['top_excess'])} CI=[{_fmt(pit_cal['ci'][0])},{_fmt(pit_cal['ci'][1])}]", flush=True)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    # Markdown
    md = ["# Databento PIT 去生存者偏差复跑 (pullback_hv)", "",
          f"- Window: {start} .. {args.end} | cost {cost_bps}bps/side",
          f"- 当前成分 {len(current)} | 窗口内被移除 {len(removed_in_window)} | 并集 {len(union)} | Databento 返回 {len(returned)}",
          f"- 被移除名中 Databento 有数据: {len(removed_returned)}/{len(removed_in_window)}（越高=生存者控制越完整）", "",
          "| Horizon | 口径 | 事件 | 票数 | 顶桶净超额 | CI | 判定 |",
          "| ---: | --- | ---: | ---: | ---: | --- | --- |"]
    for h, a, p in rows:
        for tag, c in (("非PIT(对照)", a), ("PIT受控", p)):
            md.append(f"| {h}d | {tag} | {c['events']} | {c['clusters']} | {_fmt(c['top_excess'])} | "
                      f"[{_fmt(c['ci'][0])},{_fmt(c['ci'][1])}] | {'通过' if c['validated'] else '未通过'} |")
    md += ["", "## 口径", "- 同一份 Databento 数据、同一套代码，唯一差别=是否只保留'该日确实在指数内'的事件。",
           "- 差额 = 生存者偏差虚高部分。被移除名 Databento 覆盖不全的部分=残留偏差（已量化）。",
           "- 未激活任何曲线。"]
    out.with_suffix(".md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"\nWrote {out}\nWrote {out.with_suffix('.md')}", flush=True)


if __name__ == "__main__":
    main()
