"""Small, auditable parameter registry and purged time-ordered experiments."""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import threading
from functools import lru_cache

import numpy as np

from app_database import AGENT_DIR, cache_get, cache_set
from pullback_validation import block_interval

CONFIG_PATH = AGENT_DIR / "config" / "pullback_parameters.json"
STATUS_KEY = "pullback_parameters:training_status:v1"
CANDIDATE_KEY = "pullback_parameters:candidate:v1"
ACTIVE_KEY = "pullback_parameters:active:v1"
FEATURES = ["oversold", "hv_norm", "market_tag", "sector_tag"]
TRAIN_LOCK = threading.Lock()


def settings():
    text = CONFIG_PATH.read_text(encoding="utf-8")
    config = json.loads(text)
    weights = [float(config[k]) for k in ("oversold_weight", "volatility_weight")]
    if not all(np.isfinite(w) and w >= 0 for w in weights) or sum(weights) <= 0:
        raise ValueError("invalid pullback weights")
    if not config["rs_weak_threshold"] < config["rs_full_threshold"] or not 0 <= config["rs_weak_multiplier"] <= 1:
        raise ValueError("invalid relative strength thresholds")
    config["version"] += ":" + hashlib.sha256(text.encode()).hexdigest()[:12]
    config["gap_sessions"] = max(int(config["gap_sessions"]), int(config["training_horizon_days"]) + 1)
    return config


@lru_cache(maxsize=1)
def _ranking_settings(mtime):
    return settings()


def ranking_settings():
    return _ranking_settings(CONFIG_PATH.stat().st_mtime_ns)


def feature_vector(row):
    return [float(row[f]) for f in FEATURES]


def purged_split(rows, start_date, end_date=None, gap_sessions=10):
    # Calendar sessions, not row counts: all stocks on a date stay together.
    dates = sorted({r["date"] for r in rows})
    cutoff = dates[max(0, dates.index(start_date) - gap_sessions)]
    train = [r for r in rows if r["date"] < cutoff and r["exit_date"] < start_date]
    test = [r for r in rows if r["date"] >= start_date and (end_date is None or r["date"] < end_date)]
    return train, test


def evaluate(rows, predictions, config):
    grouped = {}
    for row, predicted in zip(rows, predictions):
        if predicted < config.get("entry_threshold", 0):
            continue
        grouped.setdefault(row["date"], []).append((float(predicted), row))
    selected = []
    by_day = {r["date"]: [0.0] for r in rows}
    for day, candidates in grouped.items():
        picks = sorted(candidates, key=lambda x: (-x[0], x[1]["symbol"]))[:int(config["top_n"])]
        selected.extend(r for _, r in picks)
        by_day[day] = [float(np.mean([r["net_return"] for _, r in picks]))]
    if not selected:
        return {"n": 0, "dates": 0, "utility": -1.0, "mean_net": None, "ci95": [None, None]}
    daily = np.array([np.mean(v) for _, v in sorted(by_day.items())])
    # This is a selection utility, not a cash-account equity curve.
    utility = float(daily.mean() - .5 * daily.std())
    return {"n": len(selected), "dates": len(by_day), "utility": utility,
            "traded_dates": len(grouped), "mean_daily_basket_net": float(daily.mean()),
            "mean_net": float(np.mean([r["net_return"] for r in selected])),
            "net_win_rate": float(np.mean([r["net_return"] > 0 for r in selected])),
            "mean_own_excess": float(np.mean([r["net_excess"] for r in selected])),
            "mean_alpha": float(np.mean([r["beta_adjusted_alpha"] for r in selected if r["beta_adjusted_alpha"] is not None]))
                          if any(r["beta_adjusted_alpha"] is not None for r in selected) else None,
            "ci95": block_interval(by_day, block_days=int(config["training_horizon_days"])),
            "ci_metric": "mean_daily_basket_net_cash_included",
            "selection_metric_note": "equal-day basket returns including no-trade dates; not account equity or portfolio Sharpe"}


