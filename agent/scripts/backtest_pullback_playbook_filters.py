#!/usr/bin/env python3
"""Backtest the user's pullback playbook as incremental filters.

The production edge is currently ``pullback_hv``.  This experiment asks a
smaller, auditable question:

    Does adding market regime, relative strength, liquidity, trend quality, and
    ATR/structure risk management improve the validated pullback_hv edge?

It intentionally does not modify live ranking.  Results are written to
``agent/runs`` for review before any product change.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import random
import sys
from pathlib import Path
from statistics import mean, median
from typing import Any

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
AGENT = HERE.parent
for path in (AGENT, HERE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import cost_model  # noqa: E402
from market_data_service import get_daily_history  # noqa: E402
from overnight_alpha_service import benchmark_for_universes  # noqa: E402
from research_signal_framework_backtest import _collect_symbol_events  # noqa: E402
from screening_framework_v2_optimized import resolve_universe  # noqa: E402


RUNS_DIR = AGENT / "runs"
DEFAULT_UNIVERSES = "spx,ndx,sox"
DEFAULT_HORIZON = 8


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _safe_pct(value: Any) -> str:
    v = _num(value)
    return "--" if v is None else f"{v * 100:+.3f}%"


def _universe_symbols(universes: list[str], max_symbols_per_universe: int) -> tuple[list[str], dict[str, list[str]]]:
    seen: set[str] = set()
    symbols: list[str] = []
    sources: dict[str, list[str]] = {}
    for universe in universes:
        try:
            items, _source, _meta = resolve_universe(universe, archive_only=True)
        except Exception:
            items = []
        for raw in items[: max(1, max_symbols_per_universe)]:
            symbol = str(raw or "").strip().upper()
            if not symbol:
                continue
            sources.setdefault(symbol, []).append(universe)
            if symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)
    return symbols, sources


def _align(stock: pd.DataFrame, bench: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    common = stock.index.intersection(bench.index).sort_values()
    return stock.loc[common].copy(), bench.loc[common].copy()


def _beta(stock: pd.DataFrame, bench: pd.DataFrame, idx: int, lookback: int = 120) -> float:
    if idx < 40:
        return 1.0
    sret = pd.to_numeric(stock["Close"], errors="coerce").pct_change().iloc[max(0, idx - lookback):idx].dropna()
    bret = pd.to_numeric(bench["Close"], errors="coerce").pct_change().iloc[max(0, idx - lookback):idx].dropna()
    n = min(len(sret), len(bret))
    if n < 30:
        return 1.0
    s = sret.tail(n).to_numpy(dtype=float)
    b = bret.tail(n).to_numpy(dtype=float)
    var = float(np.var(b))
    if not math.isfinite(var) or var <= 1e-12:
        return 1.0
    out = float(np.cov(s, b)[0, 1] / var)
    return max(-1.0, min(3.0, out)) if math.isfinite(out) else 1.0


def _atr_series(frame: pd.DataFrame, window: int = 14) -> pd.Series:
    high = pd.to_numeric(frame["High"], errors="coerce")
    low = pd.to_numeric(frame["Low"], errors="coerce")
    close = pd.to_numeric(frame["Close"], errors="coerce")
    prev = close.shift(1)
    tr = pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(window).mean()


def _market_ok(spy: pd.DataFrame, date: pd.Timestamp) -> bool:
    if spy.empty or date not in spy.index:
        return False
    close = pd.to_numeric(spy["Close"], errors="coerce")
    volume = pd.to_numeric(spy.get("Volume", pd.Series(0.0, index=spy.index)), errors="coerce")
    i = int(spy.index.get_loc(date))
    if i < 200:
        return False
    ma50 = close.rolling(50).mean()
    ma200 = close.rolling(200).mean()
    price_ok = close.iloc[i] > ma200.iloc[i]
    ma50_not_down = ma50.iloc[i] >= ma50.iloc[max(0, i - 10)] * 0.995
    breakdown = (
        close.iloc[i] < ma50.iloc[i] * 0.97
        and close.pct_change(5).iloc[i] < -0.04
        and volume.iloc[i] > volume.rolling(20).mean().iloc[i] * 1.3
    )
    return bool(price_ok and ma50_not_down and not breakdown)


def _event_features(
    symbol: str,
    frame: pd.DataFrame,
    bench: pd.DataFrame,
    spy: pd.DataFrame,
    idx: int,
    horizon: int,
) -> dict[str, Any] | None:
    if idx < 220 or idx + horizon >= len(frame):
        return None
    close = pd.to_numeric(frame["Close"], errors="coerce")
    open_ = pd.to_numeric(frame["Open"], errors="coerce")
    high = pd.to_numeric(frame["High"], errors="coerce")
    low = pd.to_numeric(frame["Low"], errors="coerce")
    volume = pd.to_numeric(frame.get("Volume", pd.Series(0.0, index=frame.index)), errors="coerce").fillna(0.0)
    bclose = pd.to_numeric(bench["Close"], errors="coerce")
    bopen = pd.to_numeric(bench["Open"], errors="coerce")
    if min(close.iloc[idx], open_.iloc[idx + horizon], bclose.iloc[idx], bopen.iloc[idx + horizon]) <= 0:
        return None

    ma10 = close.rolling(10).mean()
    ma20 = close.rolling(20).mean()
    ma50 = close.rolling(50).mean()
    ma200 = close.rolling(200).mean()
    atr14 = _atr_series(frame, 14)
    recent_high = float(high.iloc[max(0, idx - 20):idx + 1].max())
    pullback_pct = recent_high / float(close.iloc[idx]) - 1.0 if close.iloc[idx] > 0 else 0.0
    pullback_days = 0
    for j in range(idx, max(-1, idx - 12), -1):
        if close.iloc[j] < recent_high:
            pullback_days += 1
        else:
            break
    ret60 = close.iloc[idx] / close.iloc[idx - 60] - 1.0
    ret126 = close.iloc[idx] / close.iloc[idx - 126] - 1.0
    bret60 = bclose.iloc[idx] / bclose.iloc[idx - 60] - 1.0
    bret126 = bclose.iloc[idx] / bclose.iloc[idx - 126] - 1.0
    avg_dollar_volume = float((close * volume).rolling(20).mean().iloc[idx])
    near_ma10_20 = (
        abs(close.iloc[idx] / ma10.iloc[idx] - 1.0) <= 0.035
        or abs(close.iloc[idx] / ma20.iloc[idx] - 1.0) <= 0.045
    )
    vol_dry = volume.rolling(5).mean().iloc[idx] <= volume.rolling(20).mean().iloc[idx] * 0.90
    no_key_low_break = close.iloc[idx] > low.iloc[max(0, idx - 20):idx].min()
    close_above_prev_high = close.iloc[idx] > high.iloc[idx - 1]
    lower_shadow = max(min(open_.iloc[idx], close.iloc[idx]) - low.iloc[idx], 0.0)
    candle_range = max(high.iloc[idx] - low.iloc[idx], 1e-9)
    lower_wick = lower_shadow / candle_range
    trend_filter = bool(
        close.iloc[idx] > ma20.iloc[idx]
        and close.iloc[idx] > ma50.iloc[idx]
        and ma50.iloc[idx] > ma200.iloc[idx]
        and ma20.iloc[idx] > ma20.iloc[idx - 5]
    )
    rs_filter = bool(ret60 > bret60 and ret126 > bret126 * 0.85)
    normal_pullback = bool(
        2 <= pullback_days <= 7
        and 0.015 <= pullback_pct <= 0.14
        and near_ma10_20
        and vol_dry
        and no_key_low_break
    )
    aggressive_wick = bool(near_ma10_20 and lower_wick >= 0.30 and pullback_pct >= 0.02)
    return {
        "market_ok": _market_ok(spy, frame.index[idx]),
        "trend_filter": trend_filter,
        "rs_filter": rs_filter,
        "liquid": bool(avg_dollar_volume >= 50_000_000 and close.iloc[idx] >= 10.0),
        "normal_pullback": normal_pullback,
        "aggressive_wick": aggressive_wick,
        "close_above_prev_high": bool(close_above_prev_high),
        "near_ma10_20": bool(near_ma10_20),
        "volume_dry": bool(vol_dry),
        "pullback_days": int(pullback_days),
        "pullback_pct": round(float(pullback_pct), 5),
        "ret60_vs_benchmark": round(float(ret60 - bret60), 5),
        "ret126_vs_benchmark": round(float(ret126 - bret126), 5),
        "avg_dollar_volume": round(avg_dollar_volume, 2),
        "atr14": round(float(atr14.iloc[idx]), 5) if pd.notna(atr14.iloc[idx]) else None,
        "beta": _beta(frame, bench, idx),
        "benchmark_return": float(bopen.iloc[idx + horizon] / bclose.iloc[idx] - 1.0),
    }


def _cluster_ci(rows: list[dict[str, Any]], key: str, n_boot: int = 2000, seed: int = 42) -> tuple[float | None, float | None]:
    by_symbol: dict[str, list[float]] = {}
    for row in rows:
        value = _num(row.get(key))
        if value is not None:
            by_symbol.setdefault(str(row["ticker"]), []).append(value)
    clusters = [item for item in by_symbol if by_symbol[item]]
    if len(clusters) < 2:
        return None, None
    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(n_boot):
        sample = [rng.choice(clusters) for _ in clusters]
        values = [value for ticker in sample for value in by_symbol[ticker]]
        if values:
            estimates.append(float(sum(values) / len(values)))
    estimates.sort()
    return estimates[int(0.025 * (len(estimates) - 1))], estimates[int(0.975 * (len(estimates) - 1))]


def _summary(label: str, rows: list[dict[str, Any]], key: str = "net_beta_alpha") -> dict[str, Any]:
    values = [_num(row.get(key)) for row in rows]
    values = [v for v in values if v is not None]
    if not values:
        return {"label": label, "n": 0, "symbols": 0}
    lo, hi = _cluster_ci(rows, key)
    own_values = [_num(row.get("net_own_excess")) for row in rows]
    own_values = [v for v in own_values if v is not None]
    return {
        "label": label,
        "n": len(values),
        "symbols": len({str(row["ticker"]) for row in rows}),
        "mean_beta_alpha": round(mean(values), 6),
        "median_beta_alpha": round(median(values), 6),
        "alpha_win_rate": round(sum(v > 0 for v in values) / len(values), 4),
        "ci_low": round(lo, 6) if lo is not None else None,
        "ci_high": round(hi, 6) if hi is not None else None,
        "significant": bool(lo is not None and lo > 0),
        "mean_own_excess": round(mean(own_values), 6) if own_values else None,
    }


def _bar(frame: pd.DataFrame, idx: int) -> dict[str, float] | None:
    if idx < 0 or idx >= len(frame):
        return None
    out: dict[str, float] = {}
    for col in ("Open", "High", "Low", "Close"):
        value = _num(frame[col].iloc[idx])
        if value is None or value <= 0:
            return None
        out[col.lower()] = value
    return out


def _simulate_atr_trade(
    frame: pd.DataFrame,
    bench: pd.DataFrame,
    idx: int,
    *,
    beta: float,
    atr_mult: float,
    horizon: int,
    reward_risk: float,
) -> dict[str, Any] | None:
    close = pd.to_numeric(frame["Close"], errors="coerce")
    low = pd.to_numeric(frame["Low"], errors="coerce")
    atr = _atr_series(frame, 14)
    entry = _num(close.iloc[idx])
    atr_value = _num(atr.iloc[idx])
    if entry is None or atr_value is None or entry <= 0 or atr_value <= 0 or idx + horizon >= len(frame):
        return None
    key_low = _num(low.iloc[max(0, idx - 10): idx + 1].min(), entry)
    risk_dist = max(entry - float(key_low), atr_mult * atr_value)
    if risk_dist <= 0:
        return None
    stop = max(0.01, entry - risk_dist)
    target = entry + reward_risk * risk_dist
    exit_idx = idx + horizon
    exit_price = _num(frame["Open"].iloc[exit_idx], _num(frame["Close"].iloc[exit_idx]))
    exit_reason = "time_exit_open"
    for j in range(idx + 1, idx + horizon + 1):
        b = _bar(frame, j)
        if not b:
            continue
        if b["low"] <= stop:
            exit_idx = j
            exit_price = min(b["open"], stop) if b["open"] < stop else stop
            exit_reason = "stop"
            break
        if b["high"] >= target:
            exit_idx = j
            exit_price = target
            exit_reason = "target"
            break
    if exit_price is None or exit_price <= 0:
        return None
    bench_entry = _num(bench["Close"].iloc[idx])
    bench_exit = _num(bench["Open"].iloc[exit_idx], _num(bench["Close"].iloc[exit_idx]))
    if bench_entry is None or bench_exit is None or bench_entry <= 0 or bench_exit <= 0:
        return None
    cost = cost_model.equity_round_trip_cost(price=entry)
    ret = exit_price / entry - 1.0 - cost
    bench_ret = bench_exit / bench_entry - 1.0
    return {
        "net_beta_alpha": ret - beta * bench_ret,
        "net_return": ret,
        "exit_reason": exit_reason,
        "hold_days": int(exit_idx - idx),
        "stop_pct": round((entry - stop) / entry, 5),
        "reward_risk": reward_risk,
    }


def _path_summary(label: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    base = _summary(label, rows, key="path_net_beta_alpha")
    if not rows:
        return base
    exits: dict[str, int] = {}
    for row in rows:
        exits[str(row.get("path_exit_reason") or "unknown")] = exits.get(str(row.get("path_exit_reason") or "unknown"), 0) + 1
    base["exit_reasons"] = exits
    base["target_rate"] = round(exits.get("target", 0) / len(rows), 4)
    base["stop_rate"] = round(exits.get("stop", 0) / len(rows), 4)
    return base


def _write_reports(payload: dict[str, Any], prefix: str) -> tuple[Path, Path]:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    base = prefix or f"pullback_playbook_filters_{dt.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    json_path = RUNS_DIR / f"{base}.json"
    md_path = RUNS_DIR / f"{base}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Pullback Playbook Filter Backtest",
        "",
        f"- Generated: {payload['generated_at']}",
        f"- Universes: {', '.join(payload['universes'])}",
        f"- Horizon: close[T] -> open[T+{payload['horizon_days']}]",
        f"- Primary metric: net beta-adjusted alpha after equity round-trip cost",
        "",
        "## Fixed-Horizon Filters",
        "",
        "| Filter | n | Symbols | Mean beta alpha | Hit | CI95 | Own-baseline excess | Verdict |",
        "|---|---:|---:|---:|---:|---|---:|---|",
    ]
    for row in payload["filter_results"]:
        ci = f"[{row.get('ci_low')}, {row.get('ci_high')}]" if row.get("ci_low") is not None else "--"
        lines.append(
            f"| {row['label']} | {row.get('n', 0)} | {row.get('symbols', 0)} | "
            f"{_safe_pct(row.get('mean_beta_alpha'))} | {float(row.get('alpha_win_rate') or 0):.1%} | "
            f"{ci} | {_safe_pct(row.get('mean_own_excess'))} | "
            f"{'PASS' if row.get('significant') else 'not passed'} |"
        )
    lines += [
        "",
        "## ATR / Structure Exit",
        "",
        "| Group | n | Mean beta alpha | Hit | Target | Stop | CI95 | Verdict |",
        "|---|---:|---:|---:|---:|---:|---|---|",
    ]
    for row in payload["path_results"]:
        ci = f"[{row.get('ci_low')}, {row.get('ci_high')}]" if row.get("ci_low") is not None else "--"
        lines.append(
            f"| {row['label']} | {row.get('n', 0)} | {_safe_pct(row.get('mean_beta_alpha'))} | "
            f"{float(row.get('alpha_win_rate') or 0):.1%} | {float(row.get('target_rate') or 0):.1%} | "
            f"{float(row.get('stop_rate') or 0):.1%} | {ci} | "
            f"{'PASS' if row.get('significant') else 'not passed'} |"
        )
    lines += [
        "",
        "## Notes",
        "",
        "- This is a research validation report, not trading advice.",
        "- Filters are evaluated on historical bars with no future data in signal construction.",
        "- Universe membership is archive-only where available; remaining bias depends on the local snapshots.",
        "- Do not add a filter unless it improves the baseline and keeps CI above zero with enough samples.",
    ]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest pullback playbook filters over the existing pullback_hv edge.")
    parser.add_argument("--universes", default=DEFAULT_UNIVERSES)
    parser.add_argument("--period", default="5y")
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON)
    parser.add_argument("--max-symbols-per-universe", type=int, default=180)
    parser.add_argument("--top-quantile", type=float, default=0.70)
    parser.add_argument("--min-events", type=int, default=80)
    parser.add_argument("--output-prefix", default="")
    args = parser.parse_args()

    universes = [item.strip().lower() for item in args.universes.split(",") if item.strip()]
    symbols, sources = _universe_symbols(universes, args.max_symbols_per_universe)
    spy, spy_source = get_daily_history("SPY", period=args.period)
    spy = spy.copy()
    benchmark_cache: dict[str, pd.DataFrame] = {"SPY": spy}
    events: list[dict[str, Any]] = []
    symbol_reports: list[dict[str, Any]] = []
    rt_cost = cost_model.equity_round_trip_cost()

    print(
        f"universes={universes} symbols={len(symbols)} period={args.period} horizon={args.horizon} cost={rt_cost:.6f}",
        flush=True,
    )
    for symbol in symbols:
        source_universes = sources.get(symbol) or []
        benchmark, benchmark_source = benchmark_for_universes(source_universes)
        if benchmark not in benchmark_cache:
            benchmark_cache[benchmark], _ = get_daily_history(benchmark, period=args.period)
        try:
            frame, data_source = get_daily_history(symbol, period=args.period)
        except Exception as exc:
            symbol_reports.append({"symbol": symbol, "available": False, "reason": str(exc)[:120]})
            continue
        bench = benchmark_cache.get(benchmark, pd.DataFrame())
        if frame.empty or bench.empty or spy.empty:
            symbol_reports.append({"symbol": symbol, "available": False, "reason": "missing_history"})
            continue
        frame, bench = _align(frame, bench)
        frame, spy_aligned = _align(frame, spy)
        frame, bench = _align(frame, bench)
        if len(frame) < 260 + args.horizon or len(bench) != len(frame):
            symbol_reports.append({"symbol": symbol, "available": False, "reason": "insufficient_aligned_history"})
            continue
        raw_events = _collect_symbol_events(
            symbol,
            frame,
            args.horizon,
            min_launch_score=0.0,
            min_tunnel_score=0.0,
            cooldown_days=5,
        )
        date_to_idx = {idx.strftime("%Y-%m-%d"): pos for pos, idx in enumerate(frame.index)}
        added = 0
        for event in raw_events:
            score = _num(event.get("pullback_hv_score"))
            idx = date_to_idx.get(str(event.get("date")))
            if score is None or idx is None:
                continue
            feat = _event_features(symbol, frame, bench, spy_aligned, idx, args.horizon)
            if not feat:
                continue
            forward = _num(event.get("forward_return"))
            own_excess = _num(event.get("excess_return"))
            if forward is None or own_excess is None:
                continue
            row = {
                **event,
                **feat,
                "source_universes": source_universes,
                "benchmark": benchmark,
                "data_source": data_source,
                "net_own_excess": own_excess - rt_cost,
                "net_beta_alpha": forward - rt_cost - float(feat["beta"]) * float(feat["benchmark_return"]),
            }
            path = _simulate_atr_trade(
                frame,
                bench,
                idx,
                beta=float(feat["beta"]),
                atr_mult=1.5,
                horizon=args.horizon,
                reward_risk=2.0,
            )
            if path:
                row["path_net_beta_alpha"] = path["net_beta_alpha"]
                row["path_net_return"] = path["net_return"]
                row["path_exit_reason"] = path["exit_reason"]
                row["path_hold_days"] = path["hold_days"]
                row["path_stop_pct"] = path["stop_pct"]
            events.append(row)
            added += 1
        symbol_reports.append({
            "symbol": symbol,
            "available": True,
            "events": added,
            "benchmark": benchmark,
            "source_universes": source_universes,
        })

    if not events:
        raise SystemExit("no events collected")
    scores = sorted(float(row["pullback_hv_score"]) for row in events)
    cutoff = scores[int(max(0.0, min(0.99, args.top_quantile)) * (len(scores) - 1))]
    top = [row for row in events if float(row["pullback_hv_score"]) >= cutoff]
    groups: list[tuple[str, list[dict[str, Any]]]] = [
        ("baseline: pullback_hv top bucket", top),
        ("+ market filter", [r for r in top if r.get("market_ok")]),
        ("+ liquidity filter", [r for r in top if r.get("liquid")]),
        ("+ market + liquidity", [r for r in top if r.get("market_ok") and r.get("liquid")]),
        ("+ trend filter", [r for r in top if r.get("trend_filter")]),
        ("+ relative strength", [r for r in top if r.get("rs_filter")]),
        ("+ trend + relative strength", [r for r in top if r.get("trend_filter") and r.get("rs_filter")]),
        ("+ normal pullback shape", [r for r in top if r.get("normal_pullback")]),
        ("+ long lower wick near MA", [r for r in top if r.get("aggressive_wick")]),
        ("+ close above prior high confirmation", [r for r in top if r.get("close_above_prev_high")]),
        ("full playbook strict", [
            r for r in top
            if r.get("market_ok") and r.get("liquid") and r.get("trend_filter") and r.get("rs_filter") and r.get("normal_pullback")
        ]),
        ("market + liquid + RS", [
            r for r in top if r.get("market_ok") and r.get("liquid") and r.get("rs_filter")
        ]),
    ]
    filter_results = [_summary(label, rows) for label, rows in groups]
    path_results = [
        _path_summary(label, rows)
        for label, rows in (
            ("path baseline: ATR1.5 stop / 2R target", top),
            ("path + market + liquidity", [r for r in top if r.get("market_ok") and r.get("liquid")]),
            ("path + market + liquid + RS", [r for r in top if r.get("market_ok") and r.get("liquid") and r.get("rs_filter")]),
            ("path full playbook strict", [
                r for r in top
                if r.get("market_ok") and r.get("liquid") and r.get("trend_filter") and r.get("rs_filter") and r.get("normal_pullback")
            ]),
        )
    ]
    payload = {
        "generated_at": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "method": "pullback_hv_incremental_playbook_filters",
        "universes": universes,
        "symbols_requested": len(symbols),
        "symbols_with_events": len({row["ticker"] for row in events}),
        "event_count_all": len(events),
        "event_count_top_bucket": len(top),
        "top_quantile": args.top_quantile,
        "top_cutoff": round(cutoff, 6),
        "horizon_days": args.horizon,
        "exit_convention": f"close[T] -> open[T+{args.horizon}]",
        "primary_metric": "net_beta_adjusted_alpha",
        "cost": {"equity_round_trip_cost": rt_cost, "spy_source": spy_source},
        "filter_results": filter_results,
        "path_results": path_results,
        "symbol_reports": symbol_reports,
        "events": events,
    }
    json_path, md_path = _write_reports(payload, args.output_prefix)
    print(f"events={len(events)} top={len(top)} symbols={payload['symbols_with_events']} json={json_path} md={md_path}", flush=True)
    print("=== FIXED-HORIZON RESULTS ===", flush=True)
    for row in filter_results:
        if row.get("n", 0) < args.min_events:
            note = "thin"
        elif row.get("significant"):
            note = "PASS"
        else:
            note = "not-pass"
        print(
            f"{row['label']:<42} n={row.get('n', 0):<5} beta_alpha={_safe_pct(row.get('mean_beta_alpha')):<9} "
            f"hit={float(row.get('alpha_win_rate') or 0):.1%} ci=[{row.get('ci_low')},{row.get('ci_high')}] {note}",
            flush=True,
        )
    print("=== PATH RESULTS ===", flush=True)
    for row in path_results:
        print(
            f"{row['label']:<42} n={row.get('n', 0):<5} beta_alpha={_safe_pct(row.get('mean_beta_alpha')):<9} "
            f"hit={float(row.get('alpha_win_rate') or 0):.1%} target={float(row.get('target_rate') or 0):.1%} "
            f"stop={float(row.get('stop_rate') or 0):.1%} ci=[{row.get('ci_low')},{row.get('ci_high')}]",
            flush=True,
        )


if __name__ == "__main__":
    main()
