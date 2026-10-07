#!/usr/bin/env python3
"""Extended playbook experiments requested after the pullback_hv validation.

Experiments:
1. Strong market -> strong sector ETF -> strong stock vs sector.
2. Fixed 5% stop vs structure + 1.5ATR / 2.0ATR stops.
3. Standalone platform-breakout events.
4. Earnings T-5 hard-filter feasibility / coverage audit.

The script is research-only and does not modify live ranking.
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
from market_data_service import get_daily_history, get_ticker_reference, get_point_in_time_earnings, point_in_time_earnings_configured  # noqa: E402
from overnight_alpha_service import benchmark_for_universes  # noqa: E402


RUNS_DIR = AGENT / "runs"
SECTOR_ETFS = {
    "technology": "XLK",
    "software": "XLK",
    "computer": "XLK",
    "semiconductor": "SOXX",
    "electronic": "XLK",
    "communication": "XLC",
    "telecom": "XLC",
    "media": "XLC",
    "entertainment": "XLC",
    "financial": "XLF",
    "bank": "XLF",
    "insurance": "XLF",
    "broker": "XLF",
    "health": "XLV",
    "medical": "XLV",
    "pharma": "XLV",
    "biotech": "XBI",
    "consumer staples": "XLP",
    "food": "XLP",
    "beverage": "XLP",
    "retail": "XLY",
    "restaurant": "XLY",
    "auto": "XLY",
    "apparel": "XLY",
    "industrial": "XLI",
    "aerospace": "XLI",
    "machinery": "XLI",
    "transport": "XLI",
    "energy": "XLE",
    "oil": "XLE",
    "gas": "XLE",
    "materials": "XLB",
    "chemical": "XLB",
    "metal": "XLB",
    "mining": "XLB",
    "utility": "XLU",
    "utilities": "XLU",
    "real estate": "XLRE",
    "reit": "XLRE",
}


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _pct(value: Any) -> str:
    out = _num(value)
    return "--" if out is None else f"{out * 100:+.3f}%"


def _cluster_ci(rows: list[dict[str, Any]], key: str, n_boot: int = 2000, seed: int = 42) -> tuple[float | None, float | None]:
    by: dict[str, list[float]] = {}
    for row in rows:
        value = _num(row.get(key))
        if value is not None:
            symbol = str(row.get("ticker") or row.get("symbol") or "")
            if symbol:
                by.setdefault(symbol, []).append(value)
    clusters = [c for c, vals in by.items() if vals]
    if len(clusters) < 2:
        return None, None
    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(n_boot):
        sample = [rng.choice(clusters) for _ in clusters]
        values = [v for c in sample for v in by[c]]
        if values:
            estimates.append(sum(values) / len(values))
    estimates.sort()
    return estimates[int(0.025 * (len(estimates) - 1))], estimates[int(0.975 * (len(estimates) - 1))]


def _summary(label: str, rows: list[dict[str, Any]], key: str = "net_beta_alpha") -> dict[str, Any]:
    vals = [_num(r.get(key)) for r in rows]
    vals = [v for v in vals if v is not None]
    if not vals:
        return {"label": label, "n": 0, "symbols": 0}
    lo, hi = _cluster_ci(rows, key)
    return {
        "label": label,
        "n": len(vals),
        "symbols": len({str(r.get("ticker") or r.get("symbol")) for r in rows}),
        "mean": round(mean(vals), 6),
        "median": round(median(vals), 6),
        "win_rate": round(sum(v > 0 for v in vals) / len(vals), 4),
        "ci_low": round(lo, 6) if lo is not None else None,
        "ci_high": round(hi, 6) if hi is not None else None,
        "significant": bool(lo is not None and lo > 0),
    }


def _align(a: pd.DataFrame, b: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    common = a.index.intersection(b.index).sort_values()
    return a.loc[common].copy(), b.loc[common].copy()


def _ret(close: pd.Series, idx: int, lookback: int) -> float | None:
    if idx - lookback < 0:
        return None
    now = _num(close.iloc[idx])
    old = _num(close.iloc[idx - lookback])
    if not now or not old or old <= 0:
        return None
    return now / old - 1.0


def _sector_etf(symbol: str, fallback: str = "SPY") -> tuple[str, str]:
    ref = get_ticker_reference(symbol) or {}
    text = f"{ref.get('sic_description') or ''} {ref.get('name') or ''}".lower()
    for key, etf in SECTOR_ETFS.items():
        if key in text:
            return etf, text[:120]
    return fallback, text[:120]


def _atr(frame: pd.DataFrame, window: int = 14) -> pd.Series:
    high = pd.to_numeric(frame["High"], errors="coerce")
    low = pd.to_numeric(frame["Low"], errors="coerce")
    close = pd.to_numeric(frame["Close"], errors="coerce")
    prev = close.shift(1)
    tr = pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(window).mean()


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


def _simulate_stop(
    stock: pd.DataFrame,
    bench: pd.DataFrame,
    idx: int,
    *,
    mode: str,
    horizon: int,
    reward_risk: float,
    beta: float,
) -> dict[str, Any] | None:
    close = pd.to_numeric(stock["Close"], errors="coerce")
    low = pd.to_numeric(stock["Low"], errors="coerce")
    entry = _num(close.iloc[idx])
    if not entry or idx + horizon >= len(stock):
        return None
    if mode == "fixed5":
        risk = entry * 0.05
    else:
        mult = 1.5 if mode == "atr15" else 2.0
        atr_value = _num(_atr(stock).iloc[idx])
        key_low = _num(low.iloc[max(0, idx - 10): idx + 1].min(), entry)
        if not atr_value or atr_value <= 0:
            return None
        risk = max(entry - float(key_low), mult * atr_value)
    if risk <= 0:
        return None
    stop = max(0.01, entry - risk)
    target = entry + reward_risk * risk
    exit_idx = idx + horizon
    exit_price = _num(stock["Open"].iloc[exit_idx], _num(stock["Close"].iloc[exit_idx]))
    reason = "time_exit"
    for j in range(idx + 1, idx + horizon + 1):
        bar = _bar(stock, j)
        if not bar:
            continue
        if bar["low"] <= stop:
            exit_idx = j
            exit_price = min(bar["open"], stop) if bar["open"] < stop else stop
            reason = "stop"
            break
        if bar["high"] >= target:
            exit_idx = j
            exit_price = target
            reason = "target"
            break
    bench_entry = _num(bench["Close"].iloc[idx])
    bench_exit = _num(bench["Open"].iloc[exit_idx], _num(bench["Close"].iloc[exit_idx]))
    if not exit_price or not bench_entry or not bench_exit:
        return None
    ret = exit_price / entry - 1.0 - cost_model.equity_round_trip_cost(price=entry)
    bench_ret = bench_exit / bench_entry - 1.0
    return {
        "net_beta_alpha": ret - beta * bench_ret,
        "net_return": ret,
        "exit_reason": reason,
        "stop_pct": round(risk / entry, 5),
        "hold_days": exit_idx - idx,
    }


def _platform_events(
    symbol: str,
    source_universes: list[str],
    stock: pd.DataFrame,
    bench: pd.DataFrame,
    *,
    horizon: int,
) -> list[dict[str, Any]]:
    if stock.empty or bench.empty:
        return []
    stock, bench = _align(stock, bench)
    close = pd.to_numeric(stock["Close"], errors="coerce")
    open_ = pd.to_numeric(stock["Open"], errors="coerce")
    high = pd.to_numeric(stock["High"], errors="coerce")
    low = pd.to_numeric(stock["Low"], errors="coerce")
    volume = pd.to_numeric(stock.get("Volume", pd.Series(0, index=stock.index)), errors="coerce").fillna(0)
    bclose = pd.to_numeric(bench["Close"], errors="coerce")
    bopen = pd.to_numeric(bench["Open"], errors="coerce")
    ma20 = close.rolling(20).mean()
    ma50 = close.rolling(50).mean()
    ma200 = close.rolling(200).mean()
    vol20 = volume.rolling(20).mean()
    events: list[dict[str, Any]] = []
    next_allowed = 0
    for idx in range(220, len(stock) - horizon):
        if idx < next_allowed:
            continue
        window = 15
        start = idx - window
        phigh = float(high.iloc[start:idx].max())
        plow = float(low.iloc[start:idx].min())
        price = _num(close.iloc[idx])
        if not price or phigh <= 0 or plow <= 0:
            continue
        platform_range = (phigh - plow) / price
        if platform_range > 0.12:
            continue
        prior_range = (float(high.iloc[idx - 35:idx - 15].max()) - float(low.iloc[idx - 35:idx - 15].min())) / price
        contraction = platform_range <= max(0.04, prior_range * 0.75)
        breakout = close.iloc[idx] > phigh * 1.002 and close.iloc[idx] > open_.iloc[idx]
        volume_active = volume.iloc[idx] >= vol20.iloc[idx] * 1.20
        trend_ok = close.iloc[idx] > ma20.iloc[idx] and close.iloc[idx] > ma50.iloc[idx] and ma50.iloc[idx] > ma200.iloc[idx]
        not_extended = close.iloc[idx] / ma20.iloc[idx] - 1.0 <= 0.12
        if not (contraction and breakout and volume_active and trend_ok and not_extended):
            continue
        entry = _num(close.iloc[idx])
        exitp = _num(open_.iloc[idx + horizon], _num(close.iloc[idx + horizon]))
        bench_entry = _num(bclose.iloc[idx])
        bench_exit = _num(bopen.iloc[idx + horizon], _num(bclose.iloc[idx + horizon]))
        if not entry or not exitp or not bench_entry or not bench_exit:
            continue
        beta = _beta(stock, bench, idx)
        ret = exitp / entry - 1.0 - cost_model.equity_round_trip_cost(price=entry)
        bench_ret = bench_exit / bench_entry - 1.0
        events.append({
            "ticker": symbol,
            "date": stock.index[idx].strftime("%Y-%m-%d"),
            "source_universes": source_universes,
            "platform_range": round(platform_range, 5),
            "prior_range": round(prior_range, 5),
            "volume_ratio": round(float(volume.iloc[idx] / vol20.iloc[idx]), 4),
            "net_beta_alpha": ret - beta * bench_ret,
            "net_return": ret,
            "beta": beta,
        })
        next_allowed = idx + 8
    return events


def _exit_table(rows: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        reason = str(row.get("exit_reason") or "unknown")
        out[reason] = out.get(reason, 0) + 1
    return out


def _experiment_three_stage(top: list[dict[str, Any]], histories: dict[str, pd.DataFrame], etf_histories: dict[str, pd.DataFrame]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    enriched: list[dict[str, Any]] = []
    for row in top:
        sym = str(row.get("ticker") or "")
        stock = histories.get(sym, pd.DataFrame())
        if stock.empty:
            continue
        fallback = str(row.get("benchmark") or "SPY")
        etf, desc = _sector_etf(sym, fallback=fallback)
        sector = etf_histories.get(etf, pd.DataFrame())
        spy = etf_histories.get("SPY", pd.DataFrame())
        if sector.empty or spy.empty:
            continue
        stock, sector = _align(stock, sector)
        stock, spy2 = _align(stock, spy)
        date = pd.Timestamp(row.get("date"))
        if date not in stock.index or date not in sector.index or date not in spy2.index:
            continue
        i = int(stock.index.get_loc(date))
        si = int(sector.index.get_loc(date))
        spi = int(spy2.index.get_loc(date))
        sclose = pd.to_numeric(stock["Close"], errors="coerce")
        eclose = pd.to_numeric(sector["Close"], errors="coerce")
        spyc = pd.to_numeric(spy2["Close"], errors="coerce")
        sector_3m = _ret(eclose, si, 63)
        sector_6m = _ret(eclose, si, 126)
        spy_3m = _ret(spyc, spi, 63)
        spy_6m = _ret(spyc, spi, 126)
        stock_3m = _ret(sclose, i, 63)
        stock_6m = _ret(sclose, i, 126)
        if None in (sector_3m, sector_6m, spy_3m, spy_6m, stock_3m, stock_6m):
            continue
        out = {
            **row,
            "sector_etf": etf,
            "sector_desc": desc,
            "sector_strong": bool(sector_3m > spy_3m and sector_6m > spy_6m),
            "stock_vs_sector_strong": bool(stock_3m > sector_3m and stock_6m > sector_6m * 0.85),
            "sector_3m_vs_spy": round(float(sector_3m - spy_3m), 5),
            "sector_6m_vs_spy": round(float(sector_6m - spy_6m), 5),
            "stock_3m_vs_sector": round(float(stock_3m - sector_3m), 5),
            "stock_6m_vs_sector": round(float(stock_6m - sector_6m), 5),
        }
        enriched.append(out)
    groups = [
        _summary("baseline top bucket", top),
        _summary("market strong only", [r for r in enriched if r.get("market_ok")]),
        _summary("sector strong only", [r for r in enriched if r.get("sector_strong")]),
        _summary("stock stronger than sector only", [r for r in enriched if r.get("stock_vs_sector_strong")]),
        _summary("market + sector", [r for r in enriched if r.get("market_ok") and r.get("sector_strong")]),
        _summary("sector + stock", [r for r in enriched if r.get("sector_strong") and r.get("stock_vs_sector_strong")]),
        _summary("market + sector + stock", [r for r in enriched if r.get("market_ok") and r.get("sector_strong") and r.get("stock_vs_sector_strong")]),
    ]
    return groups, enriched


def _experiment_atr(top: list[dict[str, Any]], histories: dict[str, pd.DataFrame], bench_histories: dict[str, pd.DataFrame], horizon: int) -> list[dict[str, Any]]:
    modes = ["fixed5", "atr15", "atr20"]
    rows_by_mode: dict[str, list[dict[str, Any]]] = {m: [] for m in modes}
    for row in top:
        sym = str(row.get("ticker") or "")
        stock = histories.get(sym, pd.DataFrame())
        bench = bench_histories.get(str(row.get("benchmark") or "SPY"), pd.DataFrame())
        if stock.empty or bench.empty:
            continue
        stock, bench = _align(stock, bench)
        date = pd.Timestamp(row.get("date"))
        if date not in stock.index:
            continue
        idx = int(stock.index.get_loc(date))
        beta = float(row.get("beta") or _beta(stock, bench, idx))
        for mode in modes:
            result = _simulate_stop(stock, bench, idx, mode=mode, horizon=horizon, reward_risk=2.0, beta=beta)
            if result:
                rows_by_mode[mode].append({"ticker": sym, "date": row.get("date"), **result})
    out = []
    for mode, rows in rows_by_mode.items():
        item = _summary(mode, rows)
        exits = _exit_table(rows)
        item["exit_reasons"] = exits
        item["stop_rate"] = round(exits.get("stop", 0) / len(rows), 4) if rows else 0
        item["target_rate"] = round(exits.get("target", 0) / len(rows), 4) if rows else 0
        item["avg_stop_pct"] = round(mean([float(r["stop_pct"]) for r in rows]), 5) if rows else None
        out.append(item)
    return out


def _experiment_platform(symbols: list[str], sources: dict[str, list[str]], histories: dict[str, pd.DataFrame], bench_histories: dict[str, pd.DataFrame], horizon: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    for sym in symbols:
        source_universes = sources.get(sym) or []
        bench, _ = benchmark_for_universes(source_universes)
        evs = _platform_events(sym, source_universes, histories.get(sym, pd.DataFrame()), bench_histories.get(bench, pd.DataFrame()), horizon=horizon)
        rows.extend(evs)
    return [_summary("standalone platform breakout", rows)], rows


def _experiment_earnings(top: list[dict[str, Any]]) -> dict[str, Any]:
    pit = point_in_time_earnings_configured()
    checked = 0
    available = 0
    near = 0
    samples: list[dict[str, Any]] = []
    if pit:
        for row in top[:2000]:
            checked += 1
            try:
                rec = get_point_in_time_earnings(str(row.get("ticker")), str(row.get("date")))
            except Exception:
                rec = None
            if not rec:
                continue
            available += 1
            days = _num(rec.get("trading_days_to_next_report") or rec.get("days_to_next_report"))
            if days is not None and 0 <= days <= 5:
                near += 1
                samples.append({"ticker": row.get("ticker"), "date": row.get("date"), "days_to_report": days})
    return {
        "pit_earnings_configured": pit,
        "checked_sample": checked,
        "available_records": available,
        "near_earnings_records": near,
        "coverage": round(available / checked, 4) if checked else 0,
        "verdict": (
            "PIT earnings is configured; use coverage above before trusting T-5 filtering."
            if pit else
            "Cannot run a clean historical T-5 earnings filter: point-in-time earnings calendar is not configured. Current free/latest earnings feeds would leak look-ahead or cover only recent reports."
        ),
        "samples": samples[:20],
    }


def _md(payload: dict[str, Any]) -> str:
    lines = [
        "# Extended Pullback Playbook Experiments",
        "",
        f"- Generated: {payload['generated_at']}",
        f"- Input: `{payload['input_json']}`",
        f"- Horizon: close[T] -> open[T+{payload['horizon_days']}]",
        "",
        "## Three-stage Relative Strength",
        "",
        "| Group | n | Symbols | Mean beta alpha | Hit | CI95 | Verdict |",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    for row in payload["three_stage_results"]:
        ci = f"[{row.get('ci_low')}, {row.get('ci_high')}]" if row.get("ci_low") is not None else "--"
        lines.append(f"| {row['label']} | {row.get('n', 0)} | {row.get('symbols', 0)} | {_pct(row.get('mean'))} | {float(row.get('win_rate') or 0):.1%} | {ci} | {'PASS' if row.get('significant') else 'not passed'} |")
    lines += [
        "",
        "## ATR / Structure Stops",
        "",
        "| Mode | n | Mean beta alpha | Hit | Stop | Target | Avg stop | CI95 | Verdict |",
        "|---|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for row in payload["atr_results"]:
        ci = f"[{row.get('ci_low')}, {row.get('ci_high')}]" if row.get("ci_low") is not None else "--"
        lines.append(f"| {row['label']} | {row.get('n', 0)} | {_pct(row.get('mean'))} | {float(row.get('win_rate') or 0):.1%} | {float(row.get('stop_rate') or 0):.1%} | {float(row.get('target_rate') or 0):.1%} | {_pct(row.get('avg_stop_pct'))} | {ci} | {'PASS' if row.get('significant') else 'not passed'} |")
    lines += [
        "",
        "## Standalone Platform Breakout",
        "",
        "| Group | n | Symbols | Mean beta alpha | Hit | CI95 | Verdict |",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    for row in payload["platform_results"]:
        ci = f"[{row.get('ci_low')}, {row.get('ci_high')}]" if row.get("ci_low") is not None else "--"
        lines.append(f"| {row['label']} | {row.get('n', 0)} | {row.get('symbols', 0)} | {_pct(row.get('mean'))} | {float(row.get('win_rate') or 0):.1%} | {ci} | {'PASS' if row.get('significant') else 'not passed'} |")
    e = payload["earnings_filter"]
    lines += [
        "",
        "## Earnings T-5 Filter",
        "",
        f"- PIT earnings configured: {e.get('pit_earnings_configured')}",
        f"- Checked sample: {e.get('checked_sample')}",
        f"- Available records: {e.get('available_records')}",
        f"- Coverage: {float(e.get('coverage') or 0):.1%}",
        f"- Verdict: {e.get('verdict')}",
        "",
        "## Product Verdict",
        "",
        payload["verdict"],
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="agent/runs/pullback_playbook_spx_ndx_sox_260625.json")
    ap.add_argument("--output-prefix", default="playbook_extended_experiments_260625")
    ap.add_argument("--horizon", type=int, default=8)
    args = ap.parse_args()
    source = Path(args.input)
    data = json.loads(source.read_text(encoding="utf-8"))
    rows = list(data.get("events") or [])
    cutoff = _num(data.get("top_cutoff"), 0.0) or 0.0
    top = [r for r in rows if (_num(r.get("pullback_hv_score"), 0.0) or 0.0) >= cutoff]
    symbols = sorted({str(r.get("ticker")) for r in rows if r.get("ticker")})
    sources: dict[str, list[str]] = {}
    for r in rows:
        sources.setdefault(str(r.get("ticker")), list(r.get("source_universes") or []))

    benchmarks = sorted({str(r.get("benchmark") or "SPY") for r in rows} | {"SPY", "QQQ", "SOXX", "XLK", "XLF", "XLV", "XLY", "XLP", "XLI", "XLE", "XLB", "XLU", "XLRE", "XLC", "XBI"})
    print(f"loading histories: symbols={len(symbols)} benchmarks/etfs={len(benchmarks)} top={len(top)}", flush=True)
    histories: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        frame, _ = get_daily_history(sym, period="5y")
        if not frame.empty:
            histories[sym] = frame
    bench_histories: dict[str, pd.DataFrame] = {}
    for sym in benchmarks:
        frame, _ = get_daily_history(sym, period="5y")
        if not frame.empty:
            bench_histories[sym] = frame

    three_stage_results, three_stage_events = _experiment_three_stage(top, histories, bench_histories)
    atr_results = _experiment_atr(top, histories, bench_histories, args.horizon)
    platform_results, platform_events = _experiment_platform(symbols, sources, histories, bench_histories, args.horizon)
    earnings_filter = _experiment_earnings(top)
    verdict = (
        "Three-stage RS should only be considered if market+sector+stock beats the prior market+liquid+RS filter with CI>0. "
        "ATR modes should not replace current risk rules unless they improve beta alpha and reduce stop/tail losses. "
        "Platform breakout is a separate strategy line and must not be mixed into pullback_hv unless it passes on its own. "
        "Earnings T-5 filtering requires point-in-time earnings coverage; without it, do not claim a historical improvement."
    )
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "input_json": str(source),
        "horizon_days": args.horizon,
        "top_event_count": len(top),
        "history_symbol_count": len(histories),
        "three_stage_results": three_stage_results,
        "three_stage_event_count": len(three_stage_events),
        "atr_results": atr_results,
        "platform_results": platform_results,
        "platform_event_count": len(platform_events),
        "earnings_filter": earnings_filter,
        "platform_events_sample": platform_events[:200],
        "verdict": verdict,
    }
    out_json = source.parent / f"{args.output_prefix}.json"
    out_md = source.parent / f"{args.output_prefix}.md"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(_md(payload), encoding="utf-8")
    print(f"json={out_json} md={out_md}", flush=True)
    print("=== THREE STAGE ===", flush=True)
    for row in three_stage_results:
        print(f"{row['label']:<36} n={row.get('n',0):<6} mean={_pct(row.get('mean')):<9} hit={float(row.get('win_rate') or 0):.1%} ci=[{row.get('ci_low')},{row.get('ci_high')}] sig={row.get('significant')}", flush=True)
    print("=== ATR ===", flush=True)
    for row in atr_results:
        print(f"{row['label']:<8} n={row.get('n',0):<6} mean={_pct(row.get('mean')):<9} hit={float(row.get('win_rate') or 0):.1%} stop={float(row.get('stop_rate') or 0):.1%} target={float(row.get('target_rate') or 0):.1%} ci=[{row.get('ci_low')},{row.get('ci_high')}] sig={row.get('significant')}", flush=True)
    print("=== PLATFORM ===", flush=True)
    for row in platform_results:
        print(f"{row['label']:<36} n={row.get('n',0):<6} mean={_pct(row.get('mean')):<9} hit={float(row.get('win_rate') or 0):.1%} ci=[{row.get('ci_low')},{row.get('ci_high')}] sig={row.get('significant')}", flush=True)
    print("=== EARNINGS ===", flush=True)
    print(earnings_filter.get("verdict"), flush=True)


if __name__ == "__main__":
    main()