def train_candidate(rows, config, provenance):
    from sklearn.linear_model import LogisticRegression, Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    dates = sorted({r["date"] for r in rows})
    minimum = int(config["min_training_dates"]) + int(config["validation_dates"]) * 3 + int(config["holdout_dates"]) + int(config["gap_sessions"])
    if len(dates) < minimum:
        return {"status": "insufficient_data", "reason": f"need {minimum} signal dates, got {len(dates)}", "eligible_for_activation": False}
    holdout_start = dates[-int(config["holdout_dates"])]
    development = [r for r in rows if r["date"] < holdout_start and r["exit_date"] < holdout_start]
    dev_dates = sorted({r["date"] for r in development})
    fold_starts = [dev_dates[-int(config["validation_dates"]) * n] for n in (3, 2, 1)]
    candidates = []
    for variant in ("core", "soft_tags"):
        indices = [0, 1] if variant == "core" else [0, 1, 2, 3]
        for alpha in config["regularization_grid"]:
            for threshold in config["entry_threshold_grid"]:
                fold_results = []
                for start, end in zip(fold_starts, fold_starts[1:] + [holdout_start]):
                    train, validation = purged_split(development, start, end, config["gap_sessions"])
                    if len({r["date"] for r in train}) < config["min_training_dates"]:
                        continue
                    model = make_pipeline(StandardScaler(), Ridge(alpha=float(alpha)))
                    x = np.array([feature_vector(r) for r in train])[:, indices]
                    y = np.array([r["net_return"] for r in train])
                    model.fit(x, y)
                    pred = model.predict(np.array([feature_vector(r) for r in validation])[:, indices])
                    fold_results.append(evaluate(validation, pred, {**config, "entry_threshold": threshold}))
                if len(fold_results) == 3:
                    candidates.append({"variant": variant, "indices": indices, "alpha": float(alpha), "entry_threshold": float(threshold),
                                       "folds": fold_results, "selection_utility": float(np.median([f["utility"] for f in fold_results]))})
    if not candidates:
        return {"status": "insufficient_data", "reason": "not enough purged training folds", "eligible_for_activation": False}
    chosen = max(candidates, key=lambda c: (c["selection_utility"], -len(c["indices"])))
    train, holdout = purged_split(rows, holdout_start, gap_sessions=config["gap_sessions"])
    indices = chosen["indices"]
    x = np.array([feature_vector(r) for r in train])[:, indices]
    y = np.array([r["net_return"] for r in train])
    model = make_pipeline(StandardScaler(), Ridge(alpha=chosen["alpha"]))
    model.fit(x, y)
    hx = np.array([feature_vector(r) for r in holdout])[:, indices]
    holdout_predictions = model.predict(hx)
    final_metrics = evaluate(holdout, holdout_predictions, {**config, "entry_threshold": chosen["entry_threshold"]})
    # A fixed equal-weight core is the predeclared incumbent. Holdout never
    # chooses features, regularization, entry thresholds or parameter weights.
    baseline = evaluate(holdout, [(r["oversold"] + r["hv_norm"]) / 2 for r in holdout], {**config, "entry_threshold": 0})
    probability = make_pipeline(StandardScaler(), LogisticRegression(C=1 / chosen["alpha"], max_iter=1000))
    labels = (y > 0).astype(int)
    probability_parameters = None
    if len(set(labels)) == 2:
        probability.fit(x, labels)
        predicted = probability.predict_proba(hx)[:, 1]
        outcome = np.array([r["net_return"] > 0 for r in holdout], dtype=int)
        base_rate = float(labels.mean())
        final_metrics["brier"] = float(np.mean((predicted - outcome) ** 2))
        final_metrics["training_base_rate_brier"] = float(np.mean((base_rate - outcome) ** 2))
        scaler, logistic = probability.steps[0][1], probability.steps[1][1]
        probability_parameters = {"center": scaler.mean_.tolist(), "scale": scaler.scale_.tolist(),
                                  "weights": logistic.coef_[0].tolist(), "intercept": float(logistic.intercept_[0])}
    scaler, regressor = model.steps[0][1], model.steps[1][1]
    parameters = {"features": [FEATURES[i] for i in indices], "center": scaler.mean_.tolist(), "scale": scaler.scale_.tolist(),
                  "weights": regressor.coef_.tolist(), "intercept": float(regressor.intercept_),
                  "entry_threshold": chosen["entry_threshold"], "horizon_days": config["training_horizon_days"],
                  "probability": probability_parameters}
    failures = []
    if not (final_metrics.get("mean_daily_basket_net") is not None and final_metrics["mean_daily_basket_net"] > 0 and final_metrics["mean_daily_basket_net"] > (baseline.get("mean_daily_basket_net") or 0)):
        failures.append("holdout_net_does_not_improve")
    if final_metrics["ci95"][0] is None or final_metrics["ci95"][0] <= 0:
        failures.append("holdout_net_ci_not_positive")
    if sum((f.get("mean_net") or 0) > 0 for f in chosen["folds"]) < 2:
        failures.append("validation_folds_unstable")
    if probability_parameters is None or final_metrics.get("brier", 1) >= final_metrics.get("training_base_rate_brier", 0):
        failures.append("probability_does_not_beat_training_base_rate")
    for key in ("point_in_time_universe", "corporate_actions_verified", "unseen_holdout", "account_backtest_verified"):
        if provenance.get(key) is not True:
            failures.append(f"unverified_{key}")
    version = hashlib.sha256(json.dumps({"parameters": parameters, "source": provenance, "holdout_start": holdout_start}, sort_keys=True).encode()).hexdigest()[:16]
    return {"status": "validated_candidate" if not failures else "research_only", "parameter_version": version,
            "eligible_for_activation": not failures, "rejection_reasons": failures, "parameters": parameters,
            "generated_at": datetime.now(timezone.utc).isoformat(), "provenance": provenance,
            "sample": {"events": len(rows), "dates": len(dates), "train_end": max(r["exit_date"] for r in train),
                       "holdout_start": holdout_start, "holdout_end": dates[-1]},
            "chosen": chosen, "candidate_search": candidates, "holdout": final_metrics, "incumbent_holdout": baseline,
            "training_rule": "3 time-ordered folds; all training exits before validation; independent terminal holdout; train-only scaling"}


