"""Stock-group and forward-time audits for the experimental v-swing model."""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, precision_recall_fscore_support, brier_score_loss
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.tree import DecisionTreeClassifier, export_text
from sklearn.isotonic import IsotonicRegression

from v_swing_model import FEATURES, features, fit_models, buy_plan, settle_buy, config, training_panel


def metrics(y, p, threshold) -> dict:
    y, p = np.asarray(y), np.asarray(p)
    precision, recall, f1, _ = precision_recall_fscore_support(y, p > threshold, average="binary", zero_division=0)
    return {"n": len(y), "positive": int(y.sum()), "baseline_pr_auc": float(y.mean()),
            "pr_auc": float(average_precision_score(y, p)) if y.sum() else None,
            "precision": float(precision), "recall": float(recall), "f1": float(f1)}


def group_audit(panel: pd.DataFrame, cfg: dict | None = None) -> dict:
    cfg = cfg or config()
    count = int(panel.symbol.nunique())
    if count < 3:
        return {"status": "insufficient_groups", "pattern_supported": False}
    predictions = {side: {model: np.full(len(panel), np.nan) for model in ("rf", "logistic")}
                   for side in ("buy", "sell")}
    thresholds = {side: {model: np.zeros(len(panel)) for model in ("rf", "logistic")}
                  for side in ("buy", "sell")}
    folds = []
    for train, test in GroupKFold(n_splits=min(5, count)).split(panel[FEATURES], groups=panel.symbol):
        training, testing = panel.iloc[train], panel.iloc[test]
        assert not set(training.symbol) & set(testing.symbol)
        folds.append({"train_symbols": sorted(set(training.symbol)), "test_symbols": sorted(set(testing.symbol))})
        for side, label in (("buy", 2), ("sell", 1)):
            y = (training.label == label).astype(int)
            if y.nunique() < 2:
                continue
            for name in ("rf", "logistic"):
                model = (RandomForestClassifier(n_estimators=cfg["n_estimators"], max_depth=cfg["max_depth"],
                                                class_weight="balanced_subsample", random_state=7, n_jobs=1)
                         if name == "rf" else make_pipeline(StandardScaler(), LogisticRegression(class_weight="balanced", max_iter=2000, random_state=7)))
                model.fit(training[FEATURES], y)
                predictions[side][name][test] = model.predict_proba(testing[FEATURES])[:, 1]
                thresholds[side][name][test] = min(float(np.quantile(model.predict_proba(training[FEATURES])[:, 1], .90)), .95)
    result = {"status": "completed", "folds": folds, "sides": {}}
    for side, label in (("buy", 2), ("sell", 1)):
        y = (panel.label == label).to_numpy(dtype=int)
        info = {}
        for name in ("rf", "logistic"):
            p = predictions[side][name]
            valid = np.isfinite(p)
            info[name] = metrics(y[valid], p[valid], thresholds[side][name][valid])
        # Resample entire securities, not highly correlated daily rows.
        rng = np.random.default_rng(7)
        groups = panel.symbol.to_numpy()
        unique = np.unique(groups)
        estimates = []
        for _ in range(300):
            selected = rng.choice(unique, len(unique), replace=True)
            idx = np.concatenate([np.flatnonzero(groups == group) for group in selected])
            p = predictions[side]["rf"][idx]
            valid = np.isfinite(p)
            if valid.any() and y[idx][valid].sum():
                estimates.append(average_precision_score(y[idx][valid], p[valid]) - y[idx][valid].mean())
        info["rf_pr_auc_lift_ci"] = np.quantile(estimates, [.025, .975]).tolist() if estimates else None
        tree = DecisionTreeClassifier(max_depth=3, class_weight="balanced", random_state=7).fit(panel[FEATURES], y)
        info["approximate_rule"] = export_text(tree, feature_names=FEATURES)
        info["rule_is_full_training_explanation_not_test_result"] = True
        result["sides"][side] = info
    result["pattern_supported"] = all((result["sides"][s]["rf_pr_auc_lift_ci"] or [-1])[0] > 0 for s in ("buy", "sell"))
    result["scope"] = "unseen-symbol annotation matching; not temporal profitability or hindsight-free labels"
    return result


