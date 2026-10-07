#!/usr/bin/env python3
"""Build and persist signal-score calibration curves (R1 closed loop).

Two data sources feed the same calibration schema:

* ``--source replay`` (default): replay launch / daily-tunnel scores across a
  research universe's price history and bin the resulting forward returns.
  This produces a large sample immediately, but current-constituent replay does
  not fully control survivor / look-ahead bias -- use archived snapshots
  (``--snapshot-date``) for a bias-controlled curve.
* ``--source live``: rebuild from resolved live ``signal_events`` accumulated by
  the running cockpit (genuine out-of-sample), once enough have been resolved.

The fitted curve maps signal score -> P(net-of-cost excess return > 0) and is
stored in the ``signal_calibration`` table.  ``signal_calibration.calibrate()``
reads the active curve at request time; until a curve exists it falls back to
the legacy sigmoid, so running this script is safe and non-breaking.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_DIR = SCRIPT_DIR.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from app_database import (  # noqa: E402
    signal_calibration_active,
    signal_calibration_upsert,
    signal_events_resolved,
)
from market_data_service import aggregate_data_quality, download_daily_history, get_daily_history  # noqa: E402
from scripts.research_signal_framework_backtest import (  # noqa: E402
    _collect_symbol_events,
    _ticker_frame,
)
from scripts.screening_framework_v2_optimized import CONFIG, load_universe_snapshot, resolve_universe  # noqa: E402
from signal_calibration import build_calibration  # noqa: E402
import cost_model  # noqa: E402
import exit_model  # noqa: E402

# Native score field used per signal type from the replay events. The
# *_contrarian variants read the same raw field; signal_calibration.normalize_score
# inverts them (R1 showed the raw scores are inverted vs net-of-cost excess).
SIGNAL_SCORE_FIELD = {
    "launch": "launch_score",
    "daily_tunnel": "tunnel_score",
    "launch_contrarian": "launch_score",
    "daily_tunnel_contrarian": "tunnel_score",
    # Volatility-expansion (HV-rise) and the combined "oversold + HV rising"
    # launch hypothesis. These score fields are pre-normalized to 0..1.
    "hv_rise": "hv_rise_norm",
    "pullback_hv": "pullback_hv_score",
}


def _collect_replay_events(
    universes: list[str],
    period: str,
    horizon: int,
    *,
    max_symbols: int,
    cooldown_days: int,
    snapshot_date: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Replay every cooldown-spaced observation across the universe (all scores)."""
    events: list[dict[str, Any]] = []
    meta: dict[str, Any] = {"universes": {}, "tickers": 0}
    all_sources: dict[str, str] = {}
    for universe in universes:
        tickers, source, _ = resolve_universe(
            universe,
            as_of=snapshot_date or None,
            archive_only=bool(snapshot_date),
        )
        if max_symbols:
            tickers = tickers[:max_symbols]
        meta["universes"][universe] = {"source": source, "tickers": len(tickers)}
        meta["tickers"] += len(tickers)
        print(f"[{universe}] downloading {len(tickers)} symbols ({source})", flush=True)
        panel, data_sources = download_daily_history(tickers, period=period)
        all_sources.update(data_sources)
        for ticker in tickers:
            # Thresholds at 0 so the full score range is sampled for calibration.
            events.extend(_collect_symbol_events(
                ticker,
                _ticker_frame(panel, ticker),
                horizon,
                min_launch_score=0.0,
                min_tunnel_score=0.0,
                cooldown_days=cooldown_days,
            ))
    meta["data_quality"] = aggregate_data_quality(all_sources)
    return events, meta