def activate_candidate(candidate, config):
    if not config.get("auto_activate") or not candidate.get("eligible_for_activation"):
        return False
    # Eligibility is rechecked from evidence, not accepted from a mutable flag.
    if candidate.get("rejection_reasons") or not all(candidate.get("provenance", {}).get(k) is True for k in
            ("point_in_time_universe", "corporate_actions_verified", "unseen_holdout", "account_backtest_verified")):
        return False
    metrics = candidate.get("holdout") or {}
    lower = (metrics.get("ci95") or [None])[0]
    if lower is None or lower <= 0 or metrics.get("mean_daily_basket_net", 0) <= (candidate.get("incumbent_holdout") or {}).get("mean_daily_basket_net", 0):
        return False
    cache_set(ACTIVE_KEY, candidate)
    return True


def status():
    config = settings()
    return {"enabled": config["training_enabled"], "interval_days": config["training_interval_days"],
            "auto_activate": config["auto_activate"], "training_horizon_days": config["training_horizon_days"],
            "active": cache_get(ACTIVE_KEY), "latest": cache_get(STATUS_KEY), "configured_parameters": config}


def maybe_start_training(force=False):
    import subprocess
    import sys
    config = settings()
    latest = cache_get(STATUS_KEY) or {}
    if latest.get("status") == "running" and latest.get("started_at"):
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(latest["started_at"])).total_seconds()
        if age < int(config["training_timeout_seconds"]) + 60:
            return {"status": "running"}
    if not config.get("training_enabled") or not (AGENT_DIR / config["dataset"]).is_file():
        return {"status": "disabled_or_dataset_missing"}
    last = latest.get("finished_at") or latest.get("started_at")
    if not force and last:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
        if age < float(config["training_interval_days"]) * 86400:
            return {"status": "not_due"}
    if not TRAIN_LOCK.acquire(blocking=False):
        return {"status": "running"}
    cache_set(STATUS_KEY, {"status": "running", "started_at": datetime.now(timezone.utc).isoformat()})

    def worker():
        try:
            result = subprocess.run([sys.executable, str(AGENT_DIR / "scripts" / "train_pullback_parameters.py")],
                                    cwd=AGENT_DIR, capture_output=True, text=True, timeout=int(config["training_timeout_seconds"]))
            if result.returncode:
                detail = {}
                for line in (result.stderr or "").splitlines():
                    try:
                        item = json.loads(line)
                        if isinstance(item, dict) and item.get("error_type"):
                            detail = item
                    except ValueError:
                        pass
                cache_set(STATUS_KEY, {"status":"failed", "error":detail.get("error_type", "process_failure"),
                                       "return_code":result.returncode, "stage":detail.get("stage", "unknown"),
                                       "finished_at":datetime.now(timezone.utc).isoformat()})
        except Exception as exc:
            cache_set(STATUS_KEY, {"status": "failed", "error": type(exc).__name__, "finished_at": datetime.now(timezone.utc).isoformat()})
        finally:
            TRAIN_LOCK.release()
    threading.Thread(target=worker, daemon=True, name="pullback-parameter-training").start()
    return {"status": "running"}


def predict_pick(pick, active):
    e = pick.get("playbook_enhancements") or {}
    launch = pick.get("raw_launch_score")
    hv = (pick.get("hv") or {}).get("hv_rise")
    if launch is None or hv is None or launch > .55:
        return None
    params = active.get("parameters") or {}
    features = {"oversold": 1 - launch, "hv_norm": min(1, max(0, .5 + .5 * hv)),
                "market_tag": int(bool(e.get("market_liquid_rs_top40"))),
                "sector_tag": int(bool(e.get("stock_stronger_than_industry")))}
    x = [(features[f] - m) / s for f, m, s in zip(params["features"], params["center"], params["scale"])]
    net = sum(w * v for w, v in zip(params["weights"], x)) + params["intercept"]
    prob = None
    p = params.get("probability")
    if p:
        z = p["intercept"] + sum(w * ((features[f] - m) / s) for f, m, s, w in zip(params["features"], p["center"], p["scale"], p["weights"]))
        prob = float(1 / (1 + np.exp(-np.clip(z, -30, 30))))
    return {"expected_net_return": net, "positive_net_probability": prob,
            "entry_candidate": net >= params["entry_threshold"], "parameter_version": active["parameter_version"],
            "horizon_days": params["horizon_days"]}
