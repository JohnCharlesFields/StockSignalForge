#!/usr/bin/env python3
"""Small, pre-registered OOS check; NEVER interprets stock returns as option P/L.

The fixed seven-name universe is a research cohort, not a PIT reconstruction.
Candidate factors/thresholds and the primary five-day horizon are chosen before
looking at validation/test outcomes. A ten-session gap prevents horizon overlap.
"""

from __future__ import annotations

import argparse
import io
import json
import random
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

AGENT = Path(__file__).resolve().parents[1]
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

from consensus_signal_service import consensus_feature_frame  # noqa: E402
from cost_model import equity_round_trip_cost  # noqa: E402
from market_data_service import _CACHE_ROOT, external_data_scope, get_daily_history  # noqa: E402
from scripts.backtest_leader_long_options import MAGNIFICENT_SEVEN  # noqa: E402
from src.factors.registry import get_default_registry  # noqa: E402

VIX_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
FACTORS = ("qlib158_roc20", "qlib158_rsv20")
HORIZONS = (1, 3, 5, 10)
PRIMARY_HORIZON = 5
FACTOR_PERCENTILE = 0.60
MIN_VALIDATION = 12
MIN_TEST = 25


def _vix_history(*, download: bool) -> pd.Series:
    path = _CACHE_ROOT / "macro" / "cboe_vix_history.csv"
    if download:
        response = requests.get(VIX_URL, timeout=25)
        response.raise_for_status()
        raw = response.content.decode("utf-8-sig")
        if not raw.startswith("DATE,OPEN,HIGH,LOW,CLOSE") or len(raw) > 3_000_000:
            raise ValueError("Cboe VIX history schema or size changed")
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(raw, encoding="utf-8")
        temp.replace(path)
    if not path.exists():
        return pd.Series(dtype=float)
    table = pd.read_csv(io.StringIO(path.read_text(encoding="utf-8-sig")), parse_dates=["DATE"])
    return pd.Series(pd.to_numeric(table["CLOSE"], errors="coerce").values, index=table["DATE"].dt.normalize())


def _histories() -> dict[str, pd.DataFrame]:
    frames = {}
    with external_data_scope(False):
        for symbol in (*MAGNIFICENT_SEVEN, "SPY"):
            frame, _ = get_daily_history(symbol, period="5y", allow_yfinance_fallback=False)
            if frame is not None and len(frame) >= 500:
                frames[symbol] = frame.sort_index()
    return frames


