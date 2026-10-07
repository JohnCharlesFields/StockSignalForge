"""Quantify historical event impact against subsequent price action.

The validator consumes an auditable event CSV and measures whether event scores
line up with forward returns. It writes event-study artifacts that can be used
to calibrate the live launch-score model.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from market_data_service import get_daily_history


DEFAULT_HORIZONS = [1, 3, 5, 10]


def normalize_symbol(symbol: str) -> str:
    return str(symbol or "").upper().replace(".US", "").replace(".", "-")


def load_events(path: Path) -> pd.DataFrame:
    events = pd.read_csv(path)
    if "symbol" not in events.columns:
        raise ValueError("events csv must contain symbol")
    if "score" not in events.columns and "sentiment_score" in events.columns:
        events["score"] = events["sentiment_score"]
    if "score" not in events.columns:
        raise ValueError("events csv must contain score or sentiment_score")
    if "event_type" not in events.columns:
        events["event_type"] = "other"
    if "date" not in events.columns:
        events["date"] = events.get("effective_at", events.get("published_at", ""))
    if "impact_score" not in events.columns:
        events["impact_score"] = events["score"].abs()
    if "confidence" not in events.columns:
        events["confidence"] = 1.0

    events["symbol"] = events["symbol"].map(normalize_symbol)
    events["date"] = pd.to_datetime(events["date"], errors="coerce", utc=True).dt.tz_convert(None).dt.normalize()
    if "effective_at" in events.columns:
        effective = pd.to_datetime(events["effective_at"], errors="coerce", utc=True).dt.tz_convert(None).dt.normalize()
        events["date"] = effective.fillna(events["date"])
    events["score"] = pd.to_numeric(events["score"], errors="coerce")
    events["impact_score"] = pd.to_numeric(events["impact_score"], errors="coerce").fillna(events["score"].abs())
    events["confidence"] = pd.to_numeric(events["confidence"], errors="coerce").fillna(1.0)
    events = events.dropna(subset=["symbol", "date", "score"])
    events = events[events["symbol"].ne("")]
    return events.reset_index(drop=True)


def fetch_history(symbols: Iterable[str], start: pd.Timestamp, end: pd.Timestamp) -> Dict[str, pd.DataFrame]:
    data: Dict[str, pd.DataFrame] = {}
    for symbol in sorted(set(symbols)):
        try:
            hist, _source = get_daily_history(
                symbol,
                start=(start - timedelta(days=10)).strftime("%Y-%m-%d"),
                end=(end + timedelta(days=20)).strftime("%Y-%m-%d"),
            )
        except Exception:
            hist = pd.DataFrame()
        if hist is None or hist.empty:
            continue
        hist = hist.rename(columns=str.lower)
        hist.index = pd.to_datetime(hist.index).tz_localize(None).normalize()
        data[symbol] = hist
    return data


def next_index(index: pd.DatetimeIndex, dt: pd.Timestamp) -> Optional[int]:
    pos = index.searchsorted(dt)
    if pos >= len(index):
        return None
    return int(pos)


def event_study(
    events: pd.DataFrame,
    price_map: Dict[str, pd.DataFrame],
    benchmark: Optional[pd.DataFrame],
    horizons: List[int],
) -> pd.DataFrame:
    rows = []
    bench_close = None
    if benchmark is not None and not benchmark.empty and "close" in benchmark.columns:
        bench_close = benchmark["close"].astype(float)

    for event in events.itertuples(index=False):
        symbol = getattr(event, "symbol")
        hist = price_map.get(symbol)
        if hist is None or hist.empty or "close" not in hist.columns:
            continue
        close = hist["close"].astype(float)
        idx = next_index(close.index, getattr(event, "date"))
        if idx is None:
            continue
        event_date = close.index[idx]
        base_price = float(close.iloc[idx])
        if not base_price or math.isnan(base_price):
            continue
        row = {
            "symbol": symbol,
            "event_date": event_date.strftime("%Y-%m-%d"),
            "event_type": str(getattr(event, "event_type", "other")),
            "score": float(getattr(event, "score", 0.0)),
            "impact_score": float(getattr(event, "impact_score", abs(float(getattr(event, "score", 0.0))))),
            "confidence": float(getattr(event, "confidence", 1.0)),
            "predictor": float(getattr(event, "score", 0.0))
            * float(getattr(event, "impact_score", 1.0))
            * float(getattr(event, "confidence", 1.0)),
            "base_price": base_price,
            "title": str(getattr(event, "title", ""))[:180],
        }
        for horizon in horizons:
            fwd_idx = idx + horizon
            if fwd_idx >= len(close):
                row[f"ret_{horizon}d"] = np.nan
                row[f"abret_{horizon}d"] = np.nan
                continue
            ret = float(close.iloc[fwd_idx] / base_price - 1.0)
            row[f"ret_{horizon}d"] = ret
            abret = ret
            if bench_close is not None:
                b_idx = next_index(bench_close.index, event_date)
                if b_idx is not None and b_idx + horizon < len(bench_close):
                    b_base = float(bench_close.iloc[b_idx])
                    b_fwd = float(bench_close.iloc[b_idx + horizon])
                    if b_base:
                        abret = ret - (b_fwd / b_base - 1.0)
            row[f"abret_{horizon}d"] = abret
        rows.append(row)
    return pd.DataFrame(rows)


def safe_corr(x: pd.Series, y: pd.Series) -> float:
    pair = pd.concat([x, y], axis=1).dropna()
    if len(pair) < 3:
        return float("nan")
    if pair.iloc[:, 0].nunique() < 2 or pair.iloc[:, 1].nunique() < 2:
        return float("nan")
    return float(pair.iloc[:, 0].corr(pair.iloc[:, 1], method="spearman"))


def linear_beta(x: pd.Series, y: pd.Series) -> float:
    pair = pd.concat([x, y], axis=1).dropna()
    if len(pair) < 3:
        return float("nan")
    xv = pair.iloc[:, 0].astype(float).values
    yv = pair.iloc[:, 1].astype(float).values
    if np.nanstd(xv) == 0:
        return float("nan")
    beta = np.polyfit(xv, yv, 1)[0]
    return float(beta)


def normal_pvalue_from_t(t_stat: float) -> float:
    if t_stat is None or pd.isna(t_stat):
        return float("nan")
    return float(math.erfc(abs(float(t_stat)) / math.sqrt(2.0)))


def mean_effect_stats(values: pd.Series) -> dict:
    clean = pd.to_numeric(values, errors="coerce").dropna()
    n = int(len(clean))
    if n == 0:
        return {
            "std_abnormal_return": float("nan"),
            "t_stat": float("nan"),
            "p_value_approx": float("nan"),
            "ci95_low": float("nan"),
            "ci95_high": float("nan"),
        }
    mean = float(clean.mean())
    std = float(clean.std(ddof=1)) if n > 1 else float("nan")
    if n > 1 and std > 0:
        se = std / math.sqrt(n)
        t_stat = mean / se
        ci_low = mean - 1.96 * se
        ci_high = mean + 1.96 * se
        p_value = normal_pvalue_from_t(t_stat)
    else:
        t_stat = float("nan")
        ci_low = float("nan")
        ci_high = float("nan")
        p_value = float("nan")
    return {
        "std_abnormal_return": std,
        "t_stat": float(t_stat),
        "p_value_approx": float(p_value),
        "ci95_low": float(ci_low),
        "ci95_high": float(ci_high),
    }


def summarize(study: pd.DataFrame, horizons: List[int]) -> dict:
    summary: dict = {
        "event_count": int(len(study)),
        "by_horizon": {},
        "by_event_type": {},
        "model": {},
    }
    if study.empty:
        return summary

    for horizon in horizons:
        target = f"abret_{horizon}d"
        raw_target = f"ret_{horizon}d"
        valid = study.dropna(subset=[target, "predictor"])
        if valid.empty:
            continue
        aligned = valid["predictor"] * valid[target]
        effect_stats = mean_effect_stats(valid[target])
        summary["by_horizon"][f"{horizon}d"] = {
            "n": int(len(valid)),
            "mean_forward_return": float(valid[raw_target].mean()),
            "mean_abnormal_return": float(valid[target].mean()),
            **effect_stats,
            "directional_hit_rate": float((aligned > 0).mean()),
            "spearman_ic": safe_corr(valid["predictor"], valid[target]),
            "linear_beta": linear_beta(valid["predictor"], valid[target]),
            "top_quintile_abret": float(valid.nlargest(max(1, len(valid) // 5), "predictor")[target].mean()),
            "bottom_quintile_abret": float(valid.nsmallest(max(1, len(valid) // 5), "predictor")[target].mean()),
        }

    for event_type, group in study.groupby("event_type"):
        stats = {}
        for horizon in horizons:
            target = f"abret_{horizon}d"
            valid = group.dropna(subset=[target, "predictor"])
            if len(valid) < 2:
                continue
            effect_stats = mean_effect_stats(valid[target])
            stats[f"{horizon}d"] = {
                "n": int(len(valid)),
                "mean_abnormal_return": float(valid[target].mean()),
                **effect_stats,
                "hit_rate": float(((valid["predictor"] * valid[target]) > 0).mean()),
                "spearman_ic": safe_corr(valid["predictor"], valid[target]),
                "linear_beta": linear_beta(valid["predictor"], valid[target]),
            }
        if stats:
            summary["by_event_type"][str(event_type)] = stats

    primary = summary["by_horizon"].get("5d") or next(iter(summary["by_horizon"].values()), {})
    primary_horizon = "5d" if "5d" in summary["by_horizon"] else next(iter(summary["by_horizon"].keys()), None)
    beta = primary.get("linear_beta")
    ic = primary.get("spearman_ic")
    type_betas = {}
    if primary_horizon:
        for event_type, stats in summary["by_event_type"].items():
            item = stats.get(primary_horizon)
            if not item:
                continue
            type_beta = item.get("linear_beta")
            if type_beta is None or pd.isna(type_beta):
                continue
            type_betas[event_type] = {
                "beta": float(type_beta),
                "ic": item.get("spearman_ic"),
                "n": item.get("n", 0),
                "mean_abnormal_return": item.get("mean_abnormal_return"),
                "hit_rate": item.get("hit_rate"),
            }
    summary["model"] = {
        "formula": "expected_abnormal_return_h = beta_{event_type,h} * score * impact_score * confidence",
        "fallback_formula": "fallback_expected_abnormal_return_h = beta_h * score * impact_score * confidence",
        "primary_horizon": primary_horizon,
        "primary_beta": beta,
        "primary_ic": ic,
        "event_type_betas": type_betas,
        "usable": bool(beta is not None and not pd.isna(beta) and primary.get("n", 0) >= 20),
        "note": "Use as calibration evidence only; no-key RSS data may have survivorship and coverage bias.",
    }
    return summary


def write_report(summary: dict, output_dir: Path) -> None:
    def fmt_pct(value) -> str:
        if value is None or pd.isna(value):
            return "NA"
        return f"{float(value):.2%}"

    def fmt_float(value, digits: int = 3) -> str:
        if value is None or pd.isna(value):
            return "NA"
        return f"{float(value):.{digits}f}"

    lines = [
        "# 事件影响定量验证报告",
        "",
        f"有效历史事件数: {summary.get('event_count', 0)}",
        "",
        "## 总体事件研究",
        "",
        "| 窗口 | 样本数 | 平均收益 | 平均超额 | 方向命中率 | Spearman IC | 线性Beta | Top-Bottom |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for horizon, row in summary.get("by_horizon", {}).items():
        top = row.get("top_quintile_abret")
        bottom = row.get("bottom_quintile_abret")
        spread = None if top is None or bottom is None else top - bottom
        lines.append(
            f"| {horizon} | {row.get('n', 0)} | {fmt_pct(row.get('mean_forward_return'))} | "
            f"{fmt_pct(row.get('mean_abnormal_return'))} | {fmt_pct(row.get('directional_hit_rate'))} | "
            f"{fmt_float(row.get('spearman_ic'), 3)} | {fmt_float(row.get('linear_beta'), 4)} | {fmt_pct(spread)} |"
        )
    lines.extend(["", "## 可量化模型", ""])
    model = summary.get("model", {})
    lines.append(f"- 预测公式: `{model.get('formula', '')}`")
    lines.append(f"- 主窗口: {model.get('primary_horizon') or '样本不足'}")
    lines.append(f"- 主窗口 Beta: {model.get('primary_beta')}")
    lines.append(f"- 主窗口 IC: {model.get('primary_ic')}")
    lines.append(f"- 是否达到可用样本门槛: {model.get('usable')}")
    lines.append("")
    lines.append("说明: 这是事件分数与后续超额收益的统计校准，不是确定性预测。样本不足或覆盖偏差会显著影响结论。")
    (output_dir / "event_impact_report.md").write_text("\n".join(lines), encoding="utf-8")


def json_safe(value):
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [json_safe(v) for v in value]
    if isinstance(value, (np.floating, float)):
        if pd.isna(value) or np.isinf(value):
            return None
        return float(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    return value


def write_report(summary: dict, output_dir: Path) -> None:
    def fmt_pct(value) -> str:
        if value is None or pd.isna(value):
            return "NA"
        return f"{float(value):.2%}"

    def fmt_float(value, digits: int = 3) -> str:
        if value is None or pd.isna(value):
            return "NA"
        return f"{float(value):.{digits}f}"

    lines = [
        "# Event Impact Quantitative Validation Report",
        "",
        f"Validated events: {summary.get('event_count', 0)}",
        "",
        "## Event Study",
        "",
        "| Window | N | Mean Return | Mean Abret | 95% CI | t-stat | p-value | Hit Rate | Spearman IC | Beta | Top-Bottom |",
        "|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for horizon, row in summary.get("by_horizon", {}).items():
        top = row.get("top_quintile_abret")
        bottom = row.get("bottom_quintile_abret")
        spread = None if top is None or bottom is None else top - bottom
        lines.append(
            f"| {horizon} | {row.get('n', 0)} | {fmt_pct(row.get('mean_forward_return'))} | "
            f"{fmt_pct(row.get('mean_abnormal_return'))} | "
            f"{fmt_pct(row.get('ci95_low'))}..{fmt_pct(row.get('ci95_high'))} | "
            f"{fmt_float(row.get('t_stat'), 2)} | {fmt_float(row.get('p_value_approx'), 3)} | "
            f"{fmt_pct(row.get('directional_hit_rate'))} | "
            f"{fmt_float(row.get('spearman_ic'), 3)} | {fmt_float(row.get('linear_beta'), 4)} | {fmt_pct(spread)} |"
        )

    model = summary.get("model", {})
    lines.extend(
        [
            "",
            "## Quantitative Model",
            "",
            f"- Prediction formula: `{model.get('formula', '')}`",
            f"- Primary horizon: {model.get('primary_horizon') or 'insufficient sample'}",
            f"- Primary beta: {model.get('primary_beta')}",
            f"- Primary IC: {model.get('primary_ic')}",
            f"- Usable sample gate: {model.get('usable')}",
            "",
            "Note: this is statistical calibration between event scores and subsequent abnormal returns, not a deterministic forecast. Small samples and coverage bias can materially change results.",
        ]
    )
    (output_dir / "event_impact_report.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events-csv", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--benchmark", default="SPY")
    parser.add_argument("--horizons", default="1,3,5,10")
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    horizons = [int(x) for x in str(args.horizons).split(",") if x.strip()]
    events = load_events(Path(args.events_csv))
    if events.empty:
        raise SystemExit("no valid events")
    start = events["date"].min()
    end = events["date"].max() + timedelta(days=max(horizons) + 10)
    price_map = fetch_history(events["symbol"], start, end)
    benchmark = fetch_history([args.benchmark], start, end).get(args.benchmark)
    study = event_study(events, price_map, benchmark, horizons)
    study.to_csv(output_dir / "event_impact_study.csv", index=False, encoding="utf-8")
    summary = json_safe(summarize(study, horizons))
    (output_dir / "event_impact_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    write_report(summary, output_dir)
    print(json.dumps({"events": len(events), "validated": len(study), "output_dir": str(output_dir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
