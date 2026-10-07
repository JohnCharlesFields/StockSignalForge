#!/usr/bin/env python3
"""Backtest bull/bear consensus + volatility impulse signals.

This is a research-only validator for the user's intuition:

    direction ~= bull consensus - bear consensus
    launch potential ~= positive direction consensus * volatility room

Historical option IV is not guaranteed to be point-in-time in the current data
stack, so this script uses replayable OHLCV-only volatility proxies (HV20 and
ATR%) from ``consensus_signal_service``.  Live IV can be layered on later only
after we have a point-in-time IV history.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_DIR = SCRIPT_DIR.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

# Local script runs on Windows should not inherit Docker-only /app cache paths.
# These defaults are set before importing modules that read env vars at import
# time.  Existing explicit non-/app values are respected.
if os.environ.get("VIBE_MARKET_DATA_CACHE_DIR", "").startswith("/app/"):
    os.environ["VIBE_MARKET_DATA_CACHE_DIR"] = str(AGENT_DIR / "data_cache" / "market_data")
elif not os.environ.get("VIBE_MARKET_DATA_CACHE_DIR"):
    os.environ["VIBE_MARKET_DATA_CACHE_DIR"] = str(AGENT_DIR / "data_cache" / "market_data")
if os.environ.get("VIBE_UNIVERSE_SNAPSHOT_DIR", "").startswith("/app/"):
    os.environ["VIBE_UNIVERSE_SNAPSHOT_DIR"] = str(AGENT_DIR / "data_cache" / "universe_snapshots")
elif not os.environ.get("VIBE_UNIVERSE_SNAPSHOT_DIR"):
    os.environ["VIBE_UNIVERSE_SNAPSHOT_DIR"] = str(AGENT_DIR / "data_cache" / "universe_snapshots")

import cost_model  # noqa: E402
import exit_model  # noqa: E402
from consensus_signal_service import consensus_feature_frame  # noqa: E402
from launch_signal_service import _ticker_frame  # noqa: E402
from market_data_service import aggregate_data_quality, download_daily_history  # noqa: E402
from scripts.research_signal_framework_backtest import _collect_symbol_events  # noqa: E402
from scripts.screening_framework_v2_optimized import resolve_universe  # noqa: E402
from signal_calibration import build_calibration  # noqa: E402


RUNS_DIR = AGENT_DIR / "runs"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "--"
    try:
        return f"{float(value):.2%}"
    except (TypeError, ValueError):
        return "--"


def _download_history_with_research_fallback(symbols: list[str], period: str) -> tuple[pd.DataFrame, dict[str, str]]:
    """Download history via project data router, then yfinance as script-only fallback."""
    panel, sources = download_daily_history(symbols, period=period)
    missing = symbols
    if not panel.empty:
        present = set()
        if isinstance(panel.columns, pd.MultiIndex):
            for level in (0, panel.columns.nlevels - 1):
                present.update(str(x).upper() for x in panel.columns.get_level_values(level))
        missing = [s for s in symbols if s not in present]
    # Research-only fallback: useful when local API-key env is absent. This does
    # not alter the app data router and is clearly labelled in output quality.
    if missing:
        try:
            import yfinance as yf

            yf_panel = yf.download(
                symbols,
                period=period,
                auto_adjust=False,
                progress=False,
                threads=False,
                timeout=30,
            )
            if not yf_panel.empty:
                panel = yf_panel if panel.empty else panel.combine_first(yf_panel)
                for symbol in symbols:
                    sources.setdefault(symbol, "yfinance:research-fallback")
        except Exception:
            pass
    return panel, sources


def _collect_consensus_events(
    ticker: str,
    frame: pd.DataFrame,
    benchmark: pd.DataFrame,
    horizon: int,
    *,
    cooldown_days: int,
) -> list[dict[str, Any]]:
    """Collect cooldown-spaced replay events for one symbol.

    Events are sampled across the full score range because calibration needs
    losers too.  Forward return uses the project-wide exit convention:
    entry close[T] -> exit open[T+horizon] by default.
    """
    if frame is None or frame.empty or "Close" not in frame or len(frame) < 80:
        return []
    clean = frame.dropna(subset=["Close"]).copy()
    close = pd.to_numeric(clean["Close"], errors="coerce").dropna()
    if len(close) < 80:
        return []
    clean = clean.reindex(close.index)
    open_ = exit_model.aligned_open(clean, close)
    baseline = exit_model.baseline_forward_returns(close, open_, horizon)
    if not baseline:
        return []
    symbol_baseline = mean(baseline)
    features = consensus_feature_frame(clean, benchmark=benchmark)
    if features.empty:
        return []

    events: list[dict[str, Any]] = []
    next_allowed = 0
    # 60 bars gives the RS/HV/ATR features enough trailing context while still
    # keeping the sample broad.  MA200-based flags simply stay neutral until
    # available.
    for i in range(60, len(clean) - horizon):
        if i < next_allowed:
            continue
        row = features.iloc[i]
        direction_score = row.get("consensus_direction_score")
        launch_score = row.get("consensus_launch_score")
        if not (pd.notna(direction_score) and pd.notna(launch_score)):
            continue
        forward_return = exit_model.forward_return(close, open_, i, horizon)
        if forward_return is None:
            continue
        events.append({
            "ticker": ticker,
            "date": clean.index[i].strftime("%Y-%m-%d"),
            "consensus_direction_score": round(_num(direction_score), 6),
            "consensus_launch_score": round(_num(launch_score), 6),
            "bull_consensus": round(_num(row.get("bull_consensus")), 6),
            "bear_consensus": round(_num(row.get("bear_consensus")), 6),
            "net_consensus": round(_num(row.get("net_consensus")), 6),
            "volatility_impulse": round(_num(row.get("volatility_impulse")), 6),
            "hv20": round(_num(row.get("hv20")), 6) if pd.notna(row.get("hv20")) else None,
            "atr_pct": round(_num(row.get("atr_pct")), 6) if pd.notna(row.get("atr_pct")) else None,
            "forward_return": forward_return,
            "symbol_baseline_return": symbol_baseline,
            "excess_return": forward_return - symbol_baseline,
        })
        next_allowed = i + max(1, cooldown_days)
    return events


def _curve(
    events: list[dict[str, Any]],
    *,
    signal_type: str,
    score_field: str,
    horizon: int,
    cost_bps: float,
) -> dict[str, Any]:
    scoped = [
        {
            "ticker": event.get("ticker"),
            "score": event.get(score_field),
            "forward_return": event.get("forward_return"),
            "excess_return": event.get("excess_return"),
        }
        for event in events
        if event.get(score_field) is not None
    ]
    return build_calibration(scoped, signal_type=signal_type, horizon=horizon, cost_bps=cost_bps)


def _cluster_bootstrap_mean_ci(
    rows: list[dict[str, Any]],
    *,
    value_key: str,
    n_bootstrap: int = 800,
    seed: int = 42,
) -> tuple[float | None, float | None]:
    clusters: dict[str, list[float]] = {}
    for row in rows:
        ticker = str(row.get("ticker") or "")
        value = row.get(value_key)
        if not ticker or value is None:
            continue
        clusters.setdefault(ticker, []).append(_num(value))
    tickers = sorted(clusters)
    if len(tickers) < 8:
        return (None, None)
    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(n_bootstrap):
        sample = [rng.choice(tickers) for _ in tickers]
        values = [v for ticker in sample for v in clusters[ticker]]
        if values:
            estimates.append(mean(values))
    if not estimates:
        return (None, None)
    estimates.sort()
    return (
        round(estimates[int(0.025 * (len(estimates) - 1))], 6),
        round(estimates[int(0.975 * (len(estimates) - 1))], 6),
    )


def _net_return_summary(
    events: list[dict[str, Any]],
    *,
    score_field: str,
    cost_bps: float,
    target_buckets: int = 10,
) -> dict[str, Any]:
    """Bucket by score and evaluate absolute net forward returns.

    This intentionally ignores symbol baseline / sector alpha.  It answers the
    practical question: if this bucket is bought and later exited, did it make
    money after the assumed round-trip cost?
    """
    round_trip_cost = 2.0 * max(0.0, cost_bps) / 10000.0
    rows = []
    for event in events:
        score = event.get(score_field)
        fwd = event.get("forward_return")
        if score is None or fwd is None:
            continue
        rows.append({
            **event,
            "_score": max(0.0, min(1.0, _num(score))),
            "forward_net": _num(fwd) - round_trip_cost,
        })
    rows.sort(key=lambda row: row["_score"])
    if not rows:
        return {"events": 0, "clusters": 0, "buckets": [], "edge_validated": False}
    n = len(rows)
    buckets: list[dict[str, Any]] = []
    for i in range(target_buckets):
        lo_i = int(round(i * n / target_buckets))
        hi_i = int(round((i + 1) * n / target_buckets))
        group = rows[lo_i:hi_i]
        if not group:
            continue
        ci_low, ci_high = _cluster_bootstrap_mean_ci(group, value_key="forward_net")
        buckets.append({
            "lo": round(min(row["_score"] for row in group), 6),
            "hi": round(max(row["_score"] for row in group), 6),
            "n": len(group),
            "clusters": len({row.get("ticker") for row in group if row.get("ticker")}),
            "hit_rate_net": round(sum(row["forward_net"] > 0 for row in group) / len(group), 6),
            "mean_forward_net": round(mean([row["forward_net"] for row in group]), 6),
            "forward_net_ci": [ci_low, ci_high],
        })
    top = buckets[-1] if buckets else {}
    bottom = buckets[0] if buckets else {}
    ci = top.get("forward_net_ci") or [None, None]
    edge_validated = bool(
        top.get("n", 0) >= 25
        and top.get("mean_forward_net", 0.0) > 0
        and ci[0] is not None
        and ci[0] > 0
    )
    return {
        "events": n,
        "clusters": len({row.get("ticker") for row in rows if row.get("ticker")}),
        "global_hit_rate_net": round(sum(row["forward_net"] > 0 for row in rows) / n, 6),
        "global_mean_forward_net": round(mean([row["forward_net"] for row in rows]), 6),
        "buckets": buckets,
        "top_bucket": top,
        "bottom_bucket": bottom,
        "edge_validated": edge_validated,
        "method": "absolute net forward return > 0 after round-trip equity cost",
    }


def _curve_summary(curve: dict[str, Any]) -> dict[str, Any]:
    global_ = curve.get("global") or {}
    edge = global_.get("edge") or {}
    top = edge.get("top_bucket") or {}
    buckets = curve.get("buckets") or []
    bottom = buckets[0] if buckets else {}
    return {
        "events": global_.get("events", 0),
        "clusters": global_.get("clusters", 0),
        "global_hit_rate": global_.get("hit_rate"),
        "global_mean_excess_net": global_.get("mean_excess_net"),
        "edge_validated": bool(edge.get("validated")),
        "edge_spread": edge.get("spread"),
        "top_bucket": top,
        "bottom_bucket": {
            "score_range": [bottom.get("lo"), bottom.get("hi")],
            "n": bottom.get("n"),
            "mean_excess_net": bottom.get("mean_excess_net"),
            "hit_rate": bottom.get("hit_rate"),
        },
    }


def _collect_universe(
    universe: str,
    *,
    period: str,
    horizons: list[int],
    max_symbols: int,
    cooldown_days: int,
    include_baseline_pullback: bool,
) -> dict[str, Any]:
    tickers, source, _meta = resolve_universe(universe, archive_only=True)
    if max_symbols:
        tickers = tickers[:max_symbols]
    symbols = sorted(set(tickers + ["SPY"]))
    panel, data_sources = _download_history_with_research_fallback(symbols, period)
    benchmark = _ticker_frame(panel, "SPY")

    by_horizon: dict[str, Any] = {}
    for horizon in horizons:
        consensus_events: list[dict[str, Any]] = []
        pullback_events: list[dict[str, Any]] = []
        for ticker in tickers:
            frame = _ticker_frame(panel, ticker)
            consensus_events.extend(_collect_consensus_events(
                ticker,
                frame,
                benchmark,
                horizon,
                cooldown_days=cooldown_days,
            ))
            if include_baseline_pullback:
                pullback_events.extend(_collect_symbol_events(
                    ticker,
                    frame,
                    horizon,
                    min_launch_score=0.0,
                    min_tunnel_score=0.0,
                    cooldown_days=cooldown_days,
                ))

        cost_bps = cost_model.equity_one_way_bps()
        direction_curve = _curve(
            consensus_events,
            signal_type="consensus_direction",
            score_field="consensus_direction_score",
            horizon=horizon,
            cost_bps=cost_bps,
        )
        launch_curve = _curve(
            consensus_events,
            signal_type="consensus_launch",
            score_field="consensus_launch_score",
            horizon=horizon,
            cost_bps=cost_bps,
        )
        item: dict[str, Any] = {
            "consensus_events": len(consensus_events),
            "consensus_direction": {
                "curve": direction_curve,
                "summary": _curve_summary(direction_curve),
                "net_return": _net_return_summary(
                    consensus_events,
                    score_field="consensus_direction_score",
                    cost_bps=cost_bps,
                ),
            },
            "consensus_launch": {
                "curve": launch_curve,
                "summary": _curve_summary(launch_curve),
                "net_return": _net_return_summary(
                    consensus_events,
                    score_field="consensus_launch_score",
                    cost_bps=cost_bps,
                ),
            },
            "_raw_consensus_events": consensus_events,
        }
        if include_baseline_pullback:
            pullback_curve = _curve(
                pullback_events,
                signal_type="pullback_hv",
                score_field="pullback_hv_score",
                horizon=horizon,
                cost_bps=cost_bps,
            )
            item["pullback_hv_baseline"] = {
                "events": len(pullback_events),
                "curve": pullback_curve,
                "summary": _curve_summary(pullback_curve),
                "net_return": _net_return_summary(
                    pullback_events,
                    score_field="pullback_hv_score",
                    cost_bps=cost_bps,
                ),
            }
            item["_raw_pullback_events"] = pullback_events
        by_horizon[str(horizon)] = item

    return {
        "universe": universe,
        "source": source,
        "tickers": len(tickers),
        "data_quality": aggregate_data_quality(data_sources),
        "horizons": by_horizon,
    }


def _dedupe_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for event in events:
        key = (str(event.get("ticker") or ""), str(event.get("date") or ""))
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        out.append(event)
    return out


def _build_cross_universe_summary(payload: dict[str, Any]) -> dict[str, Any]:
    cost_bps = _num(payload.get("cost_bps"), cost_model.equity_one_way_bps())
    result: dict[str, Any] = {
        "label": "cross_universe_deduped",
        "method": "dedupe by ticker/date within each horizon, then evaluate absolute net return and excess edge",
        "horizons": {},
    }
    for horizon in payload.get("horizons") or []:
        hkey = str(horizon)
        consensus_events: list[dict[str, Any]] = []
        pullback_events: list[dict[str, Any]] = []
        for uni in payload.get("universes") or []:
            item = (uni.get("horizons") or {}).get(hkey) or {}
            consensus_events.extend(item.get("_raw_consensus_events") or [])
            pullback_events.extend(item.get("_raw_pullback_events") or [])
        consensus_events = _dedupe_events(consensus_events)
        pullback_events = _dedupe_events(pullback_events)
        direction_curve = _curve(
            consensus_events,
            signal_type="consensus_direction",
            score_field="consensus_direction_score",
            horizon=int(horizon),
            cost_bps=cost_bps,
        )
        launch_curve = _curve(
            consensus_events,
            signal_type="consensus_launch",
            score_field="consensus_launch_score",
            horizon=int(horizon),
            cost_bps=cost_bps,
        )
        pullback_curve = _curve(
            pullback_events,
            signal_type="pullback_hv",
            score_field="pullback_hv_score",
            horizon=int(horizon),
            cost_bps=cost_bps,
        ) if pullback_events else {}
        result["horizons"][hkey] = {
            "consensus_direction": {
                "summary": _curve_summary(direction_curve),
                "net_return": _net_return_summary(
                    consensus_events,
                    score_field="consensus_direction_score",
                    cost_bps=cost_bps,
                ),
            },
            "consensus_launch": {
                "summary": _curve_summary(launch_curve),
                "net_return": _net_return_summary(
                    consensus_events,
                    score_field="consensus_launch_score",
                    cost_bps=cost_bps,
                ),
            },
            "pullback_hv_baseline": {
                "summary": _curve_summary(pullback_curve) if pullback_curve else {},
                "net_return": _net_return_summary(
                    pullback_events,
                    score_field="pullback_hv_score",
                    cost_bps=cost_bps,
                ),
            },
        }
    return result


def _strip_raw_events(payload: dict[str, Any]) -> None:
    for uni in payload.get("universes") or []:
        for item in (uni.get("horizons") or {}).values():
            item.pop("_raw_consensus_events", None)
            item.pop("_raw_pullback_events", None)


def _md(payload: dict[str, Any]) -> str:
    lines = [
        "# 多空共识度 + 波动率启动空间历史回测",
        "",
        f"- Generated: {payload['generated_at']}",
        f"- Period: {payload['period']}",
        f"- Exit convention: {payload['exit_model']}",
        f"- Cost: one-way equity cost {payload['cost_bps']} bps, calibration subtracts round trip.",
        "- IV handling: 当前回测只使用可回放的 HV20 / ATR 作为历史 IV 代理；未使用今日 option snapshot 回填历史。",
        "",
        "## 结论速览",
        "",
        "| 股票池 | 窗口 | 信号 | 事件 | 标的 | 顶桶胜率 | 顶桶净超额 | 顶桶CI | Edge |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for uni in payload["universes"]:
        for horizon, item in uni["horizons"].items():
            for key, label in [
                ("consensus_direction", "方向共识"),
                ("consensus_launch", "方向共识×波动"),
                ("pullback_hv_baseline", "回调+波动基线"),
            ]:
                if key not in item:
                    continue
                summary = item[key]["summary"]
                top = summary.get("top_bucket") or {}
                ci = top.get("excess_ci") or [None, None]
                lines.append(
                    f"| {uni['universe']} | {horizon}d | {label} | {summary.get('events', 0)} | "
                    f"{summary.get('clusters', 0)} | {_fmt_pct(top.get('hit_rate'))} | "
                    f"{_fmt_pct(top.get('mean_excess_net'))} | [{_fmt_pct(ci[0])}, {_fmt_pct(ci[1])}] | "
                    f"{'通过' if summary.get('edge_validated') else '未通过'} |"
                )
    lines.extend([
        "",
        "## 绝对净收益速览",
        "",
        "| 范围 | 窗口 | 信号 | 事件 | 顶桶赚钱率 | 顶桶净收益 | 顶桶CI | Net Edge |",
        "| --- | ---: | --- | ---: | ---: | ---: | --- | --- |",
    ])
    scopes = list(payload.get("universes") or [])
    if payload.get("aggregate"):
        scopes.append(payload["aggregate"])
    for scope in scopes:
        scope_label = scope.get("universe") or scope.get("label") or "aggregate"
        for horizon, item in (scope.get("horizons") or {}).items():
            for key, label in [
                ("consensus_direction", "方向共识"),
                ("consensus_launch", "方向共识×波动"),
                ("pullback_hv_baseline", "回调+波动基线"),
            ]:
                if key not in item:
                    continue
                net = item[key].get("net_return") or {}
                top = net.get("top_bucket") or {}
                ci = top.get("forward_net_ci") or [None, None]
                lines.append(
                    f"| {scope_label} | {horizon}d | {label} | {net.get('events', 0)} | "
                    f"{_fmt_pct(top.get('hit_rate_net'))} | {_fmt_pct(top.get('mean_forward_net'))} | "
                    f"[{_fmt_pct(ci[0])}, {_fmt_pct(ci[1])}] | "
                    f"{'通过' if net.get('edge_validated') else '未通过'} |"
                )
    lines.extend([
        "",
        "## 判读口径",
        "",
        "- 顶桶胜率不是涨跌胜率，而是 P(扣成本后未来窗口收益 > 该标的自身无条件基线)。",
        "- Edge 通过需要顶桶样本足够、平均净超额为正，且按 ticker 聚类 bootstrap 的 95% CI 下沿大于 0。",
        "- 若“方向共识×波动”优于“方向共识”，说明波动率空间确实在增强启动信号；反之说明波动项当前是噪声或需要换成真实历史 IV。",
        "",
        "## 详细桶",
    ])
    for uni in payload["universes"]:
        lines.extend(["", f"### {uni['universe']} ({uni['tickers']} tickers, {uni['source']})"])
        for horizon, item in uni["horizons"].items():
            for key, label in [
                ("consensus_direction", "方向共识"),
                ("consensus_launch", "方向共识×波动"),
                ("pullback_hv_baseline", "回调+波动基线"),
            ]:
                if key not in item:
                    continue
                curve = item[key]["curve"]
                lines.extend(["", f"#### {label} / {horizon}d", ""])
                lines.append("| score区间 | n | 校准胜率 | 净前瞻收益 | 净超额 |")
                lines.append("| --- | ---: | ---: | ---: | ---: |")
                for bucket in curve.get("buckets") or []:
                    lines.append(
                        f"| [{bucket['lo']:.3f}, {bucket['hi']:.3f}] | {bucket['n']} | "
                        f"{bucket.get('p_iso', bucket['hit_rate']):.1%} | "
                        f"{bucket['mean_forward_net']:.2%} | {bucket['mean_excess_net']:.2%} |"
                    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest OHLCV bull/bear consensus signal.")
    parser.add_argument("--universes", default="spx,ndx,sox", help="Comma-separated universe keys.")
    parser.add_argument("--period", default="5y")
    parser.add_argument("--horizons", default="5,8,10")
    parser.add_argument("--max-symbols", type=int, default=0, help="Per-universe cap for smoke runs.")
    parser.add_argument("--cooldown-days", type=int, default=5)
    parser.add_argument("--no-baseline-pullback", action="store_true")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    universes = [u.strip() for u in args.universes.split(",") if u.strip()]
    horizons = [int(h.strip()) for h in args.horizons.split(",") if h.strip()]
    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": args.period,
        "horizons": horizons,
        "universe_keys": universes,
        "max_symbols": args.max_symbols,
        "cooldown_days": args.cooldown_days,
        "cost_bps": cost_model.equity_one_way_bps(),
        "exit_model": exit_model.describe(),
        "method": {
            "direction": "bull_consensus - bear_consensus from trailing OHLCV features",
            "volatility": "HV20 rise + ATR percent as point-in-time historical IV proxy",
            "win": "net-of-cost forward return beats each symbol's unconditional baseline",
        },
        "universes": [],
    }
    for universe in universes:
        print(f"[{universe}] start", flush=True)
        payload["universes"].append(_collect_universe(
            universe,
            period=args.period,
            horizons=horizons,
            max_symbols=args.max_symbols,
            cooldown_days=args.cooldown_days,
            include_baseline_pullback=not args.no_baseline_pullback,
        ))
    payload["aggregate"] = _build_cross_universe_summary(payload)
    _strip_raw_events(payload)

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    output = Path(args.output) if args.output else RUNS_DIR / f"consensus_signal_backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = output.with_suffix(".md")
    md_path.write_text(_md(payload), encoding="utf-8")
    print(json.dumps({
        "json": str(output),
        "md": str(md_path),
        "universes": len(payload["universes"]),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
