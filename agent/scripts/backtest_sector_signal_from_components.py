#!/usr/bin/env python3
"""Backtest sector-level signals aggregated from component-stock signals.

The hypothesis is not strict causality.  It is a lead/nowcast question:
when many stocks in the same sector/track fire positive component signals on
the same day, does the sector ETF have better forward absolute return or
relative return versus SPY/QQQ?

Input defaults to the already sector-enriched playbook events so we avoid
rebuilding the expensive component signal scan.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import random
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Any

import pandas as pd

HERE = Path(__file__).resolve().parent
AGENT = HERE.parent
if os.environ.get("VIBE_MARKET_DATA_CACHE_DIR", "").startswith("/app/"):
    os.environ["VIBE_MARKET_DATA_CACHE_DIR"] = str(AGENT / "data_cache" / "market_data")
elif not os.environ.get("VIBE_MARKET_DATA_CACHE_DIR"):
    os.environ["VIBE_MARKET_DATA_CACHE_DIR"] = str(AGENT / "data_cache" / "market_data")
for path in (AGENT, HERE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import cost_model  # noqa: E402
from market_data_service import get_daily_history  # noqa: E402


RUNS_DIR = AGENT / "runs"
VALID_SECTOR_ETFS = {
    "QQQ", "SOXX", "XLK", "XLF", "XLV", "XLY", "XLP", "XLI",
    "XLE", "XLB", "XLU", "XLRE", "XLC", "XBI", "SPY",
}


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _pct(value: Any) -> str:
    out = _num(value)
    return "--" if out is None else f"{out * 100:+.2f}%"


def _history(symbol: str, memo: dict[str, pd.DataFrame], period: str) -> pd.DataFrame:
    sym = str(symbol or "").upper()
    if not sym:
        return pd.DataFrame()
    if sym not in memo:
        try:
            frame, _source = get_daily_history(sym, period=period, allow_yfinance_fallback=True)
        except Exception:
            frame = pd.DataFrame()
        memo[sym] = frame if frame is not None else pd.DataFrame()
    return memo[sym]


def _forward_return(frame: pd.DataFrame, date: str, horizon: int) -> float | None:
    if frame is None or frame.empty or not {"Close", "Open"}.issubset(frame.columns):
        return None
    day = pd.Timestamp(date)
    if day not in frame.index:
        return None
    idx = int(frame.index.get_loc(day))
    if idx + horizon >= len(frame):
        return None
    entry = _num(frame["Close"].iloc[idx])
    exitp = _num(frame["Open"].iloc[idx + horizon], _num(frame["Close"].iloc[idx + horizon]))
    if not entry or not exitp or entry <= 0 or exitp <= 0:
        return None
    return exitp / entry - 1.0


def _cluster_ci(rows: list[dict[str, Any]], key: str, cluster_key: str = "date", n_boot: int = 1200, seed: int = 42) -> tuple[float | None, float | None]:
    clusters: dict[str, list[float]] = {}
    for row in rows:
        value = _num(row.get(key))
        cluster = str(row.get(cluster_key) or "")
        if value is not None and cluster:
            clusters.setdefault(cluster, []).append(value)
    names = sorted(clusters)
    if len(names) < 8:
        return None, None
    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(n_boot):
        sample = [rng.choice(names) for _ in names]
        vals = [v for name in sample for v in clusters[name]]
        if vals:
            estimates.append(mean(vals))
    estimates.sort()
    return (
        round(estimates[int(0.025 * (len(estimates) - 1))], 6),
        round(estimates[int(0.975 * (len(estimates) - 1))], 6),
    )


def _summarize(label: str, rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    vals = [_num(row.get(key)) for row in rows]
    vals = [v for v in vals if v is not None]
    if not vals:
        return {"label": label, "n": 0, "sectors": 0, "dates": 0}
    lo, hi = _cluster_ci(rows, key)
    return {
        "label": label,
        "n": len(vals),
        "sectors": len({row.get("sector_etf") for row in rows}),
        "dates": len({row.get("date") for row in rows}),
        "mean": round(mean(vals), 6),
        "median": round(median(vals), 6),
        "win_rate": round(sum(v > 0 for v in vals) / len(vals), 4),
        "ci_low": lo,
        "ci_high": hi,
        "significant": bool(lo is not None and lo > 0),
    }


def _bucket_rows(rows: list[dict[str, Any]], score_key: str, metric_key: str, buckets: int = 5) -> list[dict[str, Any]]:
    valid = [row for row in rows if _num(row.get(score_key)) is not None and _num(row.get(metric_key)) is not None]
    valid.sort(key=lambda row: float(row[score_key]))
    if not valid:
        return []
    out = []
    n = len(valid)
    for i in range(buckets):
        lo_i = int(round(i * n / buckets))
        hi_i = int(round((i + 1) * n / buckets))
        group = valid[lo_i:hi_i]
        if not group:
            continue
        summary = _summarize(f"B{i + 1}", group, metric_key)
        out.append({
            **summary,
            "bucket": i + 1,
            "score_range": [round(float(group[0][score_key]), 6), round(float(group[-1][score_key]), 6)],
        })
    return out


def _winsor_score(value: Any) -> float:
    return max(0.0, min(1.0, _num(value, 0.0) or 0.0))


def _aggregate_events(events: list[dict[str, Any]], min_components: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    sector_members: dict[str, set[str]] = defaultdict(set)
    for row in events:
        etf = str(row.get("sector_etf") or "").upper()
        ticker = str(row.get("ticker") or row.get("symbol") or "").upper()
        if etf in VALID_SECTOR_ETFS and ticker:
            sector_members[etf].add(ticker)

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in events:
        etf = str(row.get("sector_etf") or "").upper()
        date = str(row.get("date") or "")
        if etf not in VALID_SECTOR_ETFS or not date:
            continue
        grouped[(date, etf)].append(row)

    out: list[dict[str, Any]] = []
    skipped = {"below_min_components": 0}
    for (date, etf), rows in grouped.items():
        tickers = sorted({str(r.get("ticker") or r.get("symbol") or "").upper() for r in rows if r.get("ticker") or r.get("symbol")})
        if len(tickers) < min_components:
            skipped["below_min_components"] += 1
            continue
        size = max(1, len(sector_members.get(etf) or []))
        scores = [_winsor_score(r.get("pullback_hv_score")) for r in rows]
        advs = [max(0.0, _num(r.get("avg_dollar_volume"), 0.0) or 0.0) for r in rows]
        total_adv = sum(advs)
        weighted = sum(s * w for s, w in zip(scores, advs)) / total_adv if total_adv > 0 else mean(scores)
        top_weight_share = max(advs) / total_adv if total_adv > 0 and advs else 1.0
        breadth = min(1.0, len(tickers) / size)
        market_liquid_rs = sum(bool(r.get("market_ok") and r.get("liquid") and r.get("rs_filter")) for r in rows) / len(rows)
        stock_vs_sector = sum(bool(r.get("stock_vs_sector_strong")) for r in rows) / len(rows)
        sector_strong = sum(bool(r.get("sector_strong")) for r in rows) / len(rows)
        volume_dry = sum(bool(r.get("volume_dry")) for r in rows) / len(rows)
        concentration_penalty = min(0.18, max(0.0, top_weight_share - 0.35) * 0.35)
        signal_score = (
            0.32 * breadth
            + 0.28 * weighted
            + 0.18 * median(scores)
            + 0.10 * market_liquid_rs
            + 0.07 * stock_vs_sector
            + 0.05 * sector_strong
            + 0.03 * volume_dry
            - concentration_penalty
        )
        out.append({
            "date": date,
            "sector_etf": etf,
            "component_count": len(tickers),
            "sector_size": size,
            "signal_breadth": round(breadth, 6),
            "avg_component_score": round(mean(scores), 6),
            "median_component_score": round(median(scores), 6),
            "weighted_component_score": round(weighted, 6),
            "market_liquid_rs_ratio": round(market_liquid_rs, 6),
            "stock_vs_sector_ratio": round(stock_vs_sector, 6),
            "sector_strong_ratio": round(sector_strong, 6),
            "volume_dry_ratio": round(volume_dry, 6),
            "top_weight_share": round(top_weight_share, 6),
            "sector_signal_score": round(max(0.0, min(1.0, signal_score)), 6),
            "components": tickers[:20],
        })
    return out, skipped


def _attach_dynamic_features(rows: list[dict[str, Any]], lookback: int = 5) -> list[dict[str, Any]]:
    """Add sector signal acceleration / diffusion features.

    Static breadth answers "how many fired today"; diffusion answers "is this
    sector lighting up faster than it did recently?"  The latter is closer to a
    rotation/consensus signal and less likely to simply reward large sectors.
    """
    by_sector: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_sector[str(row.get("sector_etf") or "")].append(dict(row))

    out: list[dict[str, Any]] = []
    for etf, group in by_sector.items():
        group.sort(key=lambda r: str(r.get("date") or ""))
        for i, row in enumerate(group):
            prev = group[max(0, i - lookback):i]
            prev_score = mean([float(r.get("sector_signal_score") or 0.0) for r in prev]) if prev else float(row.get("sector_signal_score") or 0.0)
            prev_breadth = mean([float(r.get("signal_breadth") or 0.0) for r in prev]) if prev else float(row.get("signal_breadth") or 0.0)
            prev_components: set[str] = set()
            for r in prev:
                prev_components.update(str(x).upper() for x in (r.get("components") or []))
            components = {str(x).upper() for x in (row.get("components") or [])}
            fresh_ratio = (len(components - prev_components) / len(components)) if components else 0.0
            score_change = float(row.get("sector_signal_score") or 0.0) - prev_score
            breadth_change = float(row.get("signal_breadth") or 0.0) - prev_breadth
            # Scale small daily deltas into a 0..1 score.  Negative acceleration
            # is not shorted here; it simply lowers the long-rotation priority.
            acceleration = max(0.0, min(1.0, 0.50 + 3.0 * score_change + 4.0 * breadth_change))
            diffusion_score = (
                0.42 * acceleration
                + 0.28 * max(0.0, min(1.0, fresh_ratio))
                + 0.18 * float(row.get("stock_vs_sector_ratio") or 0.0)
                + 0.12 * float(row.get("market_liquid_rs_ratio") or 0.0)
            )
            item = dict(row)
            item.update({
                "prev_score_5d": round(prev_score, 6),
                "prev_breadth_5d": round(prev_breadth, 6),
                "score_change_5d": round(score_change, 6),
                "breadth_change_5d": round(breadth_change, 6),
                "fresh_component_ratio_5d": round(fresh_ratio, 6),
                "sector_diffusion_score": round(max(0.0, min(1.0, diffusion_score)), 6),
            })
            out.append(item)
    out.sort(key=lambda r: (str(r.get("date") or ""), str(r.get("sector_etf") or "")))
    return out


def _trend_gate(frame: pd.DataFrame, date: str) -> dict[str, Any]:
    if frame is None or frame.empty or "Close" not in frame:
        return {"ok": False, "score": 0.0}
    day = pd.Timestamp(date)
    if day not in frame.index:
        return {"ok": False, "score": 0.0}
    idx = int(frame.index.get_loc(day))
    close = pd.to_numeric(frame["Close"], errors="coerce")
    if idx < 60:
        return {"ok": False, "score": 0.0}
    c = _num(close.iloc[idx])
    ma20 = _num(close.iloc[max(0, idx - 19): idx + 1].mean())
    ma50 = _num(close.iloc[max(0, idx - 49): idx + 1].mean())
    ma200 = _num(close.iloc[max(0, idx - 199): idx + 1].mean()) if idx >= 199 else None
    ret20 = _num(c / close.iloc[idx - 20] - 1.0) if idx >= 20 and _num(close.iloc[idx - 20]) else 0.0
    ret60 = _num(c / close.iloc[idx - 60] - 1.0) if idx >= 60 and _num(close.iloc[idx - 60]) else 0.0
    score = 0.0
    score += 0.30 if c and ma20 and c > ma20 else 0.0
    score += 0.25 if c and ma50 and c > ma50 else 0.0
    score += 0.20 if ma20 and ma50 and ma20 > ma50 else 0.0
    score += 0.15 if ret20 and ret20 > 0 else 0.0
    score += 0.10 if ret60 and ret60 > 0 else 0.0
    if ma200 is not None:
        score = 0.85 * score + (0.15 if c and c > ma200 else 0.0)
    return {
        "ok": bool(score >= 0.55),
        "score": round(max(0.0, min(1.0, score)), 6),
        "ret20": round(ret20 or 0.0, 6),
        "ret60": round(ret60 or 0.0, 6),
    }


def _attach_outcomes(rows: list[dict[str, Any]], horizons: list[int], period: str) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int]]:
    histories: dict[str, pd.DataFrame] = {}
    for symbol in sorted({r["sector_etf"] for r in rows} | {"SPY", "QQQ"}):
        _history(symbol, histories, period)
    rt_cost = cost_model.equity_round_trip_cost()
    by_h: dict[str, list[dict[str, Any]]] = {}
    skipped = {"missing_sector_return": 0, "missing_benchmark_return": 0}
    for h in horizons:
        out = []
        for row in rows:
            etf = row["sector_etf"]
            sector_ret = _forward_return(histories.get(etf, pd.DataFrame()), row["date"], h)
            spy_ret = _forward_return(histories.get("SPY", pd.DataFrame()), row["date"], h)
            qqq_ret = _forward_return(histories.get("QQQ", pd.DataFrame()), row["date"], h)
            if sector_ret is None:
                skipped["missing_sector_return"] += 1
                continue
            if spy_ret is None or qqq_ret is None:
                skipped["missing_benchmark_return"] += 1
                continue
            spy_gate = _trend_gate(histories.get("SPY", pd.DataFrame()), row["date"])
            qqq_gate = _trend_gate(histories.get("QQQ", pd.DataFrame()), row["date"])
            sector_gate = _trend_gate(histories.get(etf, pd.DataFrame()), row["date"])
            market_gate_score = 0.50 * float(spy_gate.get("score") or 0.0) + 0.50 * float(qqq_gate.get("score") or 0.0)
            gated_score = (
                0.42 * float(row.get("sector_diffusion_score") or 0.0)
                + 0.26 * float(row.get("sector_signal_score") or 0.0)
                + 0.18 * float(sector_gate.get("score") or 0.0)
                + 0.14 * market_gate_score
            )
            item = dict(row)
            item.update({
                "horizon_days": h,
                "sector_forward_return": round(sector_ret, 8),
                "sector_net_return": round(sector_ret - rt_cost, 8),
                "spy_return": round(spy_ret, 8),
                "qqq_return": round(qqq_ret, 8),
                "alpha_vs_spy": round(sector_ret - rt_cost - spy_ret, 8),
                "alpha_vs_qqq": round(sector_ret - rt_cost - qqq_ret, 8),
                "spy_trend_score": spy_gate.get("score"),
                "qqq_trend_score": qqq_gate.get("score"),
                "sector_trend_score": sector_gate.get("score"),
                "market_gate_score": round(market_gate_score, 6),
                "gated_sector_score": round(max(0.0, min(1.0, gated_score)), 6),
            })
            out.append(item)
        by_h[str(h)] = out
    return by_h, skipped


def _daily_rank_strategy(rows: list[dict[str, Any]], top_n: int, metric: str, score_key: str = "sector_signal_score") -> dict[str, Any]:
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_date[str(row.get("date"))].append(row)
    picks: list[dict[str, Any]] = []
    for date, group in by_date.items():
        group = sorted(group, key=lambda r: float(r.get(score_key) or 0.0), reverse=True)
        picks.extend(group[:top_n])
    out = _summarize(f"daily_top{top_n}_{score_key}_{metric}", picks, metric)
    out["score_key"] = score_key
    return out


def _variant_analysis(rows: list[dict[str, Any]], score_key: str) -> dict[str, Any]:
    return {
        "score_key": score_key,
        "bucket_by_score": {
            "net_return": _bucket_rows(rows, score_key, "sector_net_return"),
            "alpha_vs_spy": _bucket_rows(rows, score_key, "alpha_vs_spy"),
            "alpha_vs_qqq": _bucket_rows(rows, score_key, "alpha_vs_qqq"),
        },
        "daily_selection": {
            "top1_net_return": _daily_rank_strategy(rows, 1, "sector_net_return", score_key),
            "top1_alpha_vs_spy": _daily_rank_strategy(rows, 1, "alpha_vs_spy", score_key),
            "top1_alpha_vs_qqq": _daily_rank_strategy(rows, 1, "alpha_vs_qqq", score_key),
            "top3_net_return": _daily_rank_strategy(rows, 3, "sector_net_return", score_key),
            "top3_alpha_vs_spy": _daily_rank_strategy(rows, 3, "alpha_vs_spy", score_key),
            "top3_alpha_vs_qqq": _daily_rank_strategy(rows, 3, "alpha_vs_qqq", score_key),
        },
    }


def _analyze_horizon(rows: list[dict[str, Any]]) -> dict[str, Any]:
    score_variants = {
        "static": _variant_analysis(rows, "sector_signal_score"),
        "diffusion": _variant_analysis(rows, "sector_diffusion_score"),
        "gated": _variant_analysis(rows, "gated_sector_score"),
    }
    return {
        "events": len(rows),
        "sectors": sorted({r["sector_etf"] for r in rows}),
        "bucket_by_score": score_variants["static"]["bucket_by_score"],
        "daily_selection": score_variants["static"]["daily_selection"],
        "score_variants": score_variants,
        "per_sector": {
            etf: {
                "net_return": _summarize(f"{etf}_net_return", [r for r in rows if r["sector_etf"] == etf], "sector_net_return"),
                "alpha_vs_spy": _summarize(f"{etf}_alpha_vs_spy", [r for r in rows if r["sector_etf"] == etf], "alpha_vs_spy"),
                "alpha_vs_qqq": _summarize(f"{etf}_alpha_vs_qqq", [r for r in rows if r["sector_etf"] == etf], "alpha_vs_qqq"),
            }
            for etf in sorted({r["sector_etf"] for r in rows})
        },
    }


def _md(payload: dict[str, Any]) -> str:
    variant_labels = {
        "static": "静态热度",
        "diffusion": "扩散加速",
        "gated": "扩散+市场闸门",
    }
    lines = [
        "# 赛道级信号聚合回测",
        "",
        f"- Generated: {payload['generated_at']}",
        f"- Input: `{payload['input_json']}`",
        f"- 聚合口径: 日期 × 赛道 ETF；同日同赛道至少 {payload['min_components']} 只成分股触发信号。",
        "- 因果口径: 这是赛道广度/共识温度计，不证明个股信号导致赛道上涨。",
        "- 新增检验: 同时比较静态热度、信号扩散加速、扩散叠加 SPY/QQQ/赛道趋势闸门。",
        "",
        "## 每日选赛道结果",
        "",
        "| 窗口 | 排序口径 | 策略 | 净收益/超额 | 胜率 | CI | 显著 |",
        "| ---: | --- | --- | ---: | ---: | --- | --- |",
    ]
    for h, analysis in payload["analysis"].items():
        for variant, block in analysis.get("score_variants", {}).items():
            ds = block["daily_selection"]
            for key, label in [
                ("top1_net_return", "Top1 绝对净收益"),
                ("top1_alpha_vs_spy", "Top1 相对 SPY"),
                ("top1_alpha_vs_qqq", "Top1 相对 QQQ"),
                ("top3_net_return", "Top3 绝对净收益"),
                ("top3_alpha_vs_spy", "Top3 相对 SPY"),
                ("top3_alpha_vs_qqq", "Top3 相对 QQQ"),
            ]:
                row = ds[key]
                lines.append(
                    f"| {h}d | {variant_labels.get(variant, variant)} | {label} | {_pct(row.get('mean'))} | "
                    f"{row.get('win_rate', 0):.1%} | [{_pct(row.get('ci_low'))}, {_pct(row.get('ci_high'))}] | "
                    f"{'通过' if row.get('significant') else '未通过'} |"
                )
    lines.extend([
        "",
        "## 分桶检验：赛道信号分越高是否越好",
        "",
    ])
    for h, analysis in payload["analysis"].items():
        for variant, block in analysis.get("score_variants", {}).items():
            lines.extend(["", f"### {h}d - {variant_labels.get(variant, variant)}", "", "| Bucket | Score区间 | n | 净收益 | 相对SPY | 相对QQQ |"])
            lines.append("| ---: | --- | ---: | ---: | ---: | ---: |")
            net = block["bucket_by_score"]["net_return"]
            spy = block["bucket_by_score"]["alpha_vs_spy"]
            qqq = block["bucket_by_score"]["alpha_vs_qqq"]
            for i in range(min(len(net), len(spy), len(qqq))):
                lines.append(
                    f"| B{net[i]['bucket']} | {net[i]['score_range']} | {net[i]['n']} | "
                    f"{_pct(net[i].get('mean'))} | {_pct(spy[i].get('mean'))} | {_pct(qqq[i].get('mean'))} |"
                )
    lines.extend(["", "## 单赛道表现", ""])
    for h, analysis in payload["analysis"].items():
        lines.extend(["", f"### {h}d", "", "| ETF | n | 净收益 | 相对SPY | 相对QQQ |"])
        lines.append("| --- | ---: | ---: | ---: | ---: |")
        for etf, rows in analysis["per_sector"].items():
            lines.append(
                f"| {etf} | {rows['net_return'].get('n', 0)} | "
                f"{_pct(rows['net_return'].get('mean'))} | {_pct(rows['alpha_vs_spy'].get('mean'))} | "
                f"{_pct(rows['alpha_vs_qqq'].get('mean'))} |"
            )
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="agent/runs/pullback_playbook_sector_enriched_260625.json")
    ap.add_argument("--horizons", default="1,3,5,8")
    ap.add_argument("--period", default="5y")
    ap.add_argument("--min-components", type=int, default=3)
    ap.add_argument("--output", default="")
    args = ap.parse_args()

    source = Path(args.input)
    payload = json.loads(source.read_text(encoding="utf-8"))
    events = list(payload.get("events") or [])
    sector_rows, aggregate_skips = _aggregate_events(events, args.min_components)
    sector_rows = _attach_dynamic_features(sector_rows)
    horizons = [int(x.strip()) for x in args.horizons.split(",") if x.strip()]
    by_horizon, outcome_skips = _attach_outcomes(sector_rows, horizons, args.period)
    analysis = {h: _analyze_horizon(rows) for h, rows in by_horizon.items()}

    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "method": "component_signal_to_sector_signal_backtest",
        "input_json": str(source),
        "min_components": args.min_components,
        "horizons": horizons,
        "event_count": len(events),
        "sector_signal_rows": len(sector_rows),
        "skipped": {**aggregate_skips, **outcome_skips},
        "score_formula": {
            "sector_signal_score": "0.32*breadth + 0.28*adv_weighted_score + 0.18*median_score + 0.10*market_liquid_rs + 0.07*stock_vs_sector + 0.05*sector_strong + 0.03*volume_dry - concentration_penalty",
            "sector_diffusion_score": "0.42*score/breadth acceleration + 0.28*fresh_component_ratio + 0.18*stock_vs_sector + 0.12*market_liquid_rs",
            "gated_sector_score": "0.42*sector_diffusion + 0.26*sector_signal + 0.18*sector_trend_gate + 0.14*market_gate(SPY/QQQ)",
            "note": "Research formula only. Validate before using in production ranking.",
        },
        "analysis": analysis,
    }
    target = Path(args.output) if args.output else RUNS_DIR / f"sector_signal_backtest_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    md = target.with_suffix(".md")
    md.write_text(_md(out), encoding="utf-8")
    print(json.dumps({"json": str(target), "md": str(md), "sector_rows": len(sector_rows)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
