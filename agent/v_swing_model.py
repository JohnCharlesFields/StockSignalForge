"""Causal manual-pattern replication; classification scores are NOT win rates."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier

ROOT = Path(__file__).resolve().parent
FEATURES = ["ret1", "ret5", "ret20", "dist_ema5", "dist_ema10", "dist_ema15", "dist_ema20",
            "slope_ema5", "slope_ema10", "slope_ema15", "slope_ema20", "bull", "bear", "rsi14",
            "dist_20d_high", "dist_20d_low", "vol_ratio"]


def config() -> dict:
    value = json.loads((ROOT / "config" / "v_swing.json").read_text(encoding="utf-8"))
    if not 0 < value["threshold_quantile"] < 1 or not 1 <= value["hold_sessions"] <= 30:
        raise ValueError("invalid_v_swing_config")
    return value


def annotations() -> dict:
    path = ROOT / "config" / "v_swing_annotations.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def clean_bars(frame: pd.DataFrame, as_of: str | None = None) -> pd.DataFrame:
    if frame is None or frame.empty or not set(["Open", "High", "Low", "Close", "Volume"]).issubset(frame.columns):
        return pd.DataFrame()
    if not isinstance(frame.index, pd.DatetimeIndex):
        return pd.DataFrame()
    out = frame[["Open", "High", "Low", "Close", "Volume"]].copy()
    out.index = out.index.tz_localize(None).normalize()
    out = out[~out.index.duplicated(keep="last")].sort_index().apply(pd.to_numeric, errors="coerce")
    if as_of:
        out = out.loc[out.index <= pd.Timestamp(as_of)]
    valid = np.isfinite(out).all(axis=1) & (out[["Open", "High", "Low", "Close"]] > 0).all(axis=1)
    valid &= (out.Volume >= 0) & (out.Low <= out[["Open", "Close"]].min(axis=1)) & (out.High >= out[["Open", "Close"]].max(axis=1))
    # Do not silently bridge malformed bars, which would change rolling horizons.
    return out if valid.all() else pd.DataFrame()


def features(frame: pd.DataFrame) -> pd.DataFrame:
    bars = clean_bars(frame)
    if bars.empty:
        return pd.DataFrame(columns=FEATURES, index=pd.DatetimeIndex([]))
    close = bars.Close
    out = pd.DataFrame(index=bars.index)
    for n in (1, 5, 20):
        out[f"ret{n}"] = close.pct_change(n, fill_method=None)
    emas = {}
    for n in (5, 10, 15, 20):
        ema = emas[n] = close.ewm(span=n, adjust=False).mean()
        out[f"dist_ema{n}"] = close / ema - 1
        out[f"slope_ema{n}"] = ema.pct_change(5, fill_method=None)
    out["bull"] = ((emas[5] > emas[10]) & (emas[10] > emas[15]) & (emas[15] > emas[20])).astype(float)
    out["bear"] = ((emas[5] < emas[10]) & (emas[10] < emas[15]) & (emas[15] < emas[20])).astype(float)
    change = close.diff()
    gain = change.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-change.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rsi = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    out["rsi14"] = rsi.mask((loss == 0) & (gain > 0), 100).mask((gain == 0) & (loss > 0), 0).mask((gain == 0) & (loss == 0), 50)
    out["dist_20d_high"] = close / bars.High.rolling(20).max() - 1
    out["dist_20d_low"] = close / bars.Low.rolling(20).min() - 1
    out["vol_ratio"] = bars.Volume / bars.Volume.rolling(20).mean().replace(0, np.nan)
    return out[FEATURES].replace([np.inf, -np.inf], np.nan)


def training_panel(histories: dict[str, pd.DataFrame], cutoff: str, cfg: dict | None = None) -> tuple[pd.DataFrame, dict]:
    cfg = cfg or config()
    labels = annotations()
    if not any(labels.values()):
        raise ValueError("v_swing_private_annotations_required")
    first = min(day for days in labels.values() for day in days)
    last = min(cutoff, max(day for days in labels.values() for day in days))
    parts, omitted = [], []
    for symbol, marks in labels.items():
        if symbol in cfg.get("identity_exclusions", {}):
            omitted.extend({"symbol": symbol, "date": day, "reason": "identity_unverified"} for day in marks if day <= cutoff)
            continue
        bars = clean_bars(histories.get(symbol), cutoff)
        f = features(bars).iloc[cfg["warmup_bars"] - 1:].dropna()
        f = f.loc[(f.index >= pd.Timestamp(first)) & (f.index <= pd.Timestamp(last))].copy()
        if not f.empty:
            f["symbol"] = symbol
            f["date"] = f.index.strftime("%Y-%m-%d")
            f["label"] = [2 if marks.get(day) == "buy" else 1 if marks.get(day) == "sell" else 0 for day in f.date]
            parts.append(f)
        omitted.extend({"symbol": symbol, "date": day, "reason": "missing_bar_or_warmup"}
                       for day in marks if day <= cutoff and pd.Timestamp(day) not in f.index)
    panel = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=FEATURES + ["symbol", "date", "label"])
    return panel, {"supplied_labels": sum(len(v) for v in labels.values()),
                   "supplied_buy": sum(v == "buy" for marks in labels.values() for v in marks.values()),
                   "supplied_sell": sum(v == "sell" for marks in labels.values() for v in marks.values()),
                   "used_buy": int((panel.label == 2).sum()), "used_sell": int((panel.label == 1).sum()),
                   "rows": len(panel), "symbols": int(panel.symbol.nunique()), "cutoff": last, "omitted": omitted,
                   "label_caveat": "复盘标注；未标注日按对照编码，不代表已核验负样本；分组/时序验证仍不能消除标注后视偏差"}


def fit_models(panel: pd.DataFrame, cfg: dict | None = None, multipliers: dict | None = None) -> dict:
    cfg = cfg or config()
    if panel.empty:
        raise ValueError("no_training_rows")
    result = {"cutoff": str(panel.date.max()), "models": {}, "thresholds": {}}
    for side, label in (("buy", 2), ("sell", 1)):
        y = (panel.label == label).astype(int)
        if y.sum() < cfg["min_positive_labels"] or y.nunique() < 2:
            raise ValueError(f"insufficient_{side}_labels")
        model = RandomForestClassifier(n_estimators=cfg["n_estimators"], max_depth=cfg["max_depth"],
                                       class_weight="balanced_subsample", random_state=cfg["random_state"], n_jobs=1)
        model.fit(panel[FEATURES], y)
        mult = float((multipliers or {}).get(side, 1))
        if not np.isfinite(mult) or not .7 <= mult <= 1.6:
            raise ValueError("invalid_threshold_multiplier")
        result["models"][side] = model
        result["thresholds"][side] = min(float(np.quantile(model.predict_proba(panel[FEATURES])[:, 1], cfg["threshold_quantile"])) * mult, .95)
    return result


def buy_plan(frame: pd.DataFrame) -> dict | None:
    bars = clean_bars(frame)
    if len(bars) < 20:
        return None
    previous = bars.Close.shift()
    tr = pd.concat([bars.High - bars.Low, (bars.High - previous).abs(), (bars.Low - previous).abs()], axis=1).max(axis=1)
    atr = float(tr.tail(14).mean())
    entry = float(bars.Close.iloc[-1])
    lo, hi = float(bars.Low.tail(20).min()), float(bars.High.tail(20).max())
    stop = min(lo * .995, entry - 2 * atr)
    risk = entry - stop
    if not 0 < stop < entry or not np.isfinite(risk):
        return None
    return {"entry": entry, "stop": stop, "target": entry + 2 * risk, "atr14": atr,
            "box_lo": lo, "box_hi": hi, "in_box": (hi-lo)/lo < .12,
            "stop_distance_pct": risk / entry, "one_pct_risk_position_pct": 100 * .01 / (risk/entry),
            "stop_too_far": risk/entry > .10}


def score_day(models: dict, symbol: str, frame: pd.DataFrame, session: str, cfg: dict | None = None) -> dict:
    cfg = cfg or config()
    bars = clean_bars(frame, session)
    result = {"symbol": symbol, "signal_date": session, "status": "unavailable", "buy": False, "sell": False,
              "experimental": True, "predicted_win_rate": None, "probability_kind": "manual_pattern_match"}
    if symbol in cfg.get("identity_exclusions", {}):
        return {**result, "status": "identity_unverified"}
    if len(bars) < cfg["min_bars"]:
        return {**result, "status": "insufficient_history"}
    last = bars.index[-1].date().isoformat()
    if last != session:
        return {**result, "status": "stale", "price_as_of": last}
    if session <= models["cutoff"]:
        return {**result, "status": "training_period_not_forward"}
    x = features(bars).tail(1)
    if x.isna().any(axis=None):
        return {**result, "status": "invalid_features"}
    buy, sell = [float(models["models"][side].predict_proba(x)[:, 1][0]) for side in ("buy", "sell")]
    plan = buy_plan(bars)
    return {**result, "status": "scored", "price_as_of": last, "close": float(bars.Close.iloc[-1]),
            "buy_p": buy, "sell_p": sell, "buy_threshold": models["thresholds"]["buy"],
            "sell_threshold": models["thresholds"]["sell"], "buy": bool(plan and buy > models["thresholds"]["buy"]),
            "sell": sell > models["thresholds"]["sell"], "plan": plan, "trained_through": models["cutoff"]}


def settle_buy(frame: pd.DataFrame, signal_date: str, plan: dict, cfg: dict | None = None, execution: str = "next_open") -> dict:
    """Stop-first same-bar policy; +1R stop upgrade is effective NEXT session."""
    cfg = cfg or config()
    bars = clean_bars(frame)
    if bars.empty:
        return {"status": "unavailable", "reason": "invalid_or_missing_bars"}
    later = bars.loc[bars.index > pd.Timestamp(signal_date)]
    if later.empty:
        return {"status": "pending"}
    if execution not in ("next_open", "next_close"):
        raise ValueError("invalid_execution")
    entry = float(later.iloc[0].Open if execution == "next_open" else later.iloc[0].Close)
    stop, target = float(plan["stop"]), float(plan["target"])
    if not stop < entry < target:
        return {"status": "skipped", "reason": "entry_gap_outside_frozen_risk_plan"}
    risk = float(plan["entry"]) - stop
    if risk <= 0:
        return {"status": "skipped", "reason": "invalid_risk"}
    monitoring = later if execution == "next_open" else later.iloc[1:]
    for i, (stamp, bar) in enumerate(monitoring.iloc[:cfg["hold_sessions"]].iterrows(), 1):
        opening = float(bar.Open)
        reason, exit_price = None, None
        if opening <= stop:
            reason, exit_price = "gap_stop", opening
        elif opening >= target:
            reason, exit_price = "gap_target", opening
        elif float(bar.Low) <= stop:
            reason, exit_price = "stop", stop
        elif float(bar.High) >= target:
            reason, exit_price = "target", target
        elif i == cfg["hold_sessions"]:
            reason, exit_price = "time_exit", float(bar.Close)
        if reason:
            net = exit_price / entry - 1 - cfg["round_trip_cost"]
            return {"status": "settled", "entry_date": later.index[0].date().isoformat(), "entry_fill": entry,
                    "exit_date": stamp.date().isoformat(), "exit_price": exit_price, "exit_reason": reason,
                    "net_return": net, "r_multiple": (exit_price - entry - entry * cfg["round_trip_cost"]) / risk,
                    "win": net > 0, "execution": execution}
        if float(bar.High) >= plan["entry"] + risk:
            stop = max(stop, float(plan["entry"]))
    return {"status": "pending", "entry_date": later.index[0].date().isoformat(), "entry_fill": entry,
            "managed_stop": stop, "sessions_observed": len(monitoring)}
