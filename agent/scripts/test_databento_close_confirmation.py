#!/usr/bin/env python3
"""Test whether Databento 1-minute close confirmation improves pullback_hv win rate.

Research question:
    Given the existing daily ``pullback_hv`` signal, do candidates with a
    constructive final-hour / closing-session profile realize better net
    forward excess returns?

This is an experiment script. It does not activate calibration curves and does
not change live ranking. The API key is read from DATABENTO_API_KEY only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_DIR = SCRIPT_DIR.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from scripts.build_databento_signal_calibration import _load_databento_panel  # noqa: E402
from scripts.research_signal_framework_backtest import _collect_symbol_events  # noqa: E402
from scripts.screening_framework_v2_optimized import CONFIG, resolve_universe  # noqa: E402
import cost_model  # noqa: E402

NY = ZoneInfo("America/New_York")
DEFAULT_CACHE_DIR = AGENT_DIR / "data_cache" / "databento_intraday"


def _period_start(period: str) -> str:
    today = date.today()
    value = str(period or "180d").lower().strip()
    if value.endswith("y"):
        days = 366 * int(value[:-1] or 1)
    elif value.endswith("mo"):
        days = 31 * int(value[:-2] or 1)
    elif value.endswith("d"):
        days = int(value[:-1] or 1)
    else:
        days = 180
    return (today - timedelta(days=days)).isoformat()


def _cache_key(dataset: str, schema: str, start: str, end: str, symbols: list[str]) -> str:
    payload = json.dumps(
        {"dataset": dataset, "schema": schema, "start": start, "end": end, "symbols": symbols},
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _load_1m_raw(
    *,
    dataset: str,
    symbols: list[str],
    start: str,
    end: str,
    cache_dir: Path,
    use_cache: bool,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{_cache_key(dataset, 'ohlcv-1m', start, end, symbols)}.parquet"
    if use_cache and cache_file.exists():
        raw = pd.read_parquet(cache_file)
        return raw, {"cache_hit": True, "cache_file": str(cache_file), "raw_rows": int(len(raw))}
    try:
        import databento as db
    except ImportError as exc:
        raise RuntimeError("databento package is not installed") from exc
    if not os.environ.get("DATABENTO_API_KEY"):
        raise RuntimeError("DATABENTO_API_KEY is not set")
    client = db.Historical()
    store = client.timeseries.get_range(
        dataset=dataset,
        schema="ohlcv-1m",
        symbols=symbols,
        stype_in="raw_symbol",
        start=start,
        end=end,
    )
    raw = store.to_df()
    if not raw.empty:
        raw.to_parquet(cache_file)
    return raw, {"cache_hit": False, "cache_file": str(cache_file), "raw_rows": int(len(raw))}


def _regular_session(group: pd.DataFrame) -> pd.DataFrame:
    frame = group.copy()
    idx = pd.DatetimeIndex(pd.to_datetime(frame.index))
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    local = idx.tz_convert(NY)
    frame["_local_time"] = local.time
    mask = (local.time >= datetime.strptime("09:30", "%H:%M").time()) & (
        local.time <= datetime.strptime("16:00", "%H:%M").time()
    )
    return frame.loc[mask].copy()


def _build_close_features(raw_1m: pd.DataFrame) -> dict[tuple[str, str], dict[str, Any]]:
    """Return {(symbol, YYYY-MM-DD): close-confirmation features}."""
    if raw_1m is None or raw_1m.empty or "symbol" not in raw_1m.columns:
        return {}
    frame = raw_1m.copy()
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index))
    if frame.index.tz is None:
        utc = frame.index.tz_localize("UTC")
    else:
        utc = frame.index.tz_convert("UTC")
    local = utc.tz_convert(NY)
    frame["_date"] = [d.isoformat() for d in local.date]
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for (symbol, day), group in frame.groupby(["symbol", "_date"]):
        reg = _regular_session(group)
        if len(reg) < 60:
            continue
        for col in ("open", "high", "low", "close", "volume"):
            reg[col] = pd.to_numeric(reg[col], errors="coerce")
        reg = reg.dropna(subset=["close"])
        if reg.empty:
            continue
        last = float(reg["close"].iloc[-1])
        high = float(reg["high"].max())
        low = float(reg["low"].min())
        volume = reg["volume"].fillna(0.0).clip(lower=0.0)
        total_volume = float(volume.sum())
        vwap = float((reg["close"] * volume).sum() / total_volume) if total_volume > 0 else last
        last30 = reg.iloc[-30:] if len(reg) >= 30 else reg
        last60 = reg.iloc[-60:] if len(reg) >= 60 else reg
        last30_volume = float(last30["volume"].fillna(0.0).sum())
        last60_volume = float(last60["volume"].fillna(0.0).sum())
        last30_ret = last / float(last30["close"].iloc[0]) - 1.0 if float(last30["close"].iloc[0]) > 0 else 0.0
        last60_ret = last / float(last60["close"].iloc[0]) - 1.0 if float(last60["close"].iloc[0]) > 0 else 0.0
        rng = max(high - low, 1e-9)
        close_location = (last - low) / rng
        volume_share_30 = last30_volume / total_volume if total_volume > 0 else 0.0
        volume_share_60 = last60_volume / total_volume if total_volume > 0 else 0.0
        score = (
            0.35 * max(0.0, min(1.0, close_location))
            + 0.25 * (1.0 if last >= vwap else 0.0)
            + 0.20 * (1.0 if last30_ret > 0 else 0.0)
            + 0.10 * (1.0 if last60_ret > 0 else 0.0)
            + 0.10 * max(0.0, min(1.0, volume_share_30 / 0.14))
        )
        confirmed = bool(score >= 0.65 and last >= vwap and close_location >= 0.60)
        strong_confirmed = bool(score >= 0.75 and last30_ret > 0 and close_location >= 0.70)
        out[(str(symbol).upper(), str(day))] = {
            "close_confirm_score": round(score, 6),
            "close_confirmed": confirmed,
            "close_strong_confirmed": strong_confirmed,
            "close": round(last, 6),
            "vwap": round(vwap, 6),
            "close_vs_vwap_pct": round(last / vwap - 1.0, 6) if vwap > 0 else None,
            "close_location": round(close_location, 6),
            "last30_ret": round(last30_ret, 6),
            "last60_ret": round(last60_ret, 6),
            "last30_volume_share": round(volume_share_30, 6),
            "last60_volume_share": round(volume_share_60, 6),
        }
    return out


def _parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = str(value).strip().split(":", 1)
    return int(hour), int(minute)


def _beijing_time_label(et_hhmm: str) -> str:
    """Approximate ET -> Beijing label for US daylight-saving trading months.

    The experiment data timestamps are converted with real America/New_York
    timezone rules.  This label is only for display, using the current practical
    window the user trades in (US EDT -> Beijing +12h).
    """
    h, m = _parse_hhmm(et_hhmm)
    return f"{(h + 12) % 24:02d}:{m:02d}"


def _decision_features_for_time(raw_1m: pd.DataFrame, decision_time_et: str) -> dict[tuple[str, str], dict[str, Any]]:
    """Return point-in-time features using only bars up to ``decision_time_et``."""
    if raw_1m is None or raw_1m.empty or "symbol" not in raw_1m.columns:
        return {}
    h, m = _parse_hhmm(decision_time_et)
    cutoff = datetime.strptime(f"{h:02d}:{m:02d}", "%H:%M").time()
    frame = raw_1m.copy()
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index))
    utc = frame.index.tz_localize("UTC") if frame.index.tz is None else frame.index.tz_convert("UTC")
    local = utc.tz_convert(NY)
    frame["_date"] = [d.isoformat() for d in local.date]
    frame["_local_time"] = local.time
    out: dict[tuple[str, str], dict[str, Any]] = {}
    open_time = datetime.strptime("09:30", "%H:%M").time()
    for (symbol, day), group in frame.groupby(["symbol", "_date"]):
        session = group[(group["_local_time"] >= open_time) & (group["_local_time"] <= cutoff)].copy()
        if len(session) < 60:
            continue
        for col in ("open", "high", "low", "close", "volume"):
            session[col] = pd.to_numeric(session[col], errors="coerce")
        session = session.dropna(subset=["close"])
        if session.empty:
            continue
        current = float(session["close"].iloc[-1])
        high_so_far = float(session["high"].max())
        low_so_far = float(session["low"].min())
        open_price = float(session["open"].iloc[0])
        volume = session["volume"].fillna(0.0).clip(lower=0.0)
        total_volume = float(volume.sum())
        vwap_so_far = float((session["close"] * volume).sum() / total_volume) if total_volume > 0 else current
        last30 = session.iloc[-30:] if len(session) >= 30 else session
        last60 = session.iloc[-60:] if len(session) >= 60 else session
        ret30 = current / float(last30["close"].iloc[0]) - 1.0 if float(last30["close"].iloc[0]) > 0 else 0.0
        ret60 = current / float(last60["close"].iloc[0]) - 1.0 if float(last60["close"].iloc[0]) > 0 else 0.0
        rng = max(high_so_far - low_so_far, 1e-9)
        location = (current - low_so_far) / rng
        from_low = current / low_so_far - 1.0 if low_so_far > 0 else 0.0
        from_high = current / high_so_far - 1.0 if high_so_far > 0 else 0.0
        day_ret_so_far = current / open_price - 1.0 if open_price > 0 else 0.0
        vol_share_30 = float(last30["volume"].fillna(0.0).sum()) / total_volume if total_volume > 0 else 0.0

        # Two complementary scores:
        # - strength_score: classic "tail recovery / above VWAP" confirmation.
        # - weak_reversion_score: the pattern that the previous experiment found
        #   useful for overnight mean reversion: weak, below/near VWAP, not
        #   collapsing to fresh lows, and not already over-recovered.
        strength_score = (
            0.35 * max(0.0, min(1.0, location))
            + 0.25 * (1.0 if current >= vwap_so_far else 0.0)
            + 0.20 * (1.0 if ret30 > 0 else 0.0)
            + 0.10 * (1.0 if ret60 > 0 else 0.0)
            + 0.10 * max(0.0, min(1.0, vol_share_30 / 0.14))
        )
        below_vwap_not_far = 1.0 if -0.012 <= (current / vwap_so_far - 1.0 if vwap_so_far > 0 else 0.0) <= 0.002 else 0.0
        weak_not_crash = 1.0 if 0.18 <= location <= 0.58 else 0.0
        not_bouncing_too_hard = 1.0 if ret30 <= 0.004 and ret60 <= 0.008 else 0.0
        off_low_some = 1.0 if from_low >= 0.002 else 0.0
        weak_reversion_score = (
            0.30 * below_vwap_not_far
            + 0.30 * weak_not_crash
            + 0.20 * not_bouncing_too_hard
            + 0.10 * off_low_some
            + 0.10 * (1.0 if day_ret_so_far < 0 else 0.0)
        )
        out[(str(symbol).upper(), str(day))] = {
            "decision_time_et": decision_time_et,
            "decision_time_bj": _beijing_time_label(decision_time_et),
            "decision_price": round(current, 6),
            "decision_vwap": round(vwap_so_far, 6),
            "decision_vs_vwap_pct": round(current / vwap_so_far - 1.0, 6) if vwap_so_far > 0 else None,
            "decision_location": round(location, 6),
            "decision_ret30": round(ret30, 6),
            "decision_ret60": round(ret60, 6),
            "decision_from_low": round(from_low, 6),
            "decision_from_high": round(from_high, 6),
            "decision_day_ret": round(day_ret_so_far, 6),
            "decision_strength_score": round(strength_score, 6),
            "decision_weak_reversion_score": round(weak_reversion_score, 6),
            "decision_strength_confirmed": bool(strength_score >= 0.65 and current >= vwap_so_far and location >= 0.60),
            "decision_weak_reversion": bool(weak_reversion_score >= 0.70),
        }
    return out


def _cluster_ci(events: list[dict[str, Any]], value_key: str = "excess_net", n_bootstrap: int = 800) -> list[float | None]:
    clusters: dict[str, list[float]] = {}
    for ev in events:
        ticker = str(ev.get("ticker") or "")
        if ticker:
            clusters.setdefault(ticker, []).append(float(ev.get(value_key, 0.0)))
    tickers = sorted(clusters)
    if len(tickers) < 8:
        return [None, None]
    rng = random.Random(42)
    estimates: list[float] = []
    for _ in range(n_bootstrap):
        sample = [rng.choice(tickers) for _ in tickers]
        values = [v for t in sample for v in clusters[t]]
        if values:
            estimates.append(mean(values))
    estimates.sort()
    return [
        round(estimates[int(0.025 * (len(estimates) - 1))], 6),
        round(estimates[int(0.975 * (len(estimates) - 1))], 6),
    ]


def _summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    if not events:
        return {
            "n": 0,
            "clusters": 0,
            "hit_rate": None,
            "mean_forward_net": None,
            "mean_excess_net": None,
            "excess_ci": [None, None],
        }
    return {
        "n": len(events),
        "clusters": len({str(ev.get("ticker") or "") for ev in events}),
        "hit_rate": round(sum(float(ev.get("excess_net", 0.0)) > 0 for ev in events) / len(events), 6),
        "mean_forward_net": round(mean(float(ev.get("forward_net", 0.0)) for ev in events), 6),
        "mean_excess_net": round(mean(float(ev.get("excess_net", 0.0)) for ev in events), 6),
        "mean_confirm_score": round(mean(float(ev.get("close_confirm_score", 0.0)) for ev in events), 6),
        "excess_ci": _cluster_ci(events),
    }


def _overnight_return_maps(frames: dict[str, pd.DataFrame]) -> tuple[dict[tuple[str, str], float], dict[str, float]]:
    """Return event-date -> next-open return and per-symbol unconditional baseline."""
    by_event: dict[tuple[str, str], float] = {}
    baselines: dict[str, float] = {}
    for symbol, frame in frames.items():
        if frame is None or frame.empty or "Close" not in frame or "Open" not in frame:
            continue
        clean = frame.dropna(subset=["Close", "Open"]).copy()
        if len(clean) < 3:
            continue
        close = pd.to_numeric(clean["Close"], errors="coerce")
        next_open = pd.to_numeric(clean["Open"], errors="coerce").shift(-1)
        ret = next_open / close - 1.0
        valid = ret.replace([float("inf"), float("-inf")], pd.NA).dropna()
        if valid.empty:
            continue
        baselines[str(symbol).upper()] = float(valid.mean())
        for idx, value in valid.items():
            by_event[(str(symbol).upper(), pd.Timestamp(idx).strftime("%Y-%m-%d"))] = float(value)
    return by_event, baselines


def _next_open_maps(frames: dict[str, pd.DataFrame]) -> dict[tuple[str, str], float]:
    out: dict[tuple[str, str], float] = {}
    for symbol, frame in frames.items():
        if frame is None or frame.empty or "Open" not in frame:
            continue
        clean = frame.dropna(subset=["Open"]).copy()
        next_open = pd.to_numeric(clean["Open"], errors="coerce").shift(-1)
        for idx, value in next_open.dropna().items():
            out[(str(symbol).upper(), pd.Timestamp(idx).strftime("%Y-%m-%d"))] = float(value)
    return out


def _decision_baselines(
    feature_maps: dict[str, dict[tuple[str, str], dict[str, Any]]],
    next_open: dict[tuple[str, str], float],
    *,
    round_trip_cost: float,
) -> dict[tuple[str, str], float]:
    """Per-symbol/time unconditional decision->next-open net return baseline."""
    values: dict[tuple[str, str], list[float]] = {}
    for decision_time, fmap in feature_maps.items():
        for key, feat in fmap.items():
            symbol, _day = key
            exit_price = next_open.get(key)
            entry = float(feat.get("decision_price") or 0.0)
            if exit_price is None or entry <= 0:
                continue
            values.setdefault((symbol, decision_time), []).append(exit_price / entry - 1.0 - round_trip_cost)
    return {key: mean(vals) for key, vals in values.items() if vals}


def _apply_overnight_outcomes(
    events: list[dict[str, Any]],
    frames: dict[str, pd.DataFrame],
    *,
    round_trip_cost: float,
) -> list[dict[str, Any]]:
    returns, baselines = _overnight_return_maps(frames)
    out: list[dict[str, Any]] = []
    for ev in events:
        symbol = str(ev.get("ticker") or "").upper()
        day = str(ev.get("date"))
        if (symbol, day) not in returns or symbol not in baselines:
            continue
        forward = returns[(symbol, day)]
        excess = forward - baselines[symbol]
        row = dict(ev)
        row["forward_return"] = forward
        row["excess_return"] = excess
        row["forward_net"] = forward - round_trip_cost
        row["excess_net"] = excess - round_trip_cost
        out.append(row)
    return out


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Databento 尾盘确认胜率实验",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Universe: {report['universe']} | Symbols: {report['symbols_returned']}/{report['symbols_requested']}",
        f"- Daily window: {report['daily_start']} .. {report['end']}",
        f"- Intraday window: {report['intraday_start']} .. {report['end']}",
        f"- Top quantile: {report['top_quantile']:.0%} | Cost: {report['cost_bps']}bps/side",
        "",
        "| Horizon | Group | n | Symbols | Hit rate | Mean excess net | 95% CI | Mean close score |",
        "| ---: | --- | ---: | ---: | ---: | ---: | --- | ---: |",
    ]
    for horizon, groups in report["results"].items():
        for name, stats in groups.items():
            ci = stats.get("excess_ci") or [None, None]
            fmt = lambda v: "--" if v is None else f"{float(v):.2%}"
            mean_score = stats.get("mean_confirm_score")
            mean_score_text = "--" if mean_score is None else f"{float(mean_score):.3f}"
            lines.append(
                f"| {horizon} | {name} | {stats.get('n')} | {stats.get('clusters')} | "
                f"{fmt(stats.get('hit_rate'))} | {fmt(stats.get('mean_excess_net'))} | "
                f"[{fmt(ci[0])}, {fmt(ci[1])}] | "
                f"{mean_score_text} |"
            )
    lines.extend([
        "",
        "## 判定口径",
        "",
        "- 只研究现有 `pullback_hv` 高分事件，不改变主信号。",
        "- `confirmed` = 收盘位于日内较高位置、收盘价高于当日 VWAP、尾盘 30 分钟修复为正、尾盘成交占比合理。",
        "- 胜率 = 扣交易成本后，未来 N 日收益是否跑赢该股票自身无条件历史基线。",
        "- 本结果是研究实验，不代表确定性买入建议，也未写入 active calibration。",
    ])
    decision_results = report.get("decision_time_results") or {}
    if decision_results:
        lines.extend([
            "",
            "## 决策时间点：按该时间买入，次日开盘卖出",
            "",
            "| ET 时间 | 北京时间 | 分组 | n | Symbols | Hit rate | Mean excess net | 95% CI |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | --- |",
        ])
        for decision_time, payload in decision_results.items():
            bj = payload.get("beijing_time") or ""
            for name in ("all_top_pullback_hv", "strength_confirmed", "weak_reversion", "low_strength"):
                stats = payload.get(name) or {}
                ci = stats.get("excess_ci") or [None, None]
                fmt = lambda v: "--" if v is None else f"{float(v):.2%}"
                lines.append(
                    f"| {decision_time} | {bj} | {name} | {stats.get('n')} | {stats.get('clusters')} | "
                    f"{fmt(stats.get('hit_rate'))} | {fmt(stats.get('mean_excess_net'))} | "
                    f"[{fmt(ci[0])}, {fmt(ci[1])}] |"
                )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Databento close-confirmation experiment for pullback_hv.")
    parser.add_argument("--universe", default="spx")
    parser.add_argument("--max-symbols", type=int, default=100)
    parser.add_argument("--dataset", default="EQUS.MINI")
    parser.add_argument("--intraday-period", default="180d")
    parser.add_argument("--daily-period", default="1y")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--horizons", default="5,10")
    parser.add_argument("--include-overnight", action="store_true", default=True)
    parser.add_argument("--decision-times-et", default="14:30,15:00,15:30,15:45")
    parser.add_argument("--top-quantile", type=float, default=0.70)
    parser.add_argument("--cooldown-days", type=int, default=5)
    parser.add_argument("--cost-bps", type=float, default=-1.0)
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--output", default=str(AGENT_DIR / "runs" / "databento_close_confirmation_experiment.json"))
    args = parser.parse_args()

    CONFIG["universe_snapshot_dir"] = str(AGENT_DIR / "data_cache" / "universe_snapshots")
    symbols, universe_source, _ = resolve_universe(args.universe)
    symbols = symbols[: args.max_symbols] if args.max_symbols else symbols
    daily_start = _period_start(args.daily_period)
    intraday_start = _period_start(args.intraday_period)
    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    cost_bps = args.cost_bps if args.cost_bps >= 0 else cost_model.equity_one_way_bps()
    round_trip_cost = 2.0 * cost_bps / 10000.0

    print(f"[daily] {len(symbols)} symbols {daily_start}..{args.end}", flush=True)
    daily_frames, _daily_sources, daily_meta = _load_databento_panel(
        dataset=args.dataset,
        schema="ohlcv-1d",
        symbols=symbols,
        start=daily_start,
        end=args.end,
        cache_dir=Path(args.cache_dir) / "daily",
        use_cache=not args.no_cache,
    )
    print(f"[1m] {len(symbols)} symbols {intraday_start}..{args.end}", flush=True)
    raw_1m, intraday_meta = _load_1m_raw(
        dataset=args.dataset,
        symbols=symbols,
        start=intraday_start,
        end=args.end,
        cache_dir=Path(args.cache_dir) / "1m",
        use_cache=not args.no_cache,
    )
    features = _build_close_features(raw_1m)
    decision_times = [x.strip() for x in args.decision_times_et.split(",") if x.strip()]
    decision_feature_maps = {
        t: _decision_features_for_time(raw_1m, t)
        for t in decision_times
    }
    print(f"[features] symbol-days={len(features)}", flush=True)

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "universe": args.universe,
        "universe_source": universe_source,
        "dataset": args.dataset,
        "symbols_requested": len(symbols),
        "symbols_returned": len(daily_frames),
        "daily_start": daily_start,
        "intraday_start": intraday_start,
        "end": args.end,
        "top_quantile": args.top_quantile,
        "cost_bps": cost_bps,
        "daily_meta": daily_meta,
        "intraday_meta": intraday_meta,
        "results": {},
    }

    for horizon in horizons:
        events: list[dict[str, Any]] = []
        for symbol, frame in daily_frames.items():
            events.extend(
                _collect_symbol_events(
                    symbol,
                    frame,
                    horizon,
                    min_launch_score=0.0,
                    min_tunnel_score=0.0,
                    cooldown_days=args.cooldown_days,
                )
            )
        events = [ev for ev in events if str(ev.get("date")) >= intraday_start and ev.get("pullback_hv_score") is not None]
        scores = sorted(float(ev["pullback_hv_score"]) for ev in events if math.isfinite(float(ev["pullback_hv_score"])))
        if not scores:
            report["results"][f"{horizon}d"] = {}
            continue
        threshold = scores[min(len(scores) - 1, max(0, int(len(scores) * args.top_quantile)))]
        enriched: list[dict[str, Any]] = []
        for ev in events:
            if float(ev.get("pullback_hv_score", 0.0)) < threshold:
                continue
            key = (str(ev.get("ticker") or "").upper(), str(ev.get("date")))
            feat = features.get(key)
            if not feat:
                continue
            row = {**ev, **feat}
            row["forward_net"] = float(ev.get("forward_return") or 0.0) - round_trip_cost
            row["excess_net"] = float(ev.get("excess_return") or 0.0) - round_trip_cost
            enriched.append(row)
        confirmed = [ev for ev in enriched if ev.get("close_confirmed")]
        strong = [ev for ev in enriched if ev.get("close_strong_confirmed")]
        not_confirmed = [ev for ev in enriched if not ev.get("close_confirmed")]
        low_score = [ev for ev in enriched if float(ev.get("close_confirm_score", 0.0)) < 0.50]
        report["results"][f"{horizon}d"] = {
            "all_top_pullback_hv": _summarize(enriched),
            "close_confirmed": _summarize(confirmed),
            "close_strong_confirmed": _summarize(strong),
            "not_close_confirmed": _summarize(not_confirmed),
            "low_close_score": _summarize(low_score),
            "meta": {
                "raw_events": len(events),
                "score_threshold": round(threshold, 6),
                "events_with_intraday": len(enriched),
            },
        }
        print(
            f"[{horizon}d] raw={len(events)} top+1m={len(enriched)} confirmed={len(confirmed)} "
            f"strong={len(strong)} threshold={threshold:.4f}",
            flush=True,
        )

    if args.include_overnight:
        overnight_events: list[dict[str, Any]] = []
        for symbol, frame in daily_frames.items():
            overnight_events.extend(
                _collect_symbol_events(
                    symbol,
                    frame,
                    1,
                    min_launch_score=0.0,
                    min_tunnel_score=0.0,
                    cooldown_days=args.cooldown_days,
                )
            )
        overnight_events = [
            ev for ev in overnight_events
            if str(ev.get("date")) >= intraday_start and ev.get("pullback_hv_score") is not None
        ]
        overnight_events = _apply_overnight_outcomes(
            overnight_events,
            daily_frames,
            round_trip_cost=round_trip_cost,
        )
        scores = sorted(float(ev["pullback_hv_score"]) for ev in overnight_events if math.isfinite(float(ev["pullback_hv_score"])))
        if scores:
            threshold = scores[min(len(scores) - 1, max(0, int(len(scores) * args.top_quantile)))]
            enriched = []
            for ev in overnight_events:
                if float(ev.get("pullback_hv_score", 0.0)) < threshold:
                    continue
                key = (str(ev.get("ticker") or "").upper(), str(ev.get("date")))
                feat = features.get(key)
                if not feat:
                    continue
                enriched.append({**ev, **feat})
            confirmed = [ev for ev in enriched if ev.get("close_confirmed")]
            strong = [ev for ev in enriched if ev.get("close_strong_confirmed")]
            not_confirmed = [ev for ev in enriched if not ev.get("close_confirmed")]
            low_score = [ev for ev in enriched if float(ev.get("close_confirm_score", 0.0)) < 0.50]
            report["results"]["overnight"] = {
                "all_top_pullback_hv": _summarize(enriched),
                "close_confirmed": _summarize(confirmed),
                "close_strong_confirmed": _summarize(strong),
                "not_close_confirmed": _summarize(not_confirmed),
                "low_close_score": _summarize(low_score),
                "meta": {
                    "raw_events": len(overnight_events),
                    "score_threshold": round(threshold, 6),
                    "events_with_intraday": len(enriched),
                    "outcome": "buy close, sell next open",
                },
            }
            print(
                f"[overnight] raw={len(overnight_events)} top+1m={len(enriched)} confirmed={len(confirmed)} "
                f"strong={len(strong)} threshold={threshold:.4f}",
                flush=True,
            )

        next_open = _next_open_maps(daily_frames)
        baselines = _decision_baselines(
            decision_feature_maps,
            next_open,
            round_trip_cost=round_trip_cost,
        )
        decision_results: dict[str, Any] = {}
        for decision_time, fmap in decision_feature_maps.items():
            if not fmap or not scores:
                continue
            enriched = []
            for ev in overnight_events:
                if float(ev.get("pullback_hv_score", 0.0)) < threshold:
                    continue
                key = (str(ev.get("ticker") or "").upper(), str(ev.get("date")))
                feat = fmap.get(key)
                exit_price = next_open.get(key)
                entry = float((feat or {}).get("decision_price") or 0.0)
                baseline = baselines.get((key[0], decision_time))
                if not feat or exit_price is None or entry <= 0 or baseline is None:
                    continue
                forward_net = exit_price / entry - 1.0 - round_trip_cost
                row = {**ev, **feat}
                row["forward_net"] = forward_net
                row["excess_net"] = forward_net - baseline
                enriched.append(row)
            strength = [ev for ev in enriched if ev.get("decision_strength_confirmed")]
            weak_reversion = [ev for ev in enriched if ev.get("decision_weak_reversion")]
            low_strength = [ev for ev in enriched if float(ev.get("decision_strength_score", 0.0)) < 0.50]
            decision_results[decision_time] = {
                "beijing_time": _beijing_time_label(decision_time),
                "all_top_pullback_hv": _summarize(enriched),
                "strength_confirmed": _summarize(strength),
                "weak_reversion": _summarize(weak_reversion),
                "low_strength": _summarize(low_strength),
                "meta": {"events_with_decision_price": len(enriched), "score_threshold": round(threshold, 6)},
            }
            print(
                f"[decision {decision_time} ET / {_beijing_time_label(decision_time)} BJ] "
                f"all={len(enriched)} strength={len(strength)} weak_reversion={len(weak_reversion)}",
                flush=True,
            )
        report["decision_time_results"] = decision_results

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    output.with_suffix(".md").write_text(_markdown(report), encoding="utf-8")
    print(f"Wrote {output}")
    print(f"Wrote {output.with_suffix('.md')}")


if __name__ == "__main__":
    main()
