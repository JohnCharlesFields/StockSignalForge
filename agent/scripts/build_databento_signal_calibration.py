#!/usr/bin/env python3
"""Build a Databento-backed signal win-rate calibration report.

This script is intentionally additive: it does not replace the existing
yfinance/Tiingo/Massive routing and does not activate any calibration curve
unless ``--make-active`` is passed.  It uses Databento OHLCV daily bars as a
cleaner price source, then reuses the project's existing R1 calibration logic:

    signal score -> net-of-cost forward excess return -> empirical win rate

The API key is read from ``DATABENTO_API_KEY``.  Never write the key to disk.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_DIR = SCRIPT_DIR.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from app_database import signal_calibration_upsert  # noqa: E402
from market_data_service import aggregate_data_quality  # noqa: E402
from scripts.research_signal_framework_backtest import (  # noqa: E402
    _collect_symbol_events,
)
from scripts.screening_framework_v2_optimized import CONFIG, resolve_universe  # noqa: E402
from signal_calibration import build_calibration  # noqa: E402
from scripts.build_signal_calibration import SIGNAL_SCORE_FIELD  # noqa: E402
import cost_model  # noqa: E402


DEFAULT_DATASET = "EQUS.MINI"
DEFAULT_SCHEMA = "ohlcv-1d"
DEFAULT_CACHE_DIR = AGENT_DIR / "data_cache" / "databento_ohlcv"


def _parse_period_start(period: str) -> str:
    today = date.today()
    value = str(period or "1y").strip().lower()
    if value.endswith("y"):
        days = 366 * int(value[:-1] or 1)
    elif value.endswith("mo"):
        days = 31 * int(value[:-2] or 1)
    elif value.endswith("d"):
        days = int(value[:-1] or 1)
    else:
        days = 366
    return (today - timedelta(days=days)).isoformat()


def _cache_key(dataset: str, schema: str, start: str, end: str, symbols: list[str]) -> str:
    payload = json.dumps(
        {"dataset": dataset, "schema": schema, "start": start, "end": end, "symbols": symbols},
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _normalize_databento_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    result = frame.copy()
    if "ts_event" in result.columns:
        result.index = pd.to_datetime(result["ts_event"])
    result.index = pd.DatetimeIndex(pd.to_datetime(result.index)).tz_localize(None).normalize()
    result.index.name = "Date"
    rename = {
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    }
    result = result.rename(columns=rename)
    for column in ("Open", "High", "Low", "Close", "Volume"):
        if column not in result:
            result[column] = 0.0 if column == "Volume" else float("nan")
        result[column] = pd.to_numeric(result[column], errors="coerce")
    return result[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"]).sort_index()


def _load_databento_panel(
    *,
    dataset: str,
    schema: str,
    symbols: list[str],
    start: str,
    end: str,
    cache_dir: Path,
    use_cache: bool,
) -> tuple[dict[str, pd.DataFrame], dict[str, str], dict[str, Any]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{_cache_key(dataset, schema, start, end, symbols)}.parquet"
    if use_cache and cache_file.exists():
        raw = pd.read_parquet(cache_file)
        cache_hit = True
    else:
        try:
            import databento as db
        except ImportError as exc:
            raise RuntimeError("databento package is not installed. Run: python -m pip install databento") from exc
        if not os.environ.get("DATABENTO_API_KEY"):
            raise RuntimeError("DATABENTO_API_KEY is not set")
        client = db.Historical()
        store = client.timeseries.get_range(
            dataset=dataset,
            schema=schema,
            symbols=symbols,
            stype_in="raw_symbol",
            start=start,
            end=end,
        )
        raw = store.to_df()
        if not raw.empty:
            raw.to_parquet(cache_file)
        cache_hit = False

    frames: dict[str, pd.DataFrame] = {}
    sources: dict[str, str] = {}
    if raw is None or raw.empty or "symbol" not in raw.columns:
        return frames, sources, {"cache_hit": cache_hit, "cache_file": str(cache_file), "raw_rows": 0}
    for symbol, group in raw.groupby("symbol"):
        sym = str(symbol).upper()
        normalized = _normalize_databento_frame(group)
        if not normalized.empty:
            frames[sym] = normalized
            sources[sym] = f"databento:{dataset}:{schema}"
    return frames, sources, {
        "cache_hit": cache_hit,
        "cache_file": str(cache_file),
        "raw_rows": int(len(raw)),
        "symbols_returned": len(frames),
    }


def _collect_events_from_frames(
    frames: dict[str, pd.DataFrame],
    *,
    horizon: int,
    cooldown_days: int,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for symbol, frame in sorted(frames.items()):
        try:
            events.extend(
                _collect_symbol_events(
                    symbol,
                    frame,
                    horizon,
                    min_launch_score=0.0,
                    min_tunnel_score=0.0,
                    cooldown_days=cooldown_days,
                )
            )
        except Exception:
            continue
    return events


def _fmt_pct(value: Any) -> str:
    try:
        return f"{float(value):.2%}"
    except (TypeError, ValueError):
        return "--"


def _edge_summary(edge: dict[str, Any]) -> str:
    top = edge.get("top_bucket") or {}
    ci = top.get("excess_ci") or [None, None]
    return (
        f"validated={bool(edge.get('validated'))}, "
        f"spread={_fmt_pct(edge.get('spread'))}, "
        f"top_n={top.get('n')}, "
        f"top_excess={_fmt_pct(top.get('mean_excess_net'))}, "
        f"CI=[{_fmt_pct(ci[0])}, {_fmt_pct(ci[1])}]"
    )


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Databento 实股信号胜率回测",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Dataset: {report['dataset']} / {report['schema']}",
        f"- Universe: {report['universe']} | Symbols requested: {report['symbols_requested']} | Returned: {report['symbols_returned']}",
        f"- Window: {report['start']} .. {report['end']} | Cost: {report['cost_bps']}bps/side",
        f"- Cache hit: {report['databento_meta'].get('cache_hit')} | Raw rows: {report['databento_meta'].get('raw_rows')}",
        "",
        "| Signal | Horizon | Events | Symbols | Overall hit | Mean excess net | Edge verdict | Top bucket | Calibration ID |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for row in report["curves"]:
        global_stats = row["global"]
        edge = global_stats.get("edge") or {}
        top = edge.get("top_bucket") or {}
        ci = top.get("excess_ci") or [None, None]
        verdict = "通过" if edge.get("validated") else "未通过"
        lines.append(
            f"| {row['signal_type']} | {row['horizon_days']}d | {global_stats.get('events')} | "
            f"{global_stats.get('clusters')} | {_fmt_pct(global_stats.get('hit_rate'))} | "
            f"{_fmt_pct(global_stats.get('mean_excess_net'))} | {verdict} | "
            f"n={top.get('n')} / excess={_fmt_pct(top.get('mean_excess_net'))} / "
            f"CI=[{_fmt_pct(ci[0])}, {_fmt_pct(ci[1])}] | {row['calibration_id']} |"
        )
    lines.extend([
        "",
        "## 口径说明",
        "",
        "- 胜率不是涨跌胜率，而是：扣除交易成本后，未来 N 日收益是否跑赢该股票自身无条件历史基线。",
        "- 事件由现有 `_collect_symbol_events` 生成，沿用 launch / daily_tunnel / pullback_hv 的同一套信号分数。",
        "- 本脚本默认不激活曲线；需要上线时显式传入 `--make-active`。",
        "- Databento 只替换价格数据源，不解决指数历史成分股 PIT、财报事件 PIT 或公告时间戳问题。",
        "",
        "## 数据质量",
        "",
        "```json",
        json.dumps(report.get("data_quality", {}), ensure_ascii=False, indent=2),
        "```",
    ])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Databento-backed signal calibration backtest.")
    parser.add_argument("--universe", default="spx", help="Research universe id.")
    parser.add_argument("--symbols", default="", help="Optional comma-separated explicit symbols.")
    parser.add_argument("--max-symbols", type=int, default=50)
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA)
    parser.add_argument("--period", default="18mo")
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--horizons", default="5,10")
    parser.add_argument("--signals", default="pullback_hv,launch,daily_tunnel")
    parser.add_argument("--cooldown-days", type=int, default=5)
    parser.add_argument("--cost-bps", type=float, default=-1.0)
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--make-active", action="store_true")
    parser.add_argument("--output", default=str(AGENT_DIR / "runs" / "databento_signal_calibration.json"))
    args = parser.parse_args()

    if args.symbols.strip():
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        universe_source = "explicit"
    else:
        CONFIG["universe_snapshot_dir"] = str(AGENT_DIR / "data_cache" / "universe_snapshots")
        symbols, universe_source, _ = resolve_universe(args.universe)
        symbols = symbols[: args.max_symbols] if args.max_symbols else symbols
    start = args.start or _parse_period_start(args.period)
    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    signals = [s.strip() for s in args.signals.split(",") if s.strip() in SIGNAL_SCORE_FIELD]
    cost_bps = args.cost_bps if args.cost_bps >= 0 else cost_model.equity_one_way_bps()

    print(
        f"[databento] dataset={args.dataset} symbols={len(symbols)} start={start} end={args.end}",
        flush=True,
    )
    frames, sources, db_meta = _load_databento_panel(
        dataset=args.dataset,
        schema=args.schema,
        symbols=symbols,
        start=start,
        end=args.end,
        cache_dir=Path(args.cache_dir),
        use_cache=not args.no_cache,
    )
    data_quality = aggregate_data_quality(sources)

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": args.dataset,
        "schema": args.schema,
        "universe": args.universe,
        "universe_source": universe_source,
        "start": start,
        "end": args.end,
        "cost_bps": cost_bps,
        "symbols_requested": len(symbols),
        "symbols_returned": len(frames),
        "databento_meta": db_meta,
        "data_quality": data_quality,
        "horizons": horizons,
        "signals": signals,
        "curves": [],
    }

    for horizon in horizons:
        raw_events = _collect_events_from_frames(frames, horizon=horizon, cooldown_days=args.cooldown_days)
        report.setdefault("meta", {})[f"{horizon}d"] = {
            "raw_events": len(raw_events),
            "symbols_with_events": len({str(ev.get("ticker") or "") for ev in raw_events}),
        }
        print(f"[databento] horizon={horizon}d events={len(raw_events)}", flush=True)
        for signal_type in signals:
            score_field = SIGNAL_SCORE_FIELD[signal_type]
            scoped = [
                {
                    "score": ev.get(score_field),
                    "forward_return": ev.get("forward_return"),
                    "excess_return": ev.get("excess_return"),
                    "ticker": ev.get("ticker"),
                }
                for ev in raw_events
                if ev.get(score_field) is not None
            ]
            curve = build_calibration(scoped, signal_type=signal_type, horizon=horizon, cost_bps=cost_bps)
            curve["global"]["data_quality"] = data_quality
            calibration_id = signal_calibration_upsert(
                signal_type=signal_type,
                horizon_days=horizon,
                curve=curve,
                universe_set=f"databento:{args.universe}",
                source=f"databento:{args.dataset}",
                event_count=curve["global"]["events"],
                cluster_count=curve["global"]["clusters"],
                cost_bps=cost_bps,
                make_active=args.make_active,
            )
            report["curves"].append(
                {
                    "signal_type": signal_type,
                    "horizon_days": horizon,
                    "calibration_id": calibration_id,
                    "make_active": bool(args.make_active),
                    "global": curve["global"],
                    "buckets": curve["buckets"],
                }
            )
            print(f"  -> {signal_type}/{horizon}d {calibration_id} {_edge_summary(curve['global'].get('edge') or {})}", flush=True)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    output.with_suffix(".md").write_text(_markdown(report), encoding="utf-8")
    print(f"\nWrote {output}")
    print(f"Wrote {output.with_suffix('.md')}")


if __name__ == "__main__":
    main()