def forward_audit(histories: dict, cfg: dict | None = None) -> dict:
    cfg = cfg or config()
    frames = {s: f for s, f in histories.items() if not f.empty and s not in cfg.get("identity_exclusions", {})}
    if not frames:
        return {"status": "insufficient_history", "events": [], "folds": []}
    end = max(f.index[-1] for f in frames.values())
    # Fixed calendar splits, not chosen by profitable dates or test-set thresholds.
    starts = pd.date_range("2026-06-01", end, freq="MS")
    events, folds, latest = [], [], {}
    for start in starts:
        cutoff = (start - pd.Timedelta(days=1)).date().isoformat()
        panel, audit = training_panel(frames, cutoff, cfg)
        try:
            models = fit_models(panel, cfg)
        except ValueError:
            folds.append({"cutoff": cutoff, "status": "insufficient_labels"})
            continue
        finish = start + pd.offsets.MonthBegin(1)
        fold = {"cutoff": cutoff, "trained_through": models["cutoff"], "test_start": start.date().isoformat(),
                "test_end_exclusive": finish.date().isoformat(), "thresholds": models["thresholds"], "training_labels": audit["used_buy"] + audit["used_sell"]}
        assert fold["trained_through"] < fold["test_start"]
        folds.append(fold)
        for symbol, frame in frames.items():
            f = features(frame)
            days = f.loc[(f.index >= start) & (f.index < finish)].dropna()
            if days.empty:
                continue
            probs = {side: models["models"][side].predict_proba(days[FEATURES])[:, 1] for side in ("buy", "sell")}
            for position, day in enumerate(days.index):
                for side in ("buy", "sell"):
                    p = float(probs[side][position])
                    if p <= models["thresholds"][side]:
                        continue
                    if (symbol, side) in latest and (day - latest[symbol, side]).days <= cfg["lifecycle_days"]:
                        continue
                    latest[symbol, side] = day
                    stamp = day.date().isoformat()
                    plan = buy_plan(frame.loc[:day]) if side == "buy" else None
                    if side == "buy" and plan is None:
                        continue
                    outcome = settle_buy(frame, stamp, plan, cfg) if side == "buy" else settle_sell(frame, stamp, cfg)
                    sensitivity = settle_buy(frame, stamp, plan, cfg, "next_close") if side == "buy" else None
                    events.append({"symbol": symbol, "side": side, "signal_date": stamp, "p": p,
                                   "trained_through": models["cutoff"], "plan": plan, "outcome": outcome,
                                   "next_close_sensitivity": sensitivity, "scope": "historical_walk_forward_replay"})
    resolved = [e for e in events if e["side"] == "buy" and e["outcome"].get("status") == "settled"]
    values = [e["outcome"]["net_return"] for e in resolved]
    holdout = starts[-2].date().isoformat() if len(starts) >= 2 else None
    calibration = profit_calibration(resolved, holdout)
    return {"status": "completed", "folds": folds, "events": events, "resolved_buys": len(resolved),
            "net_win_rate": float(np.mean(np.array(values) > 0)) if values else None,
            "mean_net_return": float(np.mean(values)) if values else None,
            "profit_calibration": calibration, "holdout_start": holdout,
            "portfolio": portfolio_audit(frames, events, cfg, "next_open"),
            "portfolio_next_close_sensitivity": portfolio_audit(frames, events, cfg, "next_close"),
            "caveat": "时序回放仍使用复盘挑选的标注/现存证券和当前历史缓存，不能消除后视与幸存者偏差；不是引用提示词中的业绩"}


