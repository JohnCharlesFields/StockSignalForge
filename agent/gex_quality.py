"""Validate OI-derived GEX before using it as research evidence."""
from __future__ import annotations

import math
from typing import Any


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def unavailable_gex(reason: str, payload: dict | None = None) -> dict:
    out = dict(payload or {})
    out.update(available=False, quality_status="unavailable", reason=reason,
               net_gex=None, call_gex=None, put_gex=None, gamma_flip=None,
               distance_to_flip=None, abs_imbalance=None, gex_pct_of_100_shares=None,
               regime="unknown", risk_level="unknown",
               interpretation_cn="GEX\u6570\u636e\u4e0d\u53ef\u6838\u9a8c\uff0c\u4e0d\u8ba1\u5165\u8986\u76d6\u7387\u6216\u7814\u7a76\u52a0\u5206\u3002")
    return out


def normalize_gex(payload: Any) -> dict:
    """Legacy snapshots need finite nonempty exposure, not just available=True."""
    if not isinstance(payload, dict) or not payload.get("available"):
        return unavailable_gex((payload or {}).get("reason", "missing_gex") if isinstance(payload, dict) else "missing_gex", payload if isinstance(payload, dict) else None)
    values = [finite_number(payload.get(k)) for k in ("net_gex", "call_gex", "put_gex")]
    if any(value is None for value in values):
        return unavailable_gex("invalid_or_missing_exposure", payload)
    net, call, put = values
    total = abs(call) + abs(put)
    if total == 0:
        return unavailable_gex("no_open_interest_exposure", payload)
    # Published summaries round each side and the net independently to cents.
    if not math.isclose(net, call + put, abs_tol=.03, rel_tol=1e-9):
        return unavailable_gex("inconsistent_exposure", payload)
    out = dict(payload)
    out.update(quality_status="verified_exposure", net_gex=net, call_gex=call, put_gex=put)
    if net == 0:
        out.update(gamma_flip=None, distance_to_flip=None, regime="neutral", risk_level="medium")
    elif finite_number(out.get("gamma_flip")) is None:
        out.update(gamma_flip=None, distance_to_flip=None)
    return out


def snapshot_gex_view(snapshot: Any) -> Any:
    """Correct legacy display evidence without writing or recomputing a snapshot."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("payload"), dict):
        return snapshot
    payload = snapshot["payload"]
    if not isinstance(payload.get("rows"), list):
        return snapshot
    rows = []
    for original in payload["rows"]:
        if not isinstance(original, dict):
            rows.append(original)
            continue
        row = dict(original)
        raw = row.get("gamma_exposure")
        if raw is None and not row.get("gex_level") and not row.get("gex_regime"):
            rows.append(row)
            continue
        gex = normalize_gex(raw)
        row.update(gamma_exposure=gex, gex_level=gex.get("risk_level") if gex["available"] else None,
                   gex_regime=gex.get("regime") if gex["available"] else None)
        if isinstance(raw, dict) and raw.get("available") and not gex["available"]:
            # The persisted aggregate may include this invalid component. Do not
            # present it as fresh evidence or replace it with another probability.
            for key in ("unified_probability", "unified_probability_low", "unified_probability_high",
                        "unified_score", "overall_sort_score"):
                if key in row:
                    row[key] = None
            row["gex_quality_warning"] = "cached_aggregate_requires_review"
        rows.append(row)
    return {**snapshot, "payload": {**payload, "rows": rows}}