def _panel(frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    return {field.lower(): pd.concat({symbol: pd.to_numeric(frame[field], errors="coerce")
                                      for symbol, frame in frames.items() if symbol != "SPY"}, axis=1)
            for field in ("Open", "High", "Low", "Close", "Volume")}


def _split(day: date) -> str | None:
    if date(2022, 1, 3) <= day <= date(2023, 12, 15):
        return "train"
    if date(2024, 1, 2) <= day <= date(2024, 12, 13):
        return "validation"
    if date(2025, 1, 2) <= day <= date(2026, 9, 9):
        return "test"
    return None


def _cluster_delta(rows: list[dict], key: str, horizon: int, *, n_boot: int = 1000) -> dict:
    eligible = [row for row in rows if row.get(f"ret_{horizon}") is not None and row.get(key) is not None]
    selected = [row for row in eligible if row[key]]
    if not selected or not eligible:
        return {"n": len(selected), "mean_net": None, "baseline_mean_net": None, "delta": None, "ci95_delta": None}
    def delta(sample: list[dict]) -> float | None:
        all_values = [row[f"ret_{horizon}"] for row in sample]
        chosen = [row[f"ret_{horizon}"] for row in sample if row[key]]
        return float(np.mean(chosen) - np.mean(all_values)) if chosen and all_values else None
    clusters: dict[str, list[dict]] = {}
    for row in eligible:
        clusters.setdefault(row["signal_date"][:7], []).append(row)
    rng = random.Random(73)
    keys = sorted(clusters)
    draws = []
    for _ in range(n_boot):
        sampled = [row for month in (rng.choice(keys) for _ in keys) for row in clusters[month]]
        value = delta(sampled)
        if value is not None:
            draws.append(value)
    ci = ([float(np.quantile(draws, 0.025)), float(np.quantile(draws, 0.975))]
          if len(selected) >= MIN_TEST and len(keys) >= 6 and len(draws) >= 100 else None)
    return {"n": len(selected), "symbols": len({row["symbol"] for row in selected}),
            "mean_net": round(float(np.mean([row[f"ret_{horizon}"] for row in selected])), 6),
            "baseline_mean_net": round(float(np.mean([row[f"ret_{horizon}"] for row in eligible])), 6),
            "delta": round(float(delta(eligible)), 6),
            "ci95_delta": [round(v, 6) for v in ci] if ci else None,
            "hit": round(sum(row[f"ret_{horizon}"] > 0 for row in selected) / len(selected), 4)}


def run(*, download_vix: bool = False) -> dict:
    histories = _histories()
    if len(histories) < len(MAGNIFICENT_SEVEN) + 1:
        return {"status": "insufficient_history", "available_symbols": sorted(histories)}
    vix = _vix_history(download=download_vix)
    panel = _panel(histories)
    registry = get_default_registry()
    ranks = {aid: registry.compute(aid, panel).rank(axis=1, pct=True) for aid in FACTORS}
    cost = float(equity_round_trip_cost())
    events = []
    for symbol in MAGNIFICENT_SEVEN:
        stock = histories[symbol]
        feature = consensus_feature_frame(stock, histories["SPY"])
        if feature.empty:
            continue
        last_event = -100
        for position in range(220, len(stock) - max(HORIZONS)):
            day = pd.Timestamp(stock.index[position]).date()
            split = _split(day)
            if split is None or position - last_event < 10:
                continue
            net = feature["net_consensus"].reindex(stock.index).iloc[position]
            if pd.isna(net) or abs(float(net)) <= 0.12:
                continue
            side = 1 if net > 0 else -1
            final_exit_day = pd.Timestamp(stock.index[position + max(HORIZONS)]).date()
            if split == "train" and final_exit_day >= date(2024, 1, 2):
                continue
            if split == "validation" and final_exit_day >= date(2025, 1, 2):
                continue
            entry = float(stock["Open"].iloc[position + 1])
            if not np.isfinite(entry) or entry <= 0:
                continue
            event = {"symbol": symbol, "signal_date": day.isoformat(), "split": split,
                     "side": "CALL_proxy" if side == 1 else "PUT_proxy", "vix": None,
                     "panic_high": None, "panic_low": None}
            stamp = pd.Timestamp(stock.index[position]).normalize()
            if stamp in vix.index and np.isfinite(vix.loc[stamp]):
                value = float(vix.loc[stamp])
                event.update(vix=value, panic_high=value >= 35, panic_low=value < 20)
            for aid, frame in ranks.items():
                rank = frame.get(symbol, pd.Series(dtype=float)).reindex(stock.index).iloc[position]
                event[aid] = bool(rank >= FACTOR_PERCENTILE if side == 1 else rank <= 1 - FACTOR_PERCENTILE) if pd.notna(rank) else None
            for horizon in HORIZONS:
                exit_price = float(stock["Close"].iloc[position + horizon])
                event[f"ret_{horizon}"] = round(side * (exit_price / entry - 1) - cost, 6) if np.isfinite(exit_price) else None
            events.append(event)
            last_event = position

    by_split = {name: [row for row in events if row["split"] == name] for name in ("train", "validation", "test")}
    validation = {aid: _cluster_delta(by_split["validation"], aid, PRIMARY_HORIZON) for aid in FACTORS}
    locked = [aid for aid in FACTORS if validation[aid]["n"] >= MIN_VALIDATION
              and validation[aid]["delta"] is not None and validation[aid]["delta"] > 0]
    test = {aid: {str(h): _cluster_delta(by_split["test"], aid, h) for h in HORIZONS} for aid in locked}
    panic = {key: {str(h): _cluster_delta(by_split["test"], key, h) for h in HORIZONS}
             for key in ("panic_high", "panic_low")}
    promotions = []
    for aid in locked:
        result = test[aid][str(PRIMARY_HORIZON)]
        # Even a statistically positive stock-direction proxy cannot certify option P/L.
        if result["n"] >= MIN_TEST and result.get("ci95_delta") and result["ci95_delta"][0] > 0:
            promotions.append({"factor": aid, "stock_direction_candidate_only": True})
    return {
        "status": "completed", "generated_at": datetime.now(timezone.utc).isoformat(),
        "universe": list(MAGNIFICENT_SEVEN), "universe_warning": "固定七姐妹，非历史PIT龙头名单；不推广到全市场。",
        "outcome": "次日开盘进入，持有1/3/5/10交易日至收盘的正股方向收益，扣正股往返成本；不是期权收益。",
        "cost_fraction": cost, "split_policy": "train 2022-2023-12-15 / validation 2024-01-02..12-13 / test 2025-01-02..2026-09-09; all 10-day exits must precede the next split",
        "factors_preselected": list(FACTORS), "factor_percentile": FACTOR_PERCENTILE,
        "primary_horizon_days": PRIMARY_HORIZON, "events": {name: len(items) for name, items in by_split.items()},
        "vix_source": VIX_URL if not vix.empty else "unavailable",
        "validation": validation, "locked_after_validation": locked, "test": test,
        "panic_test": panic, "stock_direction_candidates": promotions,
        "production_enabled": False,
        "reason": "期权买入收益仍缺真实历史Ask/Bid及IV路径；Alpha与恐慌均不接入生产排序。",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--download-vix", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(download_vix=args.download_vix)
    raw = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(raw, encoding="utf-8")
    print(raw)


if __name__ == "__main__":
    main()