def portfolio_audit(frames: dict, events: list, cfg: dict, execution: str) -> dict:
    """Cash-constrained research ledger, never an average of overlapping trades."""
    buys = [e for e in events if e["side"] == "buy"]
    if not buys:
        return {"status": "no_buy_events", "execution": execution}
    start = min(pd.Timestamp(e["signal_date"]) for e in buys)
    dates = sorted(set().union(*(set(f.index[f.index >= start]) for f in frames.values())))
    orders = {}
    for e in buys:
        frame = frames[e["symbol"]]
        later = frame.loc[frame.index > pd.Timestamp(e["signal_date"])]
        if later.empty:
            continue
        # Both independent models may trigger; the live service watches rather than opens.
        if any(s["side"] == "sell" and s["symbol"] == e["symbol"] and s["signal_date"] == e["signal_date"] for s in events):
            continue
        outcome = e["outcome"] if execution == "next_open" else e["next_close_sensitivity"]
        if outcome["status"] in ("skipped", "unavailable"):
            continue
        orders.setdefault(later.index[0], []).append((e, outcome))
    cash, positions, curve, transactions, skipped = 1., {}, [], [], 0
    fee = cfg["round_trip_cost"] / 2
    last_marks = {}
    benchmark = {s: float(f.loc[start, "Close"]) for s,f in frames.items() if start in f.index}
    for day in dates:
        for symbol, frame in frames.items():
            if day in frame.index:
                last_marks[symbol] = float(frame.loc[day, "Close"])

        def exit_due():
            nonlocal cash
            for symbol, pos in list(positions.items()):
                o = pos["outcome"]
                if o.get("status") == "settled" and pd.Timestamp(o["exit_date"]) == day:
                    cash += pos["shares"] * (o["exit_price"]-pos["entry"]*fee)
                    transactions.append({"symbol": symbol, "entry_date": pos["entry_date"], "exit_date": o["exit_date"],
                                         "exit_reason": o["exit_reason"], "net_return": o["net_return"]})
                    del positions[symbol]

        # Next-open entries cannot reinvest exits observed later in the same day.
        if execution == "next_close":
            exit_due()
        candidates = sorted(orders.get(day, []), key=lambda pair: pair[0]["p"], reverse=True)
        for event, outcome in candidates:
            symbol = event["symbol"]
            if symbol in positions or len(positions) >= cfg["max_open_buys"]:
                skipped += 1
                continue
            frame = frames[symbol]
            entry = float(frame.loc[day, "Open" if execution == "next_open" else "Close"])
            # Entry sizing may use only previous completed closes for next-open orders.
            marks = {s: float(frames[s].loc[frames[s].index < day, "Close"].iloc[-1])
                     if execution == "next_open" else last_marks[s] for s in positions}
            equity = cash + sum(pos["shares"]*marks[s] for s,pos in positions.items())
            risk = event["plan"]["entry"]-event["plan"]["stop"]
            shares = min(equity*.01/risk, equity*.20/entry, cash/(entry*(1+fee)))
            if shares <= 0:
                skipped += 1
                continue
            cash -= shares*entry*(1+fee)
            positions[symbol] = {"entry": entry, "entry_date": day.date().isoformat(), "shares": shares, "outcome": outcome}
        exit_due()
        nav = cash + sum(p["shares"]*last_marks[s] for s,p in positions.items())
        bh = float(np.mean([last_marks.get(s, price)/price for s,price in benchmark.items()]))
        curve.append({"date": day.date().isoformat(), "nav": nav, "buy_hold_nav": bh, "open_positions": len(positions), "cash": cash})
    values = np.array([r["nav"] for r in curve])
    drawdown = values / np.maximum.accumulate(np.r_[1., values])[1:] - 1
    return {"status": "completed", "execution": execution, "curve": curve,
            "net_return": float(values[-1]-1), "buy_hold_return": curve[-1]["buy_hold_nav"]-1,
            "max_drawdown": float(drawdown.min()), "closed_trades": len(transactions),
            "open_positions": len(positions), "skipped_capacity_or_conflict": skipped, "trades": transactions,
            "assumptions": {"risk_per_entry": .01, "max_position": .20, "max_open": cfg["max_open_buys"],
                            "round_trip_cost": cfg["round_trip_cost"], "fractional_shares": True,
                            "benchmark": "equal_weight_existing_annotation_securities", "marked_at_close": True},
            "caveat": "研究组合；非用户账户回测，现存证券/复盘标注/未核验复权。未引用原提示词2022及2026业绩。"}


def settle_sell(frame: pd.DataFrame, stamp: str, cfg: dict) -> dict:
    later = frame.loc[frame.index > pd.Timestamp(stamp)]
    h = cfg["hold_sessions"]
    if len(later) < h:
        return {"status": "pending"}
    # Sell is a long-position exit reference, NOT a hypothetical short profit.
    return {"status": "settled", "exit_date": later.index[h-1].date().isoformat(),
            "subsequent_return": float(later.Close.iloc[h-1]/later.Open.iloc[0]-1), "not_short_trade": True}


def profit_calibration(events: list, holdout: str | None) -> dict:
    if not holdout:
        return {"status": "insufficient_time_split", "eligible": False}
    train = [e for e in events if e["signal_date"] < holdout and e["outcome"]["exit_date"] < holdout]
    test = [e for e in events if e["signal_date"] >= holdout]
    if len(train) < 60 or len(test) < 30 or len({e["symbol"] for e in train}) < 5:
        return {"status": "insufficient_samples", "eligible": False, "train_n": len(train), "test_n": len(test)}
    x, y = [e["p"] for e in train], [int(e["outcome"]["win"]) for e in train]
    if len(set(y)) < 2:
        return {"status": "single_class", "eligible": False}
    iso = IsotonicRegression(out_of_bounds="clip").fit(x, y)
    ty = np.array([int(e["outcome"]["win"]) for e in test])
    p = iso.predict([e["p"] for e in test])
    baseline = brier_score_loss(ty, np.full(len(ty), np.mean(y)))
    brier = brier_score_loss(ty, p)
    return {"status": "evaluated", "eligible": bool(brier < baseline), "train_n": len(train), "test_n": len(test),
            "brier": brier, "baseline_brier": baseline, "x": iso.X_thresholds_.tolist(), "y": iso.y_thresholds_.tolist(),
            "kind": "net_profit_after_frozen_plan_next_open", "training_exit_before": holdout,
            "experimental": True, "note": "不等于实盘胜率；基于含后视标注的时序回放"}
