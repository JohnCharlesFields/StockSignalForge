"""Signal-level alpha validation with execution-agnostic price plans.

This script answers a narrower question than the close-to-open cockpit:

    When the strategy signal fires, does the signal itself have forward alpha?

It intentionally does not assume the user buys exactly at the close, at the
open, or at 15:45.  The event-study return uses signal-date close as a neutral
reference price, then measures close-to-close forward excess return over fixed
horizons.  Separately, it generates actionable *research levels*:

    - pullback buy reference
    - breakout confirmation reference
    - invalidation / stop reference
    - first and aggressive take-profit references

Those levels are not orders and are not used to cherry-pick historical entries.
They are current-session execution aids for manual review.
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
for _p in (AGENT, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import cost_model  # noqa: E402
from market_data_service import get_daily_history  # noqa: E402

RUNS_DIR = AGENT / "runs"

DEFAULT_SYMS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD",
    "NFLX", "INTC", "MU", "QCOM", "ADBE", "TXN", "AMAT", "MRVL", "ASML",
    "ROKU", "MRNA", "MDB", "DDOG", "PANW", "SMCI", "ON", "ARM", "PYPL",
    "CSCO", "COST", "PEP",
]


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _osc(close: pd.Series) -> tuple[pd.Series, pd.Series]:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(2).mean()
    loss = (-delta.clip(upper=0)).rolling(2).mean()
    rsi2 = 100.0 - 100.0 / (1.0 + gain / loss.replace(0.0, np.nan))
    ma = close.rolling(20).mean()
    sd = close.rolling(20).std()
    z = (close - ma) / sd.replace(0.0, np.nan)
    return rsi2, z


def _atr(frame: pd.DataFrame, window: int = 14) -> float | None:
    if frame is None or frame.empty or not {"High", "Low", "Close"}.issubset(frame.columns):
        return None
    high = pd.to_numeric(frame["High"], errors="coerce")
    low = pd.to_numeric(frame["Low"], errors="coerce")
    close = pd.to_numeric(frame["Close"], errors="coerce")
    prev = close.shift(1)
    tr = pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    value = _num(tr.tail(window).mean())
    return value if value and value > 0 else None


def _beta(stock: pd.DataFrame, bench: pd.DataFrame, idx: int, lookback: int = 120) -> float:
    if idx < 30:
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
    beta = float(np.cov(s, b)[0, 1] / var)
    return max(-1.0, min(3.0, beta)) if math.isfinite(beta) else 1.0


def _cluster_ci(rows: list[dict[str, Any]], key: str, n_boot: int = 3000, seed: int = 42) -> tuple[float | None, float | None]:
    by_symbol: dict[str, list[float]] = {}
    for row in rows:
        value = _num(row.get(key))
        if value is not None:
            by_symbol.setdefault(str(row["symbol"]), []).append(value)
    clusters = [k for k, vals in by_symbol.items() if vals]
    if len(clusters) < 2:
        return None, None
    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(n_boot):
        sample = [rng.choice(clusters) for _ in clusters]
        vals = [x for c in sample for x in by_symbol[c]]
        if vals:
            estimates.append(float(sum(vals) / len(vals)))
    if not estimates:
        return None, None
    estimates.sort()
    return estimates[int(0.025 * (len(estimates) - 1))], estimates[int(0.975 * (len(estimates) - 1))]


def _price_plan(symbol: str, frame: pd.DataFrame) -> dict[str, Any]:
    close = _num(frame["Close"].iloc[-1])
    if close is None or close <= 0:
        return {"symbol": symbol, "available": False, "reason": "missing_price"}
    atr = _atr(frame) or max(close * 0.025, 0.01)
    high = _num(frame["High"].iloc[-1], close) or close
    low = _num(frame["Low"].iloc[-1], close) or close
    rolling = frame.tail(min(len(frame), 30))
    ma20 = _num(pd.to_numeric(frame["Close"], errors="coerce").rolling(20).mean().iloc[-1])
    support_low = _num(pd.to_numeric(rolling["Low"], errors="coerce").tail(10).min())
    supports = [x for x in (ma20, support_low, low) if x and 0 < x < close]
    nearest_support = max(supports) if supports else close - 0.75 * atr

    buy_pullback = max(close - 2.5 * atr, min(close - 0.05 * atr, nearest_support + 0.15 * atr))
    buy_strength = max(close + 0.15 * atr, high)
    reference_entry = buy_pullback if abs(close - buy_pullback) <= abs(buy_strength - close) else buy_strength
    invalidation = max(0.01, min(low, nearest_support - 0.25 * atr, reference_entry - 0.75 * atr))
    risk = max(0.01, reference_entry - invalidation)
    take_profit = reference_entry + 1.5 * risk
    aggressive_take_profit = reference_entry + 2.2 * risk
    return {
        "symbol": symbol,
        "available": True,
        "as_of_price": round(close, 2),
        "atr14": round(atr, 2),
        "buy_pullback_price": round(buy_pullback, 2),
        "buy_strength_price": round(buy_strength, 2),
        "stop_chasing_above": round(close + 0.9 * atr, 2),
        "stop_buying_below": round(max(0.01, nearest_support - 0.35 * atr), 2),
        "reference_entry": round(reference_entry, 2),
        "invalidation_price": round(invalidation, 2),
        "take_profit": round(take_profit, 2),
        "aggressive_take_profit": round(aggressive_take_profit, 2),
        "reward_risk": round((take_profit - reference_entry) / risk, 2),
        "nearest_support": round(nearest_support, 2),
        "plain_cn": (
            f"{symbol}: 回踩观察 ${buy_pullback:.2f}，强势确认 ${buy_strength:.2f}；"
            f"跌破 ${invalidation:.2f} 视为研究假设失效，第一止盈 ${take_profit:.2f}。"
        ),
    }


def _finite_bar(frame: pd.DataFrame, idx: int) -> dict[str, float] | None:
    if idx < 0 or idx >= len(frame):
        return None
    out: dict[str, float] = {}
    for col in ("Open", "High", "Low", "Close"):
        value = _num(frame[col].iloc[idx]) if col in frame else None
        if value is None or value <= 0:
            return None
        out[col.lower()] = value
    return out


def _entry_trigger_for_day(bar: dict[str, float], plan: dict[str, Any], mode: str) -> tuple[float, str] | None:
    pullback = _num(plan.get("buy_pullback_price"))
    breakout = _num(plan.get("buy_strength_price"))
    candidates: list[tuple[float, str]] = []
    if mode in {"pullback", "either"} and pullback and bar["low"] <= pullback:
        # Conservative limit-fill: if the market gaps below the limit, still use
        # the limit price instead of a better open.  This avoids overstating edge.
        candidates.append((pullback, "pullback"))
    if mode in {"breakout", "either"} and breakout and bar["high"] >= breakout:
        # Stop-buy style fill: if the market gaps above the trigger, fill at the
        # open; otherwise fill at the trigger.
        candidates.append((max(breakout, bar["open"]), "breakout"))
    if not candidates:
        return None
    if mode == "either" and len(candidates) > 1:
        # Intraday order is unknown with daily bars.  Use the worse long entry.
        price, label = max(candidates, key=lambda item: item[0])
        return price, f"either_{label}"
    return candidates[0]


def _simulate_path_trade(
    stock: pd.DataFrame,
    bench: pd.DataFrame,
    *,
    symbol: str,
    signal_idx: int,
    beta: float,
    plan: dict[str, Any],
    mode: str,
    entry_window: int,
    max_hold: int,
) -> dict[str, Any] | None:
    """Simulate a non-lookahead path-trigger trade from daily bars.

    Levels are frozen on the signal date.  Entry may happen only in the next
    ``entry_window`` sessions.  Exit is the earliest of stop, take-profit, or
    max-hold close.  Same-day stop/take-profit ambiguity is resolved by assuming
    the stop occurs first, which is conservative for long trades.
    """
    if not plan.get("available"):
        return None
    entry: tuple[int, float, str] | None = None
    last_entry_idx = min(len(stock) - 1, signal_idx + max(1, entry_window))
    for idx in range(signal_idx + 1, last_entry_idx + 1):
        bar = _finite_bar(stock, idx)
        if not bar:
            continue
        triggered = _entry_trigger_for_day(bar, plan, mode)
        if triggered:
            entry = (idx, triggered[0], triggered[1])
            break
    if not entry:
        return None

    entry_idx, entry_price, trigger = entry
    if entry_price <= 0:
        return None
    invalidation = _num(plan.get("invalidation_price"))
    take_profit = _num(plan.get("take_profit"))
    if not invalidation or invalidation <= 0 or not take_profit or take_profit <= entry_price:
        return None

    exit_idx = min(len(stock) - 1, entry_idx + max(1, max_hold))
    exit_price = _num(stock["Close"].iloc[exit_idx])
    exit_reason = "max_hold_close"
    for idx in range(entry_idx, exit_idx + 1):
        bar = _finite_bar(stock, idx)
        if not bar:
            continue
        if bar["low"] <= invalidation:
            exit_idx = idx
            exit_price = min(bar["open"], invalidation) if bar["open"] < invalidation else invalidation
            exit_reason = "stop_loss"
            break
        if bar["high"] >= take_profit:
            exit_idx = idx
            exit_price = take_profit
            exit_reason = "take_profit"
            break
    if not exit_price or exit_price <= 0:
        return None

    bench_entry = _num(bench["Close"].iloc[entry_idx])
    bench_exit = _num(bench["Close"].iloc[exit_idx])
    if not bench_entry or not bench_exit or bench_entry <= 0 or bench_exit <= 0:
        return None
    rt_cost = cost_model.equity_round_trip_cost(price=entry_price)
    ret = exit_price / entry_price - 1.0 - rt_cost
    bench_ret = bench_exit / bench_entry - 1.0
    alpha = ret - beta * bench_ret
    return {
        "symbol": symbol,
        "signal_date": stock.index[signal_idx].strftime("%Y-%m-%d"),
        "entry_date": stock.index[entry_idx].strftime("%Y-%m-%d"),
        "exit_date": stock.index[exit_idx].strftime("%Y-%m-%d"),
        "mode": mode,
        "trigger": trigger,
        "entry_price": round(entry_price, 4),
        "exit_price": round(exit_price, 4),
        "exit_reason": exit_reason,
        "hold_days": int(exit_idx - entry_idx),
        "net_return": ret,
        "benchmark_return": bench_ret,
        "beta_adjusted_alpha": alpha,
        "reward_risk": plan.get("reward_risk"),
        "plan": {
            "buy_pullback_price": plan.get("buy_pullback_price"),
            "buy_strength_price": plan.get("buy_strength_price"),
            "invalidation_price": plan.get("invalidation_price"),
            "take_profit": plan.get("take_profit"),
        },
    }


def _align(stock: pd.DataFrame, bench: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    common = stock.index.intersection(bench.index).sort_values()
    return stock.loc[common].copy(), bench.loc[common].copy()


def _collect_symbol_events(
    symbol: str,
    benchmark: str,
    *,
    period: str,
    years: float,
    horizons: list[int],
    max_events: int,
    entry_window: int,
    max_hold: int,
    path_modes: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    stock, stock_source = get_daily_history(symbol, period=period)
    bench, bench_source = get_daily_history(benchmark, period=period)
    if stock is None or bench is None or stock.empty or bench.empty:
        return [], [], {"symbol": symbol, "available": False, "reason": "missing_history"}
    stock, bench = _align(stock, bench)
    max_h = max(max(horizons), int(entry_window) + int(max_hold) + 1)
    if len(stock) < 80 + max_h:
        return [], [], {"symbol": symbol, "available": False, "reason": "insufficient_history"}

    close = pd.to_numeric(stock["Close"], errors="coerce").reset_index(drop=True)
    bench_close = pd.to_numeric(bench["Close"], errors="coerce").reset_index(drop=True)
    rsi2, z = _osc(close)
    cutoff = pd.Timestamp.now(tz="UTC").tz_localize(None) - pd.Timedelta(days=int(years * 365))
    dates = list(stock.index)
    rt_cost = cost_model.equity_round_trip_cost()
    picks: list[dict[str, Any]] = []
    path_picks: list[dict[str, Any]] = []
    for i in range(25, len(close) - max_h):
        raw_date = dates[i]
        di = raw_date.tz_localize(None) if getattr(raw_date, "tzinfo", None) else raw_date
        if di < cutoff:
            continue
        dos = (pd.notna(z.iloc[i]) and z.iloc[i] < -1.5) or (pd.notna(rsi2.iloc[i]) and rsi2.iloc[i] < 10)
        if not dos:
            continue
        entry = _num(close.iloc[i])
        bench_entry = _num(bench_close.iloc[i])
        if not entry or not bench_entry or entry <= 0 or bench_entry <= 0:
            continue
        beta = _beta(stock, bench, i)
        plan_at_signal = _price_plan(symbol, stock.iloc[: i + 1])
        event: dict[str, Any] = {
            "symbol": symbol,
            "date": raw_date.strftime("%Y-%m-%d"),
            "signal": "deep_oversold_pullback_hv",
            "entry_reference": entry,
            "rsi2": _num(rsi2.iloc[i]),
            "z20": _num(z.iloc[i]),
            "beta": beta,
        }
        for h in horizons:
            stock_exit = _num(close.iloc[i + h])
            bench_exit = _num(bench_close.iloc[i + h])
            if not stock_exit or not bench_exit:
                continue
            stock_ret = stock_exit / entry - 1.0 - rt_cost
            bench_ret = bench_exit / bench_entry - 1.0
            event[f"h{h}_ret"] = stock_ret
            event[f"h{h}_bench_excess"] = stock_ret - bench_ret
            event[f"h{h}_beta_alpha"] = stock_ret - beta * bench_ret
        picks.append(event)
        for mode in path_modes:
            trade = _simulate_path_trade(
                stock,
                bench,
                symbol=symbol,
                signal_idx=i,
                beta=beta,
                plan=plan_at_signal,
                mode=mode,
                entry_window=entry_window,
                max_hold=max_hold,
            )
            if trade:
                path_picks.append(trade)
    selected = picks[-max_events:] if max_events > 0 else picks
    selected_dates = {(row["symbol"], row["date"]) for row in selected}
    selected_path = [row for row in path_picks if (row["symbol"], row["signal_date"]) in selected_dates]
    return selected, selected_path, {
        "symbol": symbol,
        "available": True,
        "stock_source": stock_source,
        "benchmark": benchmark,
        "benchmark_source": bench_source,
        "price_plan": _price_plan(symbol, stock),
    }


def _summarize_path(rows: list[dict[str, Any]], modes: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"trade_count": len(rows), "symbol_count": len({r["symbol"] for r in rows}), "modes": {}}
    for mode in modes:
        mr = [r for r in rows if r.get("mode") == mode]
        vals = [_num(r.get("beta_adjusted_alpha")) for r in mr]
        vals = [v for v in vals if v is not None]
        if not vals:
            out["modes"][mode] = {"n": 0}
            continue
        ci_lo, ci_hi = _cluster_ci(mr, "beta_adjusted_alpha")
        exits: dict[str, int] = {}
        triggers: dict[str, int] = {}
        for row in mr:
            exits[str(row.get("exit_reason") or "unknown")] = exits.get(str(row.get("exit_reason") or "unknown"), 0) + 1
            triggers[str(row.get("trigger") or "unknown")] = triggers.get(str(row.get("trigger") or "unknown"), 0) + 1
        out["modes"][mode] = {
            "n": len(vals),
            "mean_beta_alpha": round(mean(vals), 6),
            "median_beta_alpha": round(median(vals), 6),
            "alpha_win_rate": round(sum(v > 0 for v in vals) / len(vals), 4),
            "mean_net_return": round(mean([float(r.get("net_return") or 0) for r in mr]), 6),
            "take_profit_rate": round(exits.get("take_profit", 0) / len(mr), 4) if mr else 0,
            "stop_loss_rate": round(exits.get("stop_loss", 0) / len(mr), 4) if mr else 0,
            "ci_low": round(ci_lo, 6) if ci_lo is not None else None,
            "ci_high": round(ci_hi, 6) if ci_hi is not None else None,
            "significant": bool(ci_lo is not None and ci_lo > 0),
            "exit_reasons": exits,
            "triggers": triggers,
        }
    return out


def _summarize(rows: list[dict[str, Any]], horizons: list[int]) -> dict[str, Any]:
    out: dict[str, Any] = {"event_count": len(rows), "symbol_count": len({r["symbol"] for r in rows}), "horizons": {}}
    for h in horizons:
        key = f"h{h}_beta_alpha"
        vals = [_num(r.get(key)) for r in rows]
        vals = [v for v in vals if v is not None]
        if not vals:
            out["horizons"][str(h)] = {"n": 0}
            continue
        ci_lo, ci_hi = _cluster_ci(rows, key)
        out["horizons"][str(h)] = {
            "n": len(vals),
            "mean_beta_alpha": round(mean(vals), 6),
            "median_beta_alpha": round(median(vals), 6),
            "alpha_win_rate": round(sum(v > 0 for v in vals) / len(vals), 4),
            "ci_low": round(ci_lo, 6) if ci_lo is not None else None,
            "ci_high": round(ci_hi, 6) if ci_hi is not None else None,
            "significant": bool(ci_lo is not None and ci_lo > 0),
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Execution-agnostic signal alpha validation.")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMS))
    ap.add_argument("--benchmark", default="QQQ")
    ap.add_argument("--period", default="3y")
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--horizons", default="1,2,3,5,8,10")
    ap.add_argument("--max-events-per-symbol", type=int, default=20)
    ap.add_argument("--entry-window", type=int, default=3, help="Sessions after signal date where a frozen plan can trigger entry.")
    ap.add_argument("--max-hold-days", type=int, default=8, help="Maximum sessions to hold after a path-trigger entry.")
    ap.add_argument("--path-modes", default="pullback,breakout,either", help="Comma-separated path entry modes.")
    ap.add_argument("--output-prefix", default="")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    path_modes = [x.strip().lower() for x in args.path_modes.split(",") if x.strip()]
    rows: list[dict[str, Any]] = []
    path_rows: list[dict[str, Any]] = []
    symbol_reports: list[dict[str, Any]] = []
    for sym in symbols:
        events, path_events, report = _collect_symbol_events(
            sym,
            args.benchmark.upper(),
            period=args.period,
            years=args.years,
            horizons=horizons,
            max_events=args.max_events_per_symbol,
            entry_window=args.entry_window,
            max_hold=args.max_hold_days,
            path_modes=path_modes,
        )
        rows.extend(events)
        path_rows.extend(path_events)
        symbol_reports.append({**report, "event_count": len(events)})

    summary = _summarize(rows, horizons)
    path_summary = _summarize_path(path_rows, path_modes)
    payload = {
        "method": "signal_level_close_to_close_beta_adjusted_alpha",
        "path_method": "frozen_plan_path_trigger_backtest",
        "note": "Signal-date close is a neutral event-study reference. Path backtest freezes levels on the signal date, then enters only if future daily bars touch pullback or breakout levels.",
        "benchmark": args.benchmark.upper(),
        "symbols": symbols,
        "horizons": horizons,
        "path_config": {
            "entry_window": args.entry_window,
            "max_hold_days": args.max_hold_days,
            "modes": path_modes,
            "conservative_assumptions": [
                "limit pullback fills at the limit even if the open gaps lower",
                "breakout fills at trigger or gap-up open, whichever is higher",
                "if stop and take-profit both occur in one daily bar, stop is assumed first",
            ],
        },
        "summary": summary,
        "path_summary": path_summary,
        "symbols_report": symbol_reports,
        "events": rows,
        "path_trades": path_rows,
    }

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    basename = args.output_prefix.strip() or f"signal_path_alpha_{dt.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    json_path = RUNS_DIR / f"{basename}.json"
    md_path = RUNS_DIR / f"{basename}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 策略信号本身超额验证",
        "",
        f"- 方法：{payload['method']}",
        f"- 基准：{args.benchmark.upper()}",
        f"- 事件数：{summary['event_count']}，标的数：{summary['symbol_count']}",
        "- 说明：这里验证的是信号本身，不绑定尾盘/开盘/15:45 执行；买入与卖出价另列为研究参考。",
        "",
        "## 超额结果",
        "",
        "| 持有窗口 | n | 平均 beta-adjusted alpha | 中位数 | alpha 胜率 | CI95 | 结论 |",
        "|---:|---:|---:|---:|---:|---|---|",
    ]
    for h in horizons:
        item = summary["horizons"].get(str(h), {})
        lines.append(
            f"| T+{h} | {item.get('n', 0)} | {float(item.get('mean_beta_alpha') or 0)*100:+.3f}% | "
            f"{float(item.get('median_beta_alpha') or 0)*100:+.3f}% | {float(item.get('alpha_win_rate') or 0)*100:.1f}% | "
            f"[{item.get('ci_low')}, {item.get('ci_high')}] | {'通过' if item.get('significant') else '未通过'} |"
        )
    lines += ["", "## 当前价格计划参考", ""]
    lines += [
        "",
        "## 路径触发回测",
        "",
        f"- 入场窗口：信号后 {args.entry_window} 个交易日内",
        f"- 最长持有：入场后 {args.max_hold_days} 个交易日",
        "- 规则：信号日冻结回踩买、强势确认、失效价、止盈价；后续触价才入场。",
        "- 保守假设：同一日同时触发止损和止盈时按止损先发生；突破跳空按更差的开盘价成交。",
        "",
        "| 入场模式 | 成交数 | 平均 beta-adjusted alpha | 中位数 | alpha 胜率 | 止盈率 | 止损率 | CI95 | 结论 |",
        "|---|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for mode in path_modes:
        item = path_summary["modes"].get(mode, {})
        lines.append(
            f"| {mode} | {item.get('n', 0)} | {float(item.get('mean_beta_alpha') or 0)*100:+.3f}% | "
            f"{float(item.get('median_beta_alpha') or 0)*100:+.3f}% | {float(item.get('alpha_win_rate') or 0)*100:.1f}% | "
            f"{float(item.get('take_profit_rate') or 0)*100:.1f}% | {float(item.get('stop_loss_rate') or 0)*100:.1f}% | "
            f"[{item.get('ci_low')}, {item.get('ci_high')}] | {'通过' if item.get('significant') else '未通过'} |"
        )
    for item in symbol_reports:
        plan = item.get("price_plan") or {}
        if not plan.get("available"):
            continue
        lines.append(
            f"- **{item['symbol']}**：现价 ${plan['as_of_price']}；回踩买 ${plan['buy_pullback_price']}；"
            f"强势确认 ${plan['buy_strength_price']}；失效 ${plan['invalidation_price']}；"
            f"第一止盈 ${plan['take_profit']}；激进止盈 ${plan['aggressive_take_profit']}。"
        )
    lines += [
        "",
        "## 纪律",
        "",
        "- 本报告不是下单指令；价格计划用于人工复核。",
        "- 回测没有用未来最低价/最高价反推最优入场，避免把执行优化伪装成 alpha。",
        "- 若要验证具体执行法，应另开路径触发回测：触达回踩价或突破价才入场，再按止损/止盈/最长持有结算。",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"events={len(rows)} symbols={len(symbols)} report_json={json_path} report_md={md_path}", flush=True)


if __name__ == "__main__":
    main()
