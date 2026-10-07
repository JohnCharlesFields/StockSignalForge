#!/usr/bin/env python3
"""Walk-forward validation for the cash-equity research timing framework.

The screener is a research-ranking tool, not an order executor.  This validator
tests whether launch and daily-tunnel cohorts have positive forward excess
returns versus each symbol's unconditional history.  Constituents must be
archived by date before the output can be called a bias-controlled backtest.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from datetime import datetime
from pathlib import Path
from statistics import NormalDist, mean, stdev
from typing import Any

import pandas as pd
import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_DIR = SCRIPT_DIR.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import exit_model  # noqa: E402
from launch_signal_service import _score_frame, _ticker_frame  # noqa: E402
from market_data_service import download_daily_history  # noqa: E402
from scripts.screening_framework_v2_optimized import CONFIG, resolve_universe  # noqa: E402


def _forward_return(close: pd.Series, index: int, horizon: int, open_: pd.Series | None = None) -> float | None:
    """entry=close[T] -> exit per exit_model (default open[T+H]). See exit_model.py."""
    return exit_model.forward_return(close, open_, index, horizon)


def _baseline_forward_returns(close: pd.Series, horizon: int, open_: pd.Series | None = None) -> list[float]:
    return exit_model.baseline_forward_returns(close, open_, horizon)


def _collect_symbol_events(
    ticker: str,
    frame: pd.DataFrame,
    horizon: int,
    *,
    min_launch_score: float = 0.50,
    min_tunnel_score: float = 70.0,
    cooldown_days: int = 5,
    include_unresolved: bool = False,
) -> list[dict[str, Any]]:
    """Replay vectorized trailing scores and keep non-overlapping observations.

    ``include_unresolved=True`` ALSO emits the trailing ``horizon`` bars (today and
    the last ~horizon-1 sessions) whose forward return cannot be computed yet --
    these carry ``forward_return=None`` / ``resolved=False`` but a real
    ``pullback_hv_score``, so a LIVE chart can mark a fresh setup. Default False
    keeps backtest/calibration behaviour identical (resolved events only)."""
    if frame.empty or "Close" not in frame or len(frame) < 45:
        return []
    clean = frame.dropna(subset=["Close"]).copy()
    close = clean["Close"].astype(float)
    open_ = exit_model.aligned_open(clean, close)  # entry close[T] -> exit open[T+H]
    baseline = _baseline_forward_returns(close, horizon, open_)
    if not baseline:
        return []
    symbol_baseline = mean(baseline)
    ret_5 = close.pct_change(5)
    ret_20 = close.pct_change(20)
    volume = pd.to_numeric(clean.get("Volume", pd.Series(0.0, index=clean.index)), errors="coerce").fillna(0.0)
    vol_ratio = volume / volume.rolling(20).mean().clip(lower=1.0)
    breakout = close / close.shift(1).rolling(20).max() - 1.0
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = -delta.clip(upper=0).rolling(14).mean()
    rsi = 100.0 - 100.0 / (1.0 + gain / loss.replace(0.0, np.nan))
    rsi = rsi.fillna(100.0)
    momentum = (3.2 * ret_5 + 1.8 * ret_20).clip(-1.0, 1.0)
    volume_boost = ((vol_ratio - 1.0) / 2.0).clip(-0.4, 0.8)
    breakout_boost = (breakout * 12.0).clip(-0.4, 0.8)
    rsi_balance = ((rsi - 50.0) / 100.0).clip(-0.3, 0.3)
    base_score = (0.5 + 0.28 * momentum + 0.20 * volume_boost + 0.22 * breakout_boost + 0.10 * rsi_balance).clip(0.0, 1.0)

    ma5, ma10, ma15, ma20 = (close.rolling(window).mean() for window in (5, 10, 15, 20))
    above_ma5 = close >= ma5
    above_ma20 = close >= ma20
    aligned = (ma5 >= ma10) & (ma10 >= ma15) & (ma15 >= ma20)
    streak = above_ma5.astype(int).groupby((~above_ma5).cumsum()).cumsum()
    max_break = ((ma5 - close) / ma5).clip(lower=0.0).rolling(20).max()
    rejection_depth = (1.0 - max_break / 0.08).clip(lower=0.0)
    zone = pd.Series(np.select(
        [close >= ma5, close >= ma10, close >= ma15, close >= ma20],
        [1.0, 0.72, 0.48, 0.28],
        default=0.0,
    ), index=clean.index)
    tunnel_score = (
        above_ma5.rolling(20).mean() * 32.0
        + (streak / 10.0).clip(upper=1.0) * 18.0
        + aligned.rolling(20).mean() * 18.0
        + above_ma20.rolling(20).mean() * 12.0
        + rejection_depth * 12.0
        + zone * 8.0
    )
    launch_score = (0.78 * base_score + 0.22 * tunnel_score / 100.0).clip(0.0, 1.0)

    # HV (realized vol) expansion: the volatility-rise launch signal. Fully
    # derivable from price, so it backfills for the whole replay -- unlike IV.
    log_ret = np.log(close / close.shift(1))
    hv20 = log_ret.rolling(20).std() * np.sqrt(252.0)
    hv_rise = (hv20 / hv20.shift(10) - 1.0).replace([np.inf, -np.inf], np.nan)
    hv_rise_norm = (0.5 + 0.5 * hv_rise).clip(0.0, 1.0)
    pullback = (1.0 - launch_score).clip(0.0, 1.0)
    # "oversold + vol expanding" -> high when both a pullback and rising HV.
    pullback_hv_score = (0.5 * pullback + 0.5 * hv_rise_norm).clip(0.0, 1.0)

    # NOTE: a 超短-playbook "structure quality" filter (uptrend + higher-low +
    # volume stabilization + shallow pullback) was tested here and REJECTED --
    # blending it into pullback_hv collapsed the 10d top-bucket excess from
    # +0.89% to +0.11% (CI crossed 0). The trend/shallow filter excludes the
    # deeply-oversold names that carry the mean-reversion edge. See MAINTENANCE_LOG.

    events: list[dict[str, Any]] = []
    next_allowed = 0
    upper = len(clean) if include_unresolved else len(clean) - horizon
    for index in range(24, upper):
        if index < next_allowed:
            continue
        if (
            math.isfinite(float(launch_score.iloc[index]))
            and float(launch_score.iloc[index]) >= min_launch_score
            and float(tunnel_score.iloc[index]) >= min_tunnel_score
        ):
            forward_return = _forward_return(close, index, horizon, open_)
            if forward_return is None and not include_unresolved:
                continue
            hv_rise_val = hv_rise.iloc[index]
            events.append({
                "ticker": ticker,
                "date": clean.index[index].strftime("%Y-%m-%d"),
                "launch_score": round(float(launch_score.iloc[index]), 4),
                "tunnel_score": round(float(tunnel_score.iloc[index]), 1),
                "hv_rise": round(float(hv_rise_val), 4) if math.isfinite(float(hv_rise_val)) else None,
                "hv_rise_norm": round(float(hv_rise_norm.iloc[index]), 4) if math.isfinite(float(hv_rise_norm.iloc[index])) else None,
                "pullback_hv_score": round(float(pullback_hv_score.iloc[index]), 4) if math.isfinite(float(pullback_hv_score.iloc[index])) else None,
                "forward_return": forward_return,
                "symbol_baseline_return": symbol_baseline,
                "excess_return": (forward_return - symbol_baseline) if forward_return is not None else None,
                "resolved": forward_return is not None,
            })
            next_allowed = index + max(1, cooldown_days)
    return events


def _cluster_bootstrap_ci(
    events: list[dict[str, Any]],
    *,
    n_bootstrap: int = 2000,
    seed: int = 42,
) -> tuple[float, float]:
    """Resample symbols, preserving correlated observations inside each symbol."""
    clusters: dict[str, list[float]] = {}
    for event in events:
        clusters.setdefault(str(event["ticker"]), []).append(float(event["excess_return"]))
    tickers = sorted(clusters)
    if len(tickers) < 2:
        return (0.0, 0.0)
    rng = random.Random(seed)
    estimates = []
    for _ in range(n_bootstrap):
        sample = [rng.choice(tickers) for _ in tickers]
        values = [value for ticker in sample for value in clusters[ticker]]
        estimates.append(mean(values))
    estimates.sort()
    return (
        estimates[int(0.025 * (len(estimates) - 1))],
        estimates[int(0.975 * (len(estimates) - 1))],
    )


def _summarize(events: list[dict[str, Any]], universe: str, horizon: int) -> dict[str, Any]:
    excess = [float(item["excess_return"]) for item in events]
    returns = [float(item["forward_return"]) for item in events]
    clusters = len({str(item["ticker"]) for item in events})
    avg_excess = mean(excess) if excess else 0.0
    sigma = stdev(excess) if len(excess) >= 2 else 0.0
    standard_error = sigma / math.sqrt(len(excess)) if sigma else 0.0
    t_stat = avg_excess / standard_error if standard_error else (99.0 if avg_excess > 0 else 0.0)
    p_value = 1.0 - NormalDist().cdf(t_stat)
    ci_low, ci_high = _cluster_bootstrap_ci(events)
    return {
        "universe": universe,
        "horizon_days": horizon,
        "events": len(events),
        "clusters": clusters,
        "mean_forward_return": round(mean(returns), 6) if returns else 0.0,
        "mean_excess_return": round(avg_excess, 6),
        "hit_rate": round(sum(value > 0 for value in returns) / len(returns), 4) if returns else 0.0,
        "t_stat": round(t_stat, 4),
        "p_value_one_sided": round(p_value, 6),
        "cluster_bootstrap_ci_95": [round(ci_low, 6), round(ci_high, 6)],
    }


def _apply_bh_fdr(rows: list[dict[str, Any]]) -> None:
    """Attach Benjamini-Hochberg q-values across universe/horizon tests."""
    ordered = sorted(enumerate(rows), key=lambda item: float(item[1]["p_value_one_sided"]))
    total = len(ordered)
    running = 1.0
    for rank, (index, row) in reversed(list(enumerate(ordered, start=1))):
        running = min(running, float(row["p_value_one_sided"]) * total / rank)
        rows[index]["q_value_bh"] = round(min(1.0, running), 6)
    for row in rows:
        ci_low = float(row["cluster_bootstrap_ci_95"][0])
        row["hypothesis_supported"] = bool(
            int(row["events"]) >= 30
            and int(row["clusters"]) >= 8
            and float(row["mean_excess_return"]) > 0
            and float(row["p_value_one_sided"]) <= 0.05
            and float(row.get("q_value_bh", 1.0)) <= 0.10
            and ci_low > 0
        )


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# 三层实股信号框架历史验证",
        "",
        f"- Generated: {payload['generated_at']}",
        f"- Universes: {', '.join(payload['universes'])}",
        f"- Signal rule: launch_score >= {payload['thresholds']['launch_score']:.2f} and daily_tunnel >= {payload['thresholds']['daily_tunnel']:.1f}",
        f"- Bias control: {payload['bias_control']}",
        "",
        "| 股票池 | 观察窗 | 事件数 | 标的数 | 平均前瞻收益 | 相对同标的基线 | 胜率 | 单侧 p 值 | BH q 值 | 聚类 Bootstrap 95% CI | 结论 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for row in payload["tests"]:
        ci = row["cluster_bootstrap_ci_95"]
        lines.append(
            f"| {row['universe']} | {row['horizon_days']}d | {row['events']} | {row['clusters']} | "
            f"{row['mean_forward_return']:.2%} | {row['mean_excess_return']:.2%} | {row['hit_rate']:.1%} | "
            f"{row['p_value_one_sided']:.4f} | {row['q_value_bh']:.4f} | [{ci[0]:.2%}, {ci[1]:.2%}] | "
            f"{'支持 H1' if row['hypothesis_supported'] else '暂不支持 H1'} |"
        )
    lines.extend([
        "",
        "## 统计口径",
        "",
        "- H0：信号出现后的前瞻收益不优于该标的无条件历史基线。",
        "- H1：信号出现后的前瞻收益高于该标的无条件历史基线。",
        "- 同一标的信号使用冷却期降频；Bootstrap 按标的聚类重采样；跨股票池与观察窗使用 BH-FDR 修正。",
        "- 当前成分股回溯只能用于研发筛查。只有按日期归档并加载历史成分快照后，才可视为控制前视偏差和幸存者偏差。",
    ])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Walk-forward statistical validation for the three-layer research timing framework.")
    parser.add_argument("--universes", default="spx,ndx,sox", help="Comma-separated research universes.")
    parser.add_argument("--period", default="5y", help="History period, for example 3y or 5y.")
    parser.add_argument("--horizons", default="5,10", help="Comma-separated forward-return horizons.")
    parser.add_argument("--max-symbols", type=int, default=0, help="Optional per-universe cap for smoke runs.")
    parser.add_argument("--launch-score", type=float, default=0.50)
    parser.add_argument("--daily-tunnel", type=float, default=70.0)
    parser.add_argument("--cooldown-days", type=int, default=5)
    parser.add_argument("--snapshot-date", default="", help="Historical constituent date. Requires archived snapshots.")
    parser.add_argument("--snapshot-dir", default=CONFIG["universe_snapshot_dir"])
    parser.add_argument("--output", default=str(AGENT_DIR / "runs" / "_research_signal_framework_backtest.json"))
    args = parser.parse_args()
    CONFIG["universe_snapshot_dir"] = args.snapshot_dir
    universes = [item.strip().lower() for item in args.universes.split(",") if item.strip()]
    horizons = [int(item) for item in args.horizons.split(",") if item.strip()]
    rows: list[dict[str, Any]] = []
    sources: dict[str, str] = {}
    ticker_counts: dict[str, int] = {}
    for universe in universes:
        tickers, source, _ = resolve_universe(
            universe,
            as_of=args.snapshot_date or None,
            archive_only=bool(args.snapshot_date),
        )
        if args.max_symbols:
            tickers = tickers[: args.max_symbols]
        sources[universe] = source
        ticker_counts[universe] = len(tickers)
        print(f"[{universe}] downloading {len(tickers)} symbols ({source})", flush=True)
        panel, _ = download_daily_history(tickers, period=args.period)
        for horizon in horizons:
            events = []
            for ticker in tickers:
                events.extend(_collect_symbol_events(
                    ticker,
                    _ticker_frame(panel, ticker),
                    horizon,
                    min_launch_score=args.launch_score,
                    min_tunnel_score=args.daily_tunnel,
                    cooldown_days=args.cooldown_days,
                ))
            print(f"[{universe}] {horizon}d: {len(events)} events", flush=True)
            rows.append(_summarize(events, universe, horizon))
    _apply_bh_fdr(rows)
    bias_control = (
        f"historical snapshots loaded for {args.snapshot_date}"
        if args.snapshot_date
        else "research-only current-constituent replay; survivor/look-ahead bias not fully controlled"
    )
    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "universes": universes,
        "ticker_counts": ticker_counts,
        "sources": sources,
        "period": args.period,
        "thresholds": {"launch_score": args.launch_score, "daily_tunnel": args.daily_tunnel, "cooldown_days": args.cooldown_days},
        "bias_control": bias_control,
        "tests": rows,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    output.with_suffix(".md").write_text(_markdown(payload), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
