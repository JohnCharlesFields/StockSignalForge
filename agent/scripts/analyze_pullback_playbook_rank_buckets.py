#!/usr/bin/env python3
"""Rank-bucket audit for pullback playbook filters.

This script reads the JSON produced by ``backtest_pullback_playbook_filters.py``
and checks whether candidate scores are monotonic: higher-ranked buckets should
have higher realized net beta-adjusted alpha.  It is intentionally offline and
does not fetch market data.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import random
from pathlib import Path
from statistics import mean, median
from typing import Any


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _clip(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _cluster_ci(rows: list[dict[str, Any]], key: str, n_boot: int = 2000, seed: int = 42) -> tuple[float | None, float | None]:
    by: dict[str, list[float]] = {}
    for row in rows:
        val = _num(row.get(key))
        if val is not None:
            by.setdefault(str(row.get("ticker") or row.get("symbol") or ""), []).append(val)
    clusters = [name for name, vals in by.items() if name and vals]
    if len(clusters) < 2:
        return None, None
    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(n_boot):
        sample = [rng.choice(clusters) for _ in clusters]
        vals = [v for c in sample for v in by[c]]
        if vals:
            estimates.append(sum(vals) / len(vals))
    estimates.sort()
    return estimates[int(0.025 * (len(estimates) - 1))], estimates[int(0.975 * (len(estimates) - 1))]


def _normalize(values: list[float], value: float) -> float:
    if not values:
        return 0.5
    ordered = sorted(values)
    lo = ordered[int(0.05 * (len(ordered) - 1))]
    hi = ordered[int(0.95 * (len(ordered) - 1))]
    if hi <= lo:
        return 0.5
    return _clip((value - lo) / (hi - lo))


def _attach_scores(rows: list[dict[str, Any]]) -> None:
    rs_raw = []
    for row in rows:
        r60 = _num(row.get("ret60_vs_benchmark"), 0.0) or 0.0
        r126 = _num(row.get("ret126_vs_benchmark"), 0.0) or 0.0
        rs_raw.append(0.65 * r60 + 0.35 * r126)
    for row, raw in zip(rows, rs_raw):
        pullback_hv = _num(row.get("pullback_hv_score"), 0.0) or 0.0
        rs_score = _normalize(rs_raw, raw)
        # Fixed a-priori weights: mostly preserve the validated pullback_hv
        # score, while allowing relative strength to nudge ordering.
        composite = 0.70 * pullback_hv + 0.30 * rs_score
        row["_rs_score"] = rs_score
        row["_composite_score"] = composite


def _bucket_rows(rows: list[dict[str, Any]], score_key: str, bucket_count: int) -> list[dict[str, Any]]:
    if not rows:
        return []
    ordered = sorted(rows, key=lambda r: _num(r.get(score_key), -999.0) or -999.0, reverse=True)
    buckets: list[dict[str, Any]] = []
    n = len(ordered)
    for idx in range(bucket_count):
        start = int(idx * n / bucket_count)
        end = int((idx + 1) * n / bucket_count)
        part = ordered[start:end]
        vals = [_num(r.get("net_beta_alpha")) for r in part]
        vals = [v for v in vals if v is not None]
        if not vals:
            buckets.append({"bucket": idx + 1, "n": 0})
            continue
        lo, hi = _cluster_ci(part, "net_beta_alpha")
        buckets.append({
            "bucket": idx + 1,
            "rank_range": f"{start + 1}-{end}",
            "n": len(vals),
            "symbols": len({str(r.get("ticker")) for r in part}),
            "score_min": round(min(float(r.get(score_key) or 0.0) for r in part), 6),
            "score_max": round(max(float(r.get(score_key) or 0.0) for r in part), 6),
            "mean_beta_alpha": round(mean(vals), 6),
            "median_beta_alpha": round(median(vals), 6),
            "alpha_win_rate": round(sum(v > 0 for v in vals) / len(vals), 4),
            "ci_low": round(lo, 6) if lo is not None else None,
            "ci_high": round(hi, 6) if hi is not None else None,
            "significant": bool(lo is not None and lo > 0),
        })
    return buckets


def _monotonic_report(buckets: list[dict[str, Any]]) -> dict[str, Any]:
    vals = [_num(b.get("mean_beta_alpha")) for b in buckets if b.get("n")]
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        return {"monotone_nonincreasing": False, "top_bucket_best": False, "spread_top_minus_bottom": None}
    top_best = vals[0] == max(vals)
    nonincreasing = all(vals[i] >= vals[i + 1] for i in range(len(vals) - 1))
    return {
        "monotone_nonincreasing": bool(nonincreasing),
        "top_bucket_best": bool(top_best),
        "spread_top_minus_bottom": round(vals[0] - vals[-1], 6),
    }


def _summarize_group(label: str, rows: list[dict[str, Any]], score_key: str, bucket_count: int) -> dict[str, Any]:
    buckets = _bucket_rows(rows, score_key, bucket_count)
    mono = _monotonic_report(buckets)
    vals = [_num(r.get("net_beta_alpha")) for r in rows]
    vals = [v for v in vals if v is not None]
    lo, hi = _cluster_ci(rows, "net_beta_alpha")
    return {
        "label": label,
        "score_key": score_key,
        "n": len(vals),
        "symbols": len({str(r.get("ticker")) for r in rows}),
        "overall_mean_beta_alpha": round(mean(vals), 6) if vals else None,
        "overall_ci_low": round(lo, 6) if lo is not None else None,
        "overall_ci_high": round(hi, 6) if hi is not None else None,
        "overall_significant": bool(lo is not None and lo > 0),
        "buckets": buckets,
        "monotonicity": mono,
    }


def _md(payload: dict[str, Any]) -> str:
    lines = [
        "# Pullback Playbook Rank-Bucket Audit",
        "",
        f"- Generated: {payload['generated_at']}",
        f"- Source: `{payload['source_json']}`",
        f"- Bucket count: {payload['bucket_count']}",
        "- Metric: net beta-adjusted alpha",
        "",
    ]
    for group in payload["groups"]:
        mono = group["monotonicity"]
        lines += [
            f"## {group['label']}",
            "",
            f"- n: {group['n']}, symbols: {group['symbols']}",
            f"- overall mean beta alpha: {float(group.get('overall_mean_beta_alpha') or 0)*100:+.3f}%",
            f"- CI95: [{group.get('overall_ci_low')}, {group.get('overall_ci_high')}]",
            f"- top bucket best: {mono.get('top_bucket_best')}",
            f"- monotone non-increasing: {mono.get('monotone_nonincreasing')}",
            f"- top-bottom spread: {float(mono.get('spread_top_minus_bottom') or 0)*100:+.3f}%",
            "",
            "| Bucket | n | Score range | Mean beta alpha | Hit | CI95 | Verdict |",
            "|---:|---:|---|---:|---:|---|---|",
        ]
        for b in group["buckets"]:
            ci = f"[{b.get('ci_low')}, {b.get('ci_high')}]" if b.get("ci_low") is not None else "--"
            lines.append(
                f"| {b.get('bucket')} | {b.get('n', 0)} | {b.get('score_min')} - {b.get('score_max')} | "
                f"{float(b.get('mean_beta_alpha') or 0)*100:+.3f}% | "
                f"{float(b.get('alpha_win_rate') or 0):.1%} | {ci} | "
                f"{'PASS' if b.get('significant') else 'not passed'} |"
            )
        lines.append("")
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
    ap.add_argument("--bucket-count", type=int, default=5)
    ap.add_argument("--output-prefix", default="")
    args = ap.parse_args()

    source = Path(args.input)
    data = json.loads(source.read_text(encoding="utf-8"))
    rows = list(data.get("events") or [])
    cutoff = _num(data.get("top_cutoff"), 0.0) or 0.0
    top = [r for r in rows if (_num(r.get("pullback_hv_score"), 0.0) or 0.0) >= cutoff]
    _attach_scores(top)
    market_liquid_rs = [r for r in top if r.get("market_ok") and r.get("liquid") and r.get("rs_filter")]
    rs_only = [r for r in top if r.get("rs_filter")]

    groups = [
        _summarize_group("baseline top bucket sorted by pullback_hv", top, "pullback_hv_score", args.bucket_count),
        _summarize_group("relative strength subset sorted by composite", rs_only, "_composite_score", args.bucket_count),
        _summarize_group("market + liquid + RS sorted by composite", market_liquid_rs, "_composite_score", args.bucket_count),
        _summarize_group("market + liquid + RS sorted by RS only", market_liquid_rs, "_rs_score", args.bucket_count),
    ]
    mlrs = groups[2]
    top_bucket = mlrs["buckets"][0] if mlrs["buckets"] else {}
    verdict = (
        "Do not promote market+liquid+RS into a hard ranking replacement yet. "
        "The filter-level edge is positive, but bucket monotonicity must be inspected: "
        f"top_bucket_best={mlrs['monotonicity'].get('top_bucket_best')}, "
        f"monotone={mlrs['monotonicity'].get('monotone_nonincreasing')}, "
        f"top_bucket_CI_low={top_bucket.get('ci_low')}. "
        "If top bucket is not best or CI is not above zero, use it only as a soft eligibility/filter tag."
    )
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source_json": str(source),
        "bucket_count": args.bucket_count,
        "method": "rank_bucket_monotonicity_audit",
        "score_definitions": {
            "_rs_score": "percentile-like normalization of 0.65*ret60_vs_benchmark + 0.35*ret126_vs_benchmark",
            "_composite_score": "0.70*pullback_hv_score + 0.30*_rs_score",
        },
        "groups": groups,
        "verdict": verdict,
    }
    out_base = args.output_prefix or f"pullback_playbook_rank_buckets_{dt.datetime.now(dt.timezone.utc).strftime('%y%m%d')}"
    out_json = source.parent / f"{out_base}.json"
    out_md = source.parent / f"{out_base}.md"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(_md(payload), encoding="utf-8")
    print(f"json={out_json} md={out_md}")
    for group in groups:
        mono = group["monotonicity"]
        print(
            f"{group['label']}: n={group['n']} mean={float(group.get('overall_mean_beta_alpha') or 0)*100:+.3f}% "
            f"top_best={mono.get('top_bucket_best')} monotone={mono.get('monotone_nonincreasing')} "
            f"spread={float(mono.get('spread_top_minus_bottom') or 0)*100:+.3f}%"
        )
        for b in group["buckets"]:
            print(
                f"  B{b.get('bucket')}: n={b.get('n')} mean={float(b.get('mean_beta_alpha') or 0)*100:+.3f}% "
                f"hit={float(b.get('alpha_win_rate') or 0):.1%} ci=[{b.get('ci_low')},{b.get('ci_high')}]"
            )


if __name__ == "__main__":
    main()
