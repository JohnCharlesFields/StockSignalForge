#!/usr/bin/env python3
"""Calibrate whether playbook soft tags deserve to enter win-rate.

This is research-only. It compares the current single-feature pullback_hv
probability model against small L2-logistic models that include the soft
playbook features. A feature can graduate into production win-rate only if it
improves out-of-sample calibration metrics without damaging top-bucket alpha.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from pathlib import Path
from statistics import mean
from typing import Any

import numpy as np


RUNS_DIR = Path(__file__).resolve().parents[1] / "runs"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _sigmoid(z: np.ndarray) -> np.ndarray:
    z = np.clip(z, -35.0, 35.0)
    return 1.0 / (1.0 + np.exp(-z))


def _standardize(train: np.ndarray, other: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mu = train.mean(axis=0)
    sd = train.std(axis=0)
    sd[sd < 1e-9] = 1.0
    return (train - mu) / sd, (other - mu) / sd, np.vstack([mu, sd])


def _fit_logistic(x: np.ndarray, y: np.ndarray, *, l2: float = 0.25, lr: float = 0.08, steps: int = 2500) -> np.ndarray:
    xb = np.c_[np.ones(len(x)), x]
    w = np.zeros(xb.shape[1])
    for _ in range(steps):
        p = _sigmoid(xb @ w)
        grad = xb.T @ (p - y) / len(y)
        grad[1:] += l2 * w[1:] / len(y)
        w -= lr * grad
    return w


def _predict(x: np.ndarray, w: np.ndarray) -> np.ndarray:
    return _sigmoid(np.c_[np.ones(len(x)), x] @ w)


def _brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def _logloss(p: np.ndarray, y: np.ndarray) -> float:
    p = np.clip(p, 1e-6, 1.0 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1.0 - y) * np.log(1.0 - p)))


def _auc(p: np.ndarray, y: np.ndarray) -> float | None:
    order = np.argsort(p)
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(p) + 1)
    pos = y == 1
    neg = y == 0
    n_pos = int(pos.sum())
    n_neg = int(neg.sum())
    if n_pos == 0 or n_neg == 0:
        return None
    return float((ranks[pos].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


def _top_bucket(p: np.ndarray, rows: list[dict[str, Any]], q: float = 0.9) -> dict[str, Any]:
    if len(rows) == 0:
        return {"n": 0}
    cutoff = float(np.quantile(p, q))
    grp = [(prob, row) for prob, row in zip(p, rows) if prob >= cutoff]
    wins = [1 if _num(row.get("net_own_excess")) > 0 else 0 for _, row in grp]
    own = [_num(row.get("net_own_excess")) for _, row in grp]
    beta = [_num(row.get("net_beta_alpha")) for _, row in grp]
    return {
        "n": len(grp),
        "avg_pred": round(mean([float(prob) for prob, _ in grp]), 4) if grp else None,
        "win_rate": round(mean(wins), 4) if wins else None,
        "mean_net_own_excess": round(mean(own), 6) if own else None,
        "mean_beta_alpha": round(mean(beta), 6) if beta else None,
    }


def _reliability(p: np.ndarray, y: np.ndarray, buckets: int = 5) -> list[dict[str, Any]]:
    edges = np.quantile(p, np.linspace(0.0, 1.0, buckets + 1))
    out = []
    for i in range(buckets):
        lo = edges[i]
        hi = edges[i + 1]
        if i == buckets - 1:
            mask = (p >= lo) & (p <= hi)
        else:
            mask = (p >= lo) & (p < hi)
        if int(mask.sum()) == 0:
            continue
        out.append({
            "bucket": i + 1,
            "n": int(mask.sum()),
            "pred": round(float(p[mask].mean()), 4),
            "realized": round(float(y[mask].mean()), 4),
            "gap": round(float(y[mask].mean() - p[mask].mean()), 4),
        })
    return out


def _build_rows(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for row in payload.get("events") or []:
        score = _num(row.get("pullback_hv_score"), default=float("nan"))
        if not math.isfinite(score):
            continue
        win = 1 if _num(row.get("net_own_excess")) > 0 else 0
        market_liquid_rs = bool(row.get("market_ok") and row.get("liquid") and row.get("rs_filter"))
        stock_vs_sector = bool(row.get("stock_vs_sector_strong"))
        sector_strong = bool(row.get("sector_strong"))
        rows.append({
            **row,
            "_date": str(row.get("date")),
            "_year": int(str(row.get("date"))[:4]) if str(row.get("date"))[:4].isdigit() else 0,
            "_win": win,
            "_market_liquid_rs": market_liquid_rs,
            "_stock_vs_sector_strong": stock_vs_sector,
            "_sector_strong": sector_strong,
            "_sector_and_stock": bool(sector_strong and stock_vs_sector),
            "_market_liquid_rs_sector_stock": bool(market_liquid_rs and stock_vs_sector),
            "_score2": score * score,
            "_score_x_mlr": score * (1.0 if market_liquid_rs else 0.0),
            "_score_x_stock_sector": score * (1.0 if stock_vs_sector else 0.0),
            "_log_adv": math.log1p(max(0.0, _num(row.get("avg_dollar_volume")))),
        })
    rows.sort(key=lambda r: (r["_date"], str(r.get("ticker") or "")))
    return rows


def _matrix(rows: list[dict[str, Any]], features: list[str]) -> np.ndarray:
    return np.asarray([[_num(row.get(f)) for f in features] for row in rows], dtype=float)


def _evaluate_model(name: str, features: list[str], train: list[dict[str, Any]], test: list[dict[str, Any]]) -> dict[str, Any]:
    if not train or not test:
        return {"name": name, "features": features, "n_train": len(train), "n_test": len(test), "skipped": True}
    x_train_raw = _matrix(train, features)
    y_train = np.asarray([r["_win"] for r in train], dtype=float)
    x_test_raw = _matrix(test, features)
    y_test = np.asarray([r["_win"] for r in test], dtype=float)
    x_train, x_test, scale = _standardize(x_train_raw, x_test_raw)
    w = _fit_logistic(x_train, y_train)
    pred_train = _predict(x_train, w)
    pred_test = _predict(x_test, w)
    coefs = []
    for f, coef, mu, sd in zip(features, w[1:], scale[0], scale[1]):
        coefs.append({"feature": f, "coef_standardized": round(float(coef), 6), "mean": round(float(mu), 6), "std": round(float(sd), 6)})
    return {
        "name": name,
        "features": features,
        "n_train": len(train),
        "n_test": len(test),
        "train": {
            "brier": round(_brier(pred_train, y_train), 6),
            "logloss": round(_logloss(pred_train, y_train), 6),
            "auc": round(_auc(pred_train, y_train) or 0.0, 6),
            "top_decile": _top_bucket(pred_train, train),
        },
        "test": {
            "brier": round(_brier(pred_test, y_test), 6),
            "logloss": round(_logloss(pred_test, y_test), 6),
            "auc": round(_auc(pred_test, y_test) or 0.0, 6),
            "base_rate": round(float(y_test.mean()), 4),
            "avg_pred": round(float(pred_test.mean()), 4),
            "top_decile": _top_bucket(pred_test, test),
            "reliability": _reliability(pred_test, y_test),
        },
        "coefficients": coefs,
        "intercept": round(float(w[0]), 6),
    }


def _model_specs(rows: list[dict[str, Any]]) -> list[tuple[str, list[str]]]:
    has_sector = any(r.get("sector_enriched") for r in rows)
    specs = [
        ("baseline_score_only", ["pullback_hv_score"]),
        ("linear_soft_features", [
            "pullback_hv_score", "market_ok", "liquid", "rs_filter",
            "_market_liquid_rs", "ret60_vs_benchmark", "ret126_vs_benchmark", "_log_adv",
        ]),
        ("nonlinear_soft_features", [
            "pullback_hv_score", "_score2", "market_ok", "liquid", "rs_filter",
            "_market_liquid_rs", "_score_x_mlr", "ret60_vs_benchmark", "ret126_vs_benchmark", "_log_adv",
        ]),
    ]
    if has_sector:
        specs.extend([
            ("sector_stock_features", [
                "pullback_hv_score", "market_ok", "liquid", "rs_filter", "_market_liquid_rs",
                "_sector_strong", "_stock_vs_sector_strong", "_sector_and_stock",
                "sector_3m_vs_spy", "sector_6m_vs_spy", "stock_3m_vs_sector", "stock_6m_vs_sector",
                "_log_adv",
            ]),
            ("nonlinear_sector_stock_features", [
                "pullback_hv_score", "_score2", "market_ok", "liquid", "rs_filter", "_market_liquid_rs",
                "_sector_strong", "_stock_vs_sector_strong", "_sector_and_stock", "_market_liquid_rs_sector_stock",
                "_score_x_mlr", "_score_x_stock_sector",
                "sector_3m_vs_spy", "sector_6m_vs_spy", "stock_3m_vs_sector", "stock_6m_vs_sector",
                "_log_adv",
            ]),
        ])
    return specs


def _yearly_cv(rows: list[dict[str, Any]], specs: list[tuple[str, list[str]]], min_train: int = 2000) -> list[dict[str, Any]]:
    years = sorted({int(r.get("_year") or 0) for r in rows if int(r.get("_year") or 0) > 0})
    folds = []
    for year in years:
        train = [r for r in rows if int(r.get("_year") or 0) < year]
        test = [r for r in rows if int(r.get("_year") or 0) == year]
        if len(train) < min_train or len(test) < 100:
            continue
        fold_models = [_evaluate_model(name, features, train, test) for name, features in specs]
        folds.append({"year": year, "n_train": len(train), "n_test": len(test), "models": fold_models})
    summary = []
    for name, _features in specs:
        items = [m for f in folds for m in f["models"] if m.get("name") == name and not m.get("skipped")]
        if not items:
            continue
        summary.append({
            "name": name,
            "folds": len(items),
            "avg_brier": round(mean([m["test"]["brier"] for m in items]), 6),
            "avg_auc": round(mean([m["test"]["auc"] for m in items]), 6),
            "avg_top_decile_hit": round(mean([m["test"]["top_decile"].get("win_rate") or 0.0 for m in items]), 4),
            "avg_top_decile_beta_alpha": round(mean([m["test"]["top_decile"].get("mean_beta_alpha") or 0.0 for m in items]), 6),
            "years": [f["year"] for f in folds],
        })
    return [{"folds": folds, "summary": summary}]


def _md(payload: dict[str, Any]) -> str:
    lines = [
        "# Playbook Soft Feature Calibration",
        "",
        f"- Generated: {payload['generated_at']}",
        f"- Input: `{payload['input_json']}`",
        f"- Target: P(net_own_excess > 0), i.e. beat own baseline after costs",
        f"- Split: chronological {payload['split']}",
        "",
        "## Model Comparison",
        "",
        "| Model | Test Brier | Test AUC | Avg pred | Realized | Top decile hit | Top decile own excess | Top decile beta alpha | Verdict |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    baseline = payload["models"][0]
    best_brier = baseline["test"]["brier"]
    best_alpha = baseline["test"]["top_decile"].get("mean_beta_alpha") or 0.0
    for m in payload["models"]:
        t = m["test"]
        top = t["top_decile"]
        better = t["brier"] < best_brier and (top.get("mean_beta_alpha") or 0.0) >= best_alpha
        lines.append(
            f"| {m['name']} | {t['brier']:.6f} | {t['auc']:.4f} | {t['avg_pred']:.1%} | {t['base_rate']:.1%} | "
            f"{(top.get('win_rate') or 0):.1%} | {(top.get('mean_net_own_excess') or 0):+.3%} | "
            f"{(top.get('mean_beta_alpha') or 0):+.3%} | {'candidate' if better else 'not better'} |"
        )
    lines += ["", "## Coefficients", ""]
    for m in payload["models"]:
        lines.append(f"### {m['name']}")
        for c in m["coefficients"]:
            lines.append(f"- `{c['feature']}`: {c['coef_standardized']:+.6f}")
        lines.append("")
    lines += [
        "",
        "## Yearly Walk-Forward CV",
        "",
    ]
    ycv = payload.get("yearly_cv") or {}
    if ycv.get("summary"):
        lines += [
            "| Model | Folds | Avg Brier | Avg AUC | Avg top hit | Avg top beta alpha |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for row in ycv["summary"]:
            lines.append(
                f"| {row['name']} | {row['folds']} | {row['avg_brier']:.6f} | {row['avg_auc']:.4f} | "
                f"{row['avg_top_decile_hit']:.1%} | {row['avg_top_decile_beta_alpha']:+.3%} |"
            )
    else:
        lines.append("- Not run.")
    lines += [
        "## Verdict",
        "",
        payload["verdict"],
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="agent/runs/pullback_playbook_spx_ndx_sox_260625.json")
    ap.add_argument("--output-prefix", default="playbook_soft_feature_calibration_260625")
    ap.add_argument("--train-frac", type=float, default=0.70)
    ap.add_argument("--yearly-cv", action="store_true")
    args = ap.parse_args()

    source = Path(args.input)
    rows = _build_rows(source)
    split = int(len(rows) * args.train_frac)
    train = rows[:split]
    test = rows[split:]

    models = _model_specs(rows)
    results = [_evaluate_model(name, features, train, test) for name, features in models]
    base = results[0]
    candidates = [
        r for r in results[1:]
        if r["test"]["brier"] < base["test"]["brier"]
        and (r["test"]["top_decile"].get("mean_beta_alpha") or -999) >= (base["test"]["top_decile"].get("mean_beta_alpha") or -999)
    ]
    verdict = (
        "No production promotion: keep soft tags outside calibrated win-rate until an out-of-sample model "
        "beats the score-only baseline on Brier while preserving top-decile beta alpha."
    )
    if candidates:
        verdict = (
            "Research candidate only: at least one soft-feature model improved Brier without lowering top-decile beta alpha. "
            "Before production, rerun with point-in-time universe, sector-ETF enrichment saved at event level, and walk-forward yearly CV."
        )

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "input_json": str(source),
        "n_events": len(rows),
        "split": {"train": len(train), "test": len(test), "train_frac": args.train_frac},
        "feature_note": "sector/stock ETF features are included only when the input JSON has sector_enriched=true event fields.",
        "models": results,
        "yearly_cv": (_yearly_cv(rows, models)[0] if args.yearly_cv else {"summary": []}),
        "verdict": verdict,
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out_json = RUNS_DIR / f"{args.output_prefix}.json"
    out_md = RUNS_DIR / f"{args.output_prefix}.md"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(_md(payload), encoding="utf-8")
    print(json.dumps({
        "json": str(out_json),
        "md": str(out_md),
        "n_events": len(rows),
        "baseline_brier": base["test"]["brier"],
        "best_brier": min(r["test"]["brier"] for r in results),
        "verdict": verdict,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