def _build_for_signal(
    raw_events: list[dict[str, Any]],
    *,
    signal_type: str,
    horizon: int,
    cost_bps: float,
    universe_set: str,
    source: str,
    data_quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    score_field = SIGNAL_SCORE_FIELD[signal_type]
    events = [
        {
            "score": event.get(score_field),
            "forward_return": event.get("forward_return"),
            "excess_return": event.get("excess_return"),
            "ticker": event.get("ticker"),
        }
        for event in raw_events
        if event.get(score_field) is not None
    ]
    curve = build_calibration(events, signal_type=signal_type, horizon=horizon, cost_bps=cost_bps)
    if data_quality is not None:
        curve["global"]["data_quality"] = data_quality
    calibration_id = signal_calibration_upsert(
        signal_type=signal_type,
        horizon_days=horizon,
        curve=curve,
        universe_set=universe_set,
        source=source,
        event_count=curve["global"]["events"],
        cluster_count=curve["global"]["clusters"],
        cost_bps=cost_bps,
        make_active=True,
    )
    return {"calibration_id": calibration_id, "curve": curve}


def _fmt_pct(value: Any) -> str:
    try:
        return f"{float(value):.2%}"
    except (TypeError, ValueError):
        return "--"


def _reliability_lines(signal_type: str, horizon: int, curve: dict[str, Any]) -> list[str]:
    edge = (curve["global"].get("edge") or {})
    top = edge.get("top_bucket") or {}
    verdict = "✅ 有 edge（顶桶超额 CI 排除 0）" if edge.get("validated") else "❌ 无验证 edge"
    ci = top.get("excess_ci") or [None, None]
    edge_line = (
        f"- **Edge 判定**: {verdict} · 区分度 {edge.get('spread', 0):.0%} · "
        f"顶桶 n={top.get('n')} 超额 {(top.get('mean_excess_net') or 0):.2%} CI[{_fmt_pct(ci[0])}, {_fmt_pct(ci[1])}]"
    )
    lines = [
        f"### {signal_type} / {horizon}d  (events={curve['global']['events']}, clusters={curve['global']['clusters']})",
        "",
        edge_line,
        "",
        "| score bucket | n | hit_rate P(excess>0) | isotonic | mean fwd (net) | mean excess (net) | Wilson 95% |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for bucket in curve["buckets"]:
        lines.append(
            f"| [{bucket['lo']:.3f}, {bucket['hi']:.3f}] | {bucket['n']} | {bucket['hit_rate']:.1%} | "
            f"{bucket.get('p_iso', bucket['hit_rate']):.1%} | {bucket['mean_forward_net']:.2%} | "
            f"{bucket['mean_excess_net']:.2%} | [{bucket['ci_low']:.1%}, {bucket['ci_high']:.1%}] |"
        )
    lines.append("")
    return lines


def _pit_windows(grid: list[str]) -> list[tuple[str, str]]:
    """Return [(as_of, window_end)] half-open windows over the sorted grid."""
    from datetime import datetime as _dt, timedelta as _td

    sorted_grid = sorted(grid)
    windows = []
    for index, as_of in enumerate(sorted_grid):
        end = sorted_grid[index + 1] if index + 1 < len(sorted_grid) else (
            _dt.strptime(as_of, "%Y-%m-%d") + _td(days=95)
        ).strftime("%Y-%m-%d")
        windows.append((as_of, end))
    return windows


def _collect_pit_events(
    grid: list[str],
    horizon: int,
    *,
    cooldown_days: int,
    max_symbols: int,
    period: str = "3y",
    _panel_cache: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Survivor-bias-controlled replay: each event is kept only if its symbol was
    an actual index member in the window [Di, D(i+1)) the event falls in.

    Uses the bulk, threaded ``download_daily_history`` (one call, cache-first,
    throttle-resistant) and slices per window -- per-symbol fetches over a
    historical window get rate-limited by yfinance at this scale.
    """
    windows = _pit_windows(grid)
    members_by_window: dict[str, set[str]] = {}
    all_symbols: set[str] = set()
    for as_of, _end in windows:
        loaded = load_universe_snapshot("spx", as_of)
        if not loaded:
            print(f"[pit] {as_of}: no archived snapshot, skipped", flush=True)
            members_by_window[as_of] = set()
            continue
        members = loaded[0][:max_symbols] if max_symbols else loaded[0]
        members_by_window[as_of] = set(members)
        all_symbols.update(members)

    if _panel_cache is not None and "panel" in _panel_cache:
        panel = _panel_cache["panel"]
    else:
        print(f"[pit] bulk-downloading {len(all_symbols)} unique members ({period})...", flush=True)
        panel, _sources = download_daily_history(sorted(all_symbols), period=period)
        if _panel_cache is not None:
            _panel_cache["panel"] = panel

    unfetchable: set[str] = set()
    events: list[dict[str, Any]] = []
    for symbol in sorted(all_symbols):
        try:
            frame = _ticker_frame(panel, symbol)
        except Exception:
            frame = None
        if frame is None or frame.empty or "Close" not in frame:
            unfetchable.add(symbol)
            continue
        try:
            symbol_events = _collect_symbol_events(
                symbol, frame, horizon, min_launch_score=0.0, min_tunnel_score=0.0, cooldown_days=cooldown_days,
            )
        except Exception:
            continue
        for ev in symbol_events:
            day = str(ev.get("date"))
            for as_of, end in windows:
                if as_of <= day < end:
                    if symbol in members_by_window.get(as_of, set()):
                        events.append(ev)
                    break

    meta = {
        "windows": [{"as_of": a, "window_end": e, "members": len(members_by_window.get(a, set()))} for a, e in windows],
        "unique_symbols": len(all_symbols),
        "unfetchable": len(unfetchable),
        "events": len(events),
    }
    print(f"[pit] {horizon}d: {len(all_symbols)} symbols, {len(unfetchable)} unfetchable, {len(events)} events", flush=True)
    return events, meta


def run_pit_mode(args: argparse.Namespace) -> None:
    """Build survivor-bias-controlled (point-in-time) calibration and compare to current."""
    grid = [d.strip() for d in args.pit_grid.split(",") if d.strip()]
    horizons = [int(item) for item in args.horizons.split(",") if item.strip()]
    signal_types = [item.strip() for item in args.signals.split(",") if item.strip() in SIGNAL_SCORE_FIELD]
    cost_bps = args.cost_bps if args.cost_bps >= 0 else cost_model.equity_one_way_bps()

    report: dict[str, Any] = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "mode": "pit_replay", "grid": grid, "cost_bps": cost_bps,
        "horizons": horizons, "signals": signal_types, "curves": [],
    }
    md = [
        "# 生存者偏差受控 (PIT) 校准对比",
        "",
        f"- Generated: {report['generated_at']}  |  网格: {', '.join(grid)}  |  成本: {cost_bps}bps/边",
        "- 每个事件只用其窗口内**当时真在指数里**的成分（含后来被移除、但仍有行情的票）。",
        "- ⚠️ yfinance 无退市票行情，`unfetchable` 列即残留生存者偏差量。",
        "",
        "| 信号 | horizon | PIT edge | PIT 顶桶超额 [CI] | 当前 edge | 当前 顶桶超额 [CI] |",
        "| --- | ---: | :---: | --- | :---: | --- |",
    ]
    panel_cache: dict[str, Any] = {}
    for horizon in horizons:
        raw_events, meta = _collect_pit_events(
            grid, horizon, cooldown_days=args.cooldown_days, max_symbols=args.max_symbols,
            period=args.period, _panel_cache=panel_cache,
        )
        report.setdefault("meta", {})[f"{horizon}d"] = meta
        for signal_type in signal_types:
            curve = build_calibration(
                [{"score": ev.get(SIGNAL_SCORE_FIELD[signal_type]),
                  "forward_return": ev.get("forward_return"),
                  "excess_return": ev.get("excess_return"),
                  "ticker": ev.get("ticker")} for ev in raw_events
                 if ev.get(SIGNAL_SCORE_FIELD[signal_type]) is not None],
                signal_type=signal_type, horizon=horizon, cost_bps=cost_bps,
            )
            curve["global"]["data_quality"] = {"note": "pit_membership_controlled", **meta}
            cal_id = signal_calibration_upsert(
                signal_type=signal_type, horizon_days=horizon, curve=curve,
                universe_set="spx_pit", source="pit_replay",
                event_count=curve["global"]["events"], cluster_count=curve["global"]["clusters"],
                cost_bps=cost_bps, make_active=False,
            )
            pit_edge = curve["global"]["edge"]
            current = signal_calibration_active(signal_type, horizon)
            cur_edge = ((current or {}).get("curve", {}).get("global", {}) or {}).get("edge", {}) if current else {}
            report["curves"].append({"signal_type": signal_type, "horizon_days": horizon,
                                     "calibration_id": cal_id, "pit_edge": pit_edge, "current_edge": cur_edge})

            def _ci(edge: dict) -> str:
                tb = (edge or {}).get("top_bucket") or {}
                ci = tb.get("excess_ci") or [None, None]
                return f"{_fmt_pct(tb.get('mean_excess_net'))} [{_fmt_pct(ci[0])}, {_fmt_pct(ci[1])}]"

            md.append(
                f"| {signal_type} | {horizon}d | {'✅' if pit_edge.get('validated') else '❌'} | {_ci(pit_edge)} | "
                f"{'✅' if cur_edge.get('validated') else '❌'} | {_ci(cur_edge)} |"
            )
            print(f"  -> {signal_type}/{horizon}d PIT validated={pit_edge.get('validated')} (id {cal_id})", flush=True)

    output = Path(args.output.replace("_signal_calibration", "_signal_calibration_pit"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    output.with_suffix(".md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"\nWrote {output} and {output.with_suffix('.md')}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build signal-score calibration curves from replay or live events.")
    parser.add_argument("--source", choices=["replay", "live"], default="replay")
    parser.add_argument("--universes", default="spx,ndx,sox", help="Comma-separated universes (replay only).")
    parser.add_argument("--period", default="5y", help="History period for replay, e.g. 2y / 5y.")
    parser.add_argument("--horizons", default="5,10", help="Comma-separated forward-return horizons.")
    parser.add_argument(
        "--signals",
        default="launch,daily_tunnel,launch_contrarian,daily_tunnel_contrarian",
        help="Comma-separated signal types to calibrate.",
    )
    parser.add_argument("--cost-bps", type=float, default=-1.0,
                        help="One-way trading cost in bps (round-trip = 2x). Default: cost_model.equity_one_way_bps().")
    parser.add_argument("--max-symbols", type=int, default=0, help="Optional per-universe cap for smoke runs.")
    parser.add_argument("--cooldown-days", type=int, default=5)
    parser.add_argument("--snapshot-date", default="", help="Historical constituent date (requires archived snapshots).")
    parser.add_argument("--snapshot-dir", default=CONFIG["universe_snapshot_dir"])
    parser.add_argument("--pit-grid", default="", help="Comma-separated as-of dates for survivor-bias-controlled PIT replay.")
    parser.add_argument("--output", default=str(AGENT_DIR / "runs" / "_signal_calibration.json"))
    args = parser.parse_args()

    CONFIG["universe_snapshot_dir"] = args.snapshot_dir
    if args.pit_grid.strip():
        run_pit_mode(args)
        return
    cost_bps = args.cost_bps if args.cost_bps >= 0 else cost_model.equity_one_way_bps()
    args.cost_bps = cost_bps
    universes = [item.strip().lower() for item in args.universes.split(",") if item.strip()]
    horizons = [int(item) for item in args.horizons.split(",") if item.strip()]
    signal_types = [item.strip() for item in args.signals.split(",") if item.strip() in SIGNAL_SCORE_FIELD]
    universe_set = ",".join(universes) if args.source == "replay" else "live"

    report: dict[str, Any] = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "source": args.source,
        "universe_set": universe_set,
        "cost_bps": args.cost_bps,
        "cost_assumptions": cost_model.cost_summary(),
        "exit_assumptions": exit_model.describe(),
        "horizons": horizons,
        "signals": signal_types,
        "curves": [],
    }
    _exit = exit_model.describe()
    md_lines = [
        "# 信号分数校准曲线（R1）",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Source: {args.source}  |  Universe: {universe_set}  |  Cost: {args.cost_bps}bps/side",
        f"- 出场口径：{_exit['mode']}  入场={_exit['entry']} 出场={_exit['exit']}（盘末买入→盘初卖出）",
        "- 胜负口径：净成本后的超额收益 vs 个股自身基线（P(excess>0)）。",
        "",
    ]

    for horizon in horizons:
        data_quality: dict[str, Any] | None = None
        if args.source == "live":
            raw_events = signal_events_resolved(horizon_days=horizon)
            # Live events already carry the per-signal score and outcomes; tag
            # the native score field both signal types can read.
            for event in raw_events:
                event.setdefault("launch_score", event.get("score"))
                event.setdefault("tunnel_score", event.get("score"))
            report.setdefault("meta", {})[f"{horizon}d"] = {"live_events": len(raw_events)}
            print(f"[live] {horizon}d: {len(raw_events)} resolved events", flush=True)
        else:
            raw_events, meta = _collect_replay_events(
                universes,
                args.period,
                horizon,
                max_symbols=args.max_symbols,
                cooldown_days=args.cooldown_days,
                snapshot_date=args.snapshot_date,
            )
            data_quality = meta.get("data_quality")
            report.setdefault("meta", {})[f"{horizon}d"] = meta
            print(f"[replay] {horizon}d: {len(raw_events)} events", flush=True)

        for signal_type in signal_types:
            # For live source, match the BASE signal type's logged events; the
            # contrarian curve is derived from the same rows (normalize inverts).
            base_type = (
                signal_type[: -len("_contrarian")]
                if signal_type.endswith("_contrarian")
                else signal_type
            )
            scoped = (
                [event for event in raw_events if event.get("signal_type") == base_type]
                if args.source == "live"
                else raw_events
            )
            built = _build_for_signal(
                scoped,
                signal_type=signal_type,
                horizon=horizon,
                cost_bps=args.cost_bps,
                data_quality=data_quality,
                universe_set=universe_set,
                source=args.source,
            )
            report["curves"].append({
                "signal_type": signal_type,
                "horizon_days": horizon,
                "calibration_id": built["calibration_id"],
                "global": built["curve"]["global"],
                "buckets": built["curve"]["buckets"],
            })
            md_lines.extend(_reliability_lines(signal_type, horizon, built["curve"]))
            print(f"  -> {signal_type}/{horizon}d active: {built['calibration_id']}", flush=True)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    output.with_suffix(".md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    print(f"\nWrote {output} and {output.with_suffix('.md')}", flush=True)


if __name__ == "__main__":
    main()
