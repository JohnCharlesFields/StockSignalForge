"""Rebuild outcomes from cached bars, then run a purged parameter experiment."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import sqlite3

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app_database import AGENT_DIR, DB_PATH, cache_set, ensure_database
import cost_model
import market_data_service as market
from market_calendar import is_trading_day
from pullback_parameter_service import CANDIDATE_KEY, STATUS_KEY, activate_candidate, settings, train_candidate
from pullback_validation import next_session_open, shift_session


def build_dataset(path, horizon):
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("events") or []
    # Append new immutable daily forecasts so the weekly job gains new data.
    ensure_database()
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        live = conn.execute("SELECT as_of_date,symbol,created_at,forecast_json FROM predictions WHERE evaluation_version='recorded_open_v3' AND mode='live'").fetchall()
    for row in live:
        saved = json.loads(row["forecast_json"] or "{}")
        features = saved.get("features") or {}
        launch, hv = features.get("raw_launch_score"), (features.get("hv") or {}).get("hv_rise")
        if launch is None or hv is None:
            continue
        recorded = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
        signal_day = datetime.fromisoformat(row["as_of_date"]).date()
        if next_session_open(recorded, signal_day) != shift_session(signal_day, 1):
            continue
        e = features.get("playbook_enhancements") or {}
        rows.append({"ticker": row["symbol"], "date": row["as_of_date"], "launch_score": launch,
                     "hv_rise_norm": min(1, max(0, .5 + .5 * hv)), "avg_dollar_volume": (features.get("liquidity") or {}).get("adv"),
                     "market_ok": bool(e.get("market_liquid_rs_top40")), "liquid": True, "rs_filter": True,
                     "stock_vs_sector_strong": bool(e.get("stock_stronger_than_industry"))})
    by_symbol = {}
    for row in rows:
        by_symbol.setdefault(str(row.get("ticker") or row.get("symbol")), []).append(row)
    spy = market._read_daily_cache("SPY")
    if spy.empty:
        raise ValueError("SPY cache unavailable")
    built, skipped = [], 0
    for symbol, events in by_symbol.items():
        frame = market._read_daily_cache(symbol)
        if frame.empty or "Open" not in frame:
            skipped += len(events)
            continue
        frame = frame[~frame.index.duplicated()].sort_index()
        op = pd.to_numeric(frame.Open, errors="coerce")
        close = pd.to_numeric(frame.Close, errors="coerce")
        gross = op.shift(-(horizon + 1)) / op.shift(-1) - 1
        known_baseline = (op.shift(-horizon) / op - 1).shift(horizon).rolling(252, min_periods=60).mean()
        paired = pd.concat([close.pct_change(), spy.Close.pct_change()], axis=1, sort=True)
        beta = paired.iloc[:, 0].rolling(60, min_periods=40).cov(paired.iloc[:, 1]) / paired.iloc[:, 1].rolling(60, min_periods=40).var()
        locations = {d.date().isoformat(): i for i, d in enumerate(frame.index)}
        for event in events:
            day = str(event.get("date") or "")[:10]
            i = locations.get(day)
            if i is None or i + horizon + 1 >= len(frame) or not is_trading_day(frame.index[i].date()):
                skipped += 1
                continue
            entry_day, exit_day = shift_session(frame.index[i].date(), 1), shift_session(frame.index[i].date(), horizon + 1)
            if frame.index[i + 1].date() != entry_day or frame.index[i + horizon + 1].date() != exit_day:
                skipped += 1
                continue
            if any(frame.index[i + n].date() != shift_session(frame.index[i].date(), n) for n in range(1, horizon + 2)):
                skipped += 1
                continue
            entry, exit_ = pd.Timestamp(entry_day), pd.Timestamp(exit_day)
            if entry not in spy.index or exit_ not in spy.index or not np.isfinite(known_baseline.iloc[i]):
                skipped += 1
                continue
            raw = float(gross.iloc[i])
            price = float(op.iloc[i + 1])
            if not np.isfinite(raw) or price <= 0:
                skipped += 1
                continue
            benchmark = float(spy.loc[exit_, "Open"] / spy.loc[entry, "Open"] - 1)
            b = float(beta.get(frame.index[i], np.nan))
            cost = cost_model.equity_round_trip_cost(price=price, avg_dollar_volume=event.get("avg_dollar_volume"))
            launch, hv = event.get("launch_score"), event.get("hv_rise_norm")
            if launch is None or hv is None or not np.isfinite(launch) or not np.isfinite(hv):
                skipped += 1
                continue
            group = "mean_reversion" if launch <= .55 else "trend_pullback" if event.get("trend_filter") and event.get("normal_pullback") else "other"
            built.append({"symbol": symbol, "date": day, "exit_date": exit_day.isoformat(), "group": group,
                          "entry_date": entry_day.isoformat(), "entry_open": price, "exit_open": float(op.iloc[i + horizon + 1]), "cost": cost,
                          "oversold": 1 - min(1, max(0, float(launch))), "hv_norm": min(1, max(0, float(hv))),
                          "market_tag": int(bool(event.get("market_ok") and event.get("liquid") and event.get("rs_filter"))),
                          "sector_tag": int(bool(event.get("stock_vs_sector_strong"))), "net_return": raw - cost,
                          "net_excess": raw - float(known_baseline.iloc[i]) - cost,
                          "beta_adjusted_alpha": raw - b * benchmark - cost if np.isfinite(b) else None})
    provenance = {"source_path": str(path), "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                  "price_source": "existing_daily_cache", "label": "next_session_open_to_open_5d_net",
                  "feature_source": "previously_inspected_replay", "point_in_time_universe": False,
                  "corporate_actions_verified": False, "unseen_holdout": False, "account_backtest_verified": False,
                  "events_used": len(built), "events_skipped": skipped,
                  "immutable_live_rows_appended": len(live),
                  "survivorship_note": "archived event pool; no historical membership certification"}
    return built, provenance


def account_backtest(rows, predictions, config):
    """Cash-limited long-only daily bars; integer shares and fixed-horizon exits."""
    orders = {}
    for row, expected in zip(rows, predictions):
        if expected >= config.get("entry_threshold", 0):
            orders.setdefault(row["entry_date"], []).append((float(expected), row))
    if not rows:
        return {"available": False}
    frames = {sym: market._read_daily_cache(sym) for sym in {r["symbol"] for r in rows}}
    start, end = min(r["entry_date"] for r in rows), max(r["exit_date"] for r in rows)
    sessions = [d for d in pd.date_range(start, end) if is_trading_day(d.date())]
    initial = cash = 2000.0
    held, curve, trades = {}, [], []
    for stamp in sessions:
        day = stamp.date().isoformat()
        for symbol, position in list(held.items()):
            if position["exit_date"] == day:
                proceeds = position["qty"] * position["exit_open"] * (1 - position["cost"] / 2)
                cash += proceeds
                trades.append(proceeds / position["spent"] - 1)
                del held[symbol]
        marked = sum(p["qty"] * float(frames[symbol].loc[stamp, "Open"]) for symbol, p in held.items())
        equity = cash + marked
        for _, row in sorted(orders.get(day, []), key=lambda x: (-x[0], x[1]["symbol"])):
            if len(held) >= int(config["top_n"]):
                break
            if row["symbol"] in held:
                continue
            allocation = min(cash, equity / int(config["top_n"]))
            qty = int(allocation / (row["entry_open"] * (1 + row["cost"] / 2)))
            if qty < 1:
                continue
            spent = qty * row["entry_open"] * (1 + row["cost"] / 2)
            cash -= spent
            held[row["symbol"]] = {**row, "qty": qty, "spent": spent}
        total = cash + sum(p["qty"] * float(frames[symbol].loc[stamp, "Close"]) for symbol, p in held.items())
        curve.append({"date": day, "equity": total, "cash": cash, "positions": len(held)})
    equity = np.array([initial] + [p["equity"] for p in curve])
    drawdown = equity / np.maximum.accumulate(equity) - 1
    return {"available": True, "initial_capital": initial, "ending_equity": float(equity[-1]),
            "net_return": float(equity[-1] / initial - 1), "max_drawdown": float(drawdown.min()),
            "closed_trades": len(trades), "net_win_rate": float(np.mean(np.array(trades) > 0)) if trades else None,
            "equity_curve": curve, "rules": "integer shares, equal equity slots, cash-constrained, one position per ticker, fixed 5-session open exit; no price stops"}


def run(output=None):
    config = settings()
    path = AGENT_DIR / config["dataset"]
    rows, provenance = build_dataset(path, int(config["training_horizon_days"]))
    results = {}
    for group in ("mean_reversion", "trend_pullback"):
        result = train_candidate([r for r in rows if r["group"] == group], config, provenance)
        if result.get("parameters"):
            parameters = result["parameters"]
            holdout = [r for r in rows if r["group"] == group and r["date"] >= result["sample"]["holdout_start"]]
            def predict(row):
                return parameters["intercept"] + sum(w * ((row[f] - m) / s) for f, m, s, w in zip(parameters["features"], parameters["center"], parameters["scale"], parameters["weights"]))
            result["account_backtest"] = account_backtest(holdout, [predict(r) for r in holdout], {**config, "entry_threshold": parameters["entry_threshold"]})
            result["incumbent_account_backtest"] = account_backtest(holdout, [(r["oversold"] + r["hv_norm"])/2 for r in holdout], {**config, "entry_threshold":0})
            account, incumbent = result["account_backtest"], result["incumbent_account_backtest"]
            if account["net_return"] <= max(0, incumbent["net_return"]) or account["max_drawdown"] < incumbent["max_drawdown"]:
                result["rejection_reasons"].append("account_return_or_drawdown_does_not_improve")
            result["eligible_for_activation"] = not result["rejection_reasons"]
        result["strategy_group"] = group
        results[group] = result
    candidate = results["mean_reversion"]
    candidate["activated"] = activate_candidate(candidate, config)
    cache_set(CANDIDATE_KEY, candidate)
    report = {"status": "completed", "finished_at": datetime.now(timezone.utc).isoformat(),
              "source": provenance, "groups": results, "activated": candidate["activated"],
              "note": "Research candidate only unless all promotion gates pass. Training cadence does not change holding horizon."}
    out = Path(output) if output else market._CACHE_ROOT.parent / "parameter_research" / f"pullback_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    cache_set(STATUS_KEY, {"status": "completed", "finished_at": report["finished_at"], "report_path": str(out),
                           "activated": report["activated"], "candidate_version": candidate.get("parameter_version"),
                           "rejection_reasons": candidate.get("rejection_reasons", []), "groups": {k: v["status"] for k, v in results.items()}})
    print(json.dumps({"report_path": str(out), "groups": {k: v["status"] for k, v in results.items()}, "activated": report["activated"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output")
    try:
        run(parser.parse_args().output)
    except Exception as exc:
        print(json.dumps({"status":"failed", "error_type":type(exc).__name__, "stage":"parameter_training"}), file=sys.stderr, flush=True)
        raise SystemExit(1)
