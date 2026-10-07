"""Signal score -> realized-outcome calibration (R1 closed loop).

This module turns raw, hand-tuned signal scores (launch_score, daily_tunnel)
into *empirically calibrated* probabilities by binning historical events by
score and measuring how often the net-of-cost forward excess return was
actually positive.  A monotone (isotonic) fit maps score -> P(excess > 0).

Design notes
------------
* The fit (Pool-Adjacent-Violators) runs at *build* time.  The result is
  persisted as a plain step function (bucket edges + values), so the live
  ``calibrate()`` read path is pure-python and never imports scikit-learn.
* "Win" is defined as a positive **net-of-cost excess return versus each
  symbol's own unconditional baseline**.  A round-trip cost of
  ``2 * cost_bps`` is subtracted from every event's forward return before the
  excess and the hit flag are computed.
* Scores are normalised to the 0..1 range per signal type so a single curve
  schema works for both ``launch`` (already 0..1) and ``daily_tunnel``
  (0..100 -> divide by 100).  Use :func:`normalize_score` on both sides.

Keep this module ASCII-only so Windows terminals and container builds do not
turn labels into mojibake.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

CURVE_VERSION = "calib_v1"

# Minimum samples in the bucket that contains the queried score before we
# trust the empirical number instead of falling back to the legacy sigmoid.
DEFAULT_MIN_SUPPORT = 25
# Target number of (equal-count) score buckets; adjacent thin buckets merge.
DEFAULT_TARGET_BUCKETS = 10
DEFAULT_MIN_BUCKET_N = 20
# Legacy hand-tuned sigmoid, mirrored from peer_earnings_signal_service so the
# fallback matches what the rest of the system used before calibration existed.
_SIGMOID_SLOPE = 5.0
_SIGMOID_CENTER = 0.52


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


CONTRARIAN_SUFFIX = "_contrarian"


def normalize_score(signal_type: str, score: Any) -> float:
    """Map a native signal score into the shared 0..1 calibration axis.

    A ``<base>_contrarian`` signal type inverts the base score: R1 calibration
    showed the raw launch/daily_tunnel scores are *inverted* vs net-of-cost
    excess (low score -> beats baseline), so the contrarian axis ``1 - base``
    is the hypothesis that the oversold end carries the edge.
    """
    if signal_type.endswith(CONTRARIAN_SUFFIX):
        base = signal_type[: -len(CONTRARIAN_SUFFIX)]
        return min(1.0, max(0.0, 1.0 - normalize_score(base, score)))
    value = _finite(score)
    if signal_type == "daily_tunnel":
        value = value / 100.0
    return min(1.0, max(0.0, value))


def _cluster_bootstrap_excess_ci(
    rows: Sequence[Dict[str, Any]],
    *,
    n_bootstrap: int = 800,
    seed: int = 42,
) -> tuple[Optional[float], Optional[float]]:
    """95% CI for mean net excess, resampling by ticker to respect clustering."""
    clusters: Dict[str, List[float]] = {}
    for row in rows:
        clusters.setdefault(str(row.get("ticker") or ""), []).append(float(row.get("excess_net", 0.0)))
    tickers = [t for t in clusters if t]
    if len(tickers) < 8:
        return (None, None)
    import random

    rng = random.Random(seed)
    estimates: List[float] = []
    for _ in range(n_bootstrap):
        sample = [rng.choice(tickers) for _ in tickers]
        values = [v for t in sample for v in clusters[t]]
        if values:
            estimates.append(sum(values) / len(values))
    if not estimates:
        return (None, None)
    estimates.sort()
    return (
        round(estimates[int(0.025 * (len(estimates) - 1))], 6),
        round(estimates[int(0.975 * (len(estimates) - 1))], 6),
    )


def legacy_sigmoid(score: float) -> float:
    """The pre-calibration hand-tuned probability proxy (0..1 score in)."""
    return 1.0 / (1.0 + math.exp(-_SIGMOID_SLOPE * (_finite(score) - _SIGMOID_CENTER)))


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score confidence interval for a binomial proportion."""
    if total <= 0:
        return (0.0, 0.0)
    phat = successes / total
    denom = 1.0 + z * z / total
    center = (phat + z * z / (2 * total)) / denom
    margin = (z * math.sqrt((phat * (1 - phat) + z * z / (4 * total)) / total)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def _pav(points: Sequence[tuple[float, float, float]]) -> List[float]:
    """Weighted Pool-Adjacent-Violators isotonic (non-decreasing) fit.

    ``points`` is a sequence of (x, y, weight) already sorted by x.  Returns the
    fitted y values aligned with the input order.
    """
    blocks: List[List[float]] = []  # each block: [sum_wy, sum_w, count]
    for _x, y, weight in points:
        w = max(weight, 1e-9)
        blocks.append([y * w, w, 1.0])
        while len(blocks) >= 2:
            prev_mean = blocks[-2][0] / blocks[-2][1]
            cur_mean = blocks[-1][0] / blocks[-1][1]
            if prev_mean <= cur_mean + 1e-12:
                break
            merged = [blocks[-2][0] + blocks[-1][0], blocks[-2][1] + blocks[-1][1], blocks[-2][2] + blocks[-1][2]]
            blocks.pop()
            blocks.pop()
            blocks.append(merged)
    fitted: List[float] = []
    for block in blocks:
        mean = block[0] / block[1]
        fitted.extend([mean] * int(block[2]))
    return fitted


def _quantile_buckets(scores: List[float], target: int) -> List[float]:
    """Return interior cut points for up to ``target`` equal-count buckets."""
    ordered = sorted(scores)
    n = len(ordered)
    if n == 0 or target <= 1:
        return []
    cuts: List[float] = []
    for i in range(1, target):
        idx = min(n - 1, max(0, int(round(i * n / target)) - 1))
        cut = ordered[idx]
        if not cuts or cut > cuts[-1] + 1e-12:
            cuts.append(cut)
    return cuts


def build_calibration(
    events: Sequence[Dict[str, Any]],
    *,
    signal_type: str,
    horizon: int,
    cost_bps: float = 5.0,
    target_buckets: int = DEFAULT_TARGET_BUCKETS,
    min_bucket_n: int = DEFAULT_MIN_BUCKET_N,
) -> Dict[str, Any]:
    """Build a calibration curve from replay/live events.

    Each event must carry ``score`` (native units for ``signal_type``),
    ``forward_return`` and ``excess_return`` (both gross), and ``ticker``.
    Returns a JSON-serialisable curve dict ready for persistence.
    """
    round_trip_cost = 2.0 * max(0.0, _finite(cost_bps)) / 10000.0
    rows: List[Dict[str, Any]] = []
    for event in events:
        score = normalize_score(signal_type, event.get("score"))
        forward_net = _finite(event.get("forward_return")) - round_trip_cost
        excess_net = _finite(event.get("excess_return")) - round_trip_cost
        rows.append({
            "score": score,
            "forward_net": forward_net,
            "excess_net": excess_net,
            "win": 1 if excess_net > 0 else 0,
            "ticker": str(event.get("ticker") or ""),
        })

    buckets: List[Dict[str, Any]] = []
    pav_points: List[tuple[float, float, float]] = []
    merged: List[List[Dict[str, Any]]] = []
    if rows:
        cuts = _quantile_buckets([row["score"] for row in rows], target_buckets)
        edges = [0.0] + cuts + [1.0 + 1e-9]
        # Assign rows to half-open buckets [lo, hi); merge thin trailing buckets.
        grouped: List[List[Dict[str, Any]]] = [[] for _ in range(len(edges) - 1)]
        for row in rows:
            placed = False
            for b in range(len(edges) - 1):
                if edges[b] <= row["score"] < edges[b + 1]:
                    grouped[b].append(row)
                    placed = True
                    break
            if not placed:
                grouped[-1].append(row)
        carry: List[Dict[str, Any]] = []
        for group in grouped:
            carry = carry + group
            if len(carry) >= min_bucket_n:
                merged.append(carry)
                carry = []
        if carry:
            if merged:
                merged[-1].extend(carry)
            else:
                merged.append(carry)
        for group in merged:
            if not group:
                continue
            n = len(group)
            wins = sum(row["win"] for row in group)
            hit_rate = wins / n
            ci_low, ci_high = wilson_interval(wins, n)
            buckets.append({
                "lo": round(min(row["score"] for row in group), 6),
                "hi": round(max(row["score"] for row in group), 6),
                "center": round(sum(row["score"] for row in group) / n, 6),
                "n": n,
                "hit_rate": round(hit_rate, 6),
                "ci_low": round(ci_low, 6),
                "ci_high": round(ci_high, 6),
                "mean_forward_net": round(sum(row["forward_net"] for row in group) / n, 6),
                "mean_excess_net": round(sum(row["excess_net"] for row in group) / n, 6),
            })
            pav_points.append((buckets[-1]["center"], hit_rate, float(n)))

    iso_values = _pav(pav_points) if pav_points else []
    for bucket, group, iso in zip(buckets, merged, iso_values):
        bucket["p_iso"] = round(iso, 6)
        lo, hi = _cluster_bootstrap_excess_ci(group)
        bucket["excess_ci_low"] = lo
        bucket["excess_ci_high"] = hi

    # Edge verdict: does the top (highest-score) bucket beat its baseline with a
    # cluster-bootstrap CI that excludes zero?  This is what flips a signal from
    # "low_edge" to "validated" -- and it stays honest when the answer is no.
    edge: Dict[str, Any] = {"validated": False}
    if buckets:
        top = buckets[-1]
        probs = [b.get("p_iso", b.get("hit_rate", 0.5)) for b in buckets]
        spread = (max(probs) - min(probs)) if probs else 0.0
        ci_low = top.get("excess_ci_low")
        edge = {
            "validated": bool(
                top.get("n", 0) >= DEFAULT_MIN_SUPPORT
                and top.get("mean_excess_net", 0.0) > 0
                and ci_low is not None
                and ci_low > 0
            ),
            "spread": round(spread, 6),
            "top_bucket": {
                "score_range": [top.get("lo"), top.get("hi")],
                "n": top.get("n"),
                "mean_excess_net": top.get("mean_excess_net"),
                "excess_ci": [top.get("excess_ci_low"), top.get("excess_ci_high")],
                "hit_rate": top.get("hit_rate"),
            },
        }

    total = len(rows)
    total_wins = sum(row["win"] for row in rows)
    clusters = len({row["ticker"] for row in rows if row["ticker"]})
    return {
        "curve_version": CURVE_VERSION,
        "signal_type": signal_type,
        "horizon_days": int(horizon),
        "score_unit": "normalized_0_1",
        "cost_bps": float(cost_bps),
        "buckets": buckets,
        "global": {
            "events": total,
            "clusters": clusters,
            "hit_rate": round(total_wins / total, 6) if total else 0.0,
            "mean_forward_net": round(sum(row["forward_net"] for row in rows) / total, 6) if total else 0.0,
            "mean_excess_net": round(sum(row["excess_net"] for row in rows) / total, 6) if total else 0.0,
            "edge": edge,
        },
    }


def apply_calibration(curve: Dict[str, Any], score: float, *, min_support: int = DEFAULT_MIN_SUPPORT) -> Dict[str, Any]:
    """Look up an already-normalised 0..1 score in a built curve."""
    buckets = (curve or {}).get("buckets") or []
    if not buckets:
        return {"p_calibrated": None, "expected_return": None, "bucket_n": 0, "support_ok": False}
    value = min(1.0, max(0.0, _finite(score)))
    chosen = None
    for bucket in buckets:
        if bucket["lo"] <= value <= bucket["hi"]:
            chosen = bucket
            break
    if chosen is None:
        # Outside observed range -> snap to nearest edge bucket.
        chosen = buckets[0] if value < buckets[0]["lo"] else buckets[-1]
    bucket_n = int(chosen.get("n", 0))
    return {
        "p_calibrated": chosen.get("p_iso", chosen.get("hit_rate")),
        "expected_return": chosen.get("mean_forward_net"),
        "expected_excess": chosen.get("mean_excess_net"),
        "ci_low": chosen.get("ci_low"),
        "ci_high": chosen.get("ci_high"),
        "bucket_n": bucket_n,
        "support_ok": bucket_n >= min_support,
    }


def calibrate(
    signal_type: str,
    score: Any,
    horizon: int = 5,
    *,
    min_support: int = DEFAULT_MIN_SUPPORT,
) -> Dict[str, Any]:
    """Live read entry point: return a calibrated probability for one score.

    Reads the active persisted curve for ``(signal_type, horizon)``.  When no
    curve exists or the local bucket support is too thin, it falls back to the
    legacy sigmoid and labels ``source = 'fallback_sigmoid'``.  Never raises.
    """
    normalized = normalize_score(signal_type, score)
    base = {
        "signal_type": signal_type,
        "horizon_days": int(horizon),
        "score": round(normalized, 6),
    }
    curve_row: Optional[Dict[str, Any]] = None
    try:
        from app_database import signal_calibration_active

        curve_row = signal_calibration_active(signal_type, int(horizon))
    except Exception:
        curve_row = None

    if curve_row and isinstance(curve_row.get("curve"), dict):
        applied = apply_calibration(curve_row["curve"], normalized, min_support=min_support)
        if applied.get("support_ok") and applied.get("p_calibrated") is not None:
            base.update(applied)
            base.update({
                "source": "calibrated",
                "calibration_id": curve_row.get("calibration_id"),
                "generated_at": curve_row.get("generated_at"),
                "event_count": curve_row.get("event_count"),
            })
            return base
        # Curve exists but the queried region is under-sampled: surface the
        # empirical numbers as context but still mark it a fallback.
        base.update({k: applied.get(k) for k in ("bucket_n", "ci_low", "ci_high")})

    base.update({
        "p_calibrated": round(legacy_sigmoid(normalized), 6),
        "expected_return": None,
        "expected_excess": None,
        "support_ok": False,
        "source": "fallback_sigmoid",
        "calibration_id": curve_row.get("calibration_id") if curve_row else None,
    })
    return base
