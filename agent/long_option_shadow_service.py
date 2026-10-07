"""Forward-only stock-direction audit for the long-option research board.

No option contract or option P/L is inferred. Predictions are inserted before
the next session opens and are never overwritten by later screening runs.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from app_database import connection
from cost_model import equity_round_trip_cost
from market_calendar import is_trading_day, most_recent_session
from market_data_service import external_data_scope, get_daily_history

EASTERN = ZoneInfo("America/New_York")
MODEL_VERSION = "consensus_v1_threshold_012"
HORIZONS = (1, 3, 5, 10)
PRIMARY_HORIZON = 5


def _next_open_utc(signal_date: date) -> datetime:
    following = signal_date + timedelta(days=1)
    while not is_trading_day(following):
        following += timedelta(days=1)
    return datetime.combine(following, time(9, 30), tzinfo=EASTERN).astimezone(timezone.utc)


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def record_snapshot(snapshot: dict[str, Any], *, source: str = "manual",
                    now: datetime | None = None) -> dict[str, Any]:
    """Log eligible rows once. A stale/late snapshot is auditable but not live OOS."""
    recorded = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        signal_date = date.fromisoformat(str(snapshot["data_as_of"]))
        generated = _aware(str(snapshot["generated_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        return {"inserted": 0, "eligible": 0, "reason": f"invalid_snapshot_time:{type(exc).__name__}"}
    timely = (signal_date == most_recent_session(recorded)
              and generated <= recorded < _next_open_utc(signal_date)
              and generated.astimezone(EASTERN).date() >= signal_date)
    mode = "live" if timely else "late_replay"
    candidates = []
    for row in snapshot.get("rows") or []:
        symbol = str(row.get("symbol") or "").strip().upper()
        status = row.get("status")
        direction = row.get("side") if status == "signal" else "WAIT"
        if (not symbol or status not in {"signal", "no_direction"}
                or direction not in {"C", "P", "WAIT"}
                or row.get("stock_data_as_of") != signal_date.isoformat()
                or row.get("benchmark_data_as_of") != signal_date.isoformat()):
            continue
        features = {key: row.get(key) for key in (
            "bull_consensus", "bear_consensus", "relative_strength_20d", "hv20",
            "signal_score", "stock_data_as_of", "benchmark_data_as_of")}
        candidates.append((symbol, direction, row.get("spot"), row.get("net_consensus"),
                           json.dumps(features, ensure_ascii=False, allow_nan=False)))
    inserted = 0
    with connection() as conn:
        for symbol, direction, spot, net, features_json in candidates:
            for horizon in HORIZONS:
                key = f"{signal_date}|{symbol}|{horizon}|{MODEL_VERSION}"
                prediction_id = hashlib.sha256(key.encode("ascii")).hexdigest()[:32]
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO long_option_direction_predictions(
                        prediction_id, signal_date, symbol, horizon_days, model_version,
                        direction, source, mode, recorded_at, snapshot_generated_at,
                        stock_price, net_consensus, features_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (prediction_id, signal_date.isoformat(), symbol, horizon, MODEL_VERSION,
                     direction, source, mode, recorded.isoformat(), generated.isoformat(),
                     spot, net, features_json),
                )
                inserted += cursor.rowcount
    return {"inserted": inserted, "eligible": len(candidates), "mode": mode,
            "signal_date": signal_date.isoformat(), "horizons": list(HORIZONS)}


def record_daily_from_cache(*, now: datetime | None = None) -> dict[str, Any]:
    """Use only cached OHLCV; never start the expensive leader/option-volume scan."""
    recorded = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    session = most_recent_session(recorded)
    if recorded >= _next_open_utc(session):
        return {"inserted": 0, "reason": "next_session_already_open"}
    from long_option_screen_service import SNAPSHOT_PATH, _screen_one
    from scripts.backtest_leader_long_options import MAGNIFICENT_SEVEN

    symbols = set(MAGNIFICENT_SEVEN)
    if SNAPSHOT_PATH.exists():
        try:
            prior = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
            symbols.update(str(row.get("symbol") or "").upper() for row in prior.get("rows") or [])
        except (OSError, ValueError, TypeError):
            pass
    symbols.discard("")
    with connection() as conn:
        existing = {row["symbol"] for row in conn.execute(
            """SELECT symbol FROM long_option_direction_predictions
               WHERE signal_date=? AND horizon_days=? AND model_version=?""",
            (session.isoformat(), PRIMARY_HORIZON, MODEL_VERSION),
        )}
    missing = sorted(symbols - existing)
    if not missing:
        return {"inserted": 0, "eligible": 0, "reason": "already_recorded", "signal_date": session.isoformat()}
    with external_data_scope(False):
        benchmark, _ = get_daily_history("SPY", period="2y", allow_yfinance_fallback=False)
        if benchmark is None or benchmark.empty or pd.Timestamp(benchmark.index[-1]).date() != session:
            return {"inserted": 0, "reason": "spy_cache_not_current", "signal_date": session.isoformat()}
        rows = []
        for symbol in missing:
            try:
                rows.append(_screen_one(symbol, session, benchmark.tail(280)))
            except Exception:
                continue
    return record_snapshot({"generated_at": recorded.isoformat(), "data_as_of": session.isoformat(),
                            "rows": rows}, source="scheduled_cache", now=recorded)


def _clean_history(frame: pd.DataFrame) -> pd.DataFrame:
    cols = ("Open", "High", "Low", "Close", "Volume")
    if frame is None or frame.empty or not set(cols).issubset(frame.columns):
        return pd.DataFrame()
    data = frame[list(cols)].apply(pd.to_numeric, errors="coerce").sort_index()
    data.index = pd.to_datetime(data.index).normalize()
    return data.loc[~data.index.duplicated(keep="last")]


def resolve_pending(*, now: datetime | None = None, limit: int = 10000) -> dict[str, Any]:
    """Settle only matured cached bars. Missing data stays pending, never guessed."""
    recorded = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    latest_session = most_recent_session(recorded)
    with connection() as conn:
        pending = [dict(row) for row in conn.execute(
            """SELECT prediction_id, signal_date, symbol, direction, horizon_days
               FROM long_option_direction_predictions WHERE resolution_state='pending'
               ORDER BY signal_date, symbol LIMIT ?""", (max(1, int(limit)),))]
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for row in pending:
        by_symbol[row["symbol"]].append(row)
    resolved = invalid = 0
    with external_data_scope(False):
        spy, _ = get_daily_history("SPY", period="2y", allow_yfinance_fallback=False)
        spy = _clean_history(spy)
        for symbol, rows in by_symbol.items():
            frame, _ = get_daily_history(symbol, period="2y", allow_yfinance_fallback=False)
            frame = _clean_history(frame)
            if frame.empty or spy.empty:
                continue
            for row in rows:
                stamp = pd.Timestamp(row["signal_date"])
                if stamp not in frame.index:
                    continue
                pos = int(frame.index.get_loc(stamp))
                exit_pos = pos + int(row["horizon_days"])
                if exit_pos >= len(frame) or frame.index[exit_pos].date() > latest_session:
                    continue
                entry_date, exit_date = frame.index[pos + 1], frame.index[exit_pos]
                if entry_date not in spy.index or exit_date not in spy.index:
                    continue
                path = frame.iloc[max(0, pos - 200):exit_pos + 1]
                prices = (float(frame["Open"].iloc[pos + 1]), float(frame["Close"].iloc[exit_pos]),
                          float(spy.at[entry_date, "Open"]), float(spy.at[exit_date, "Close"]))
                valid = (all(math.isfinite(v) and v > 0 for v in prices)
                         and path[list(("Open", "High", "Low", "Close", "Volume"))].notna().all().all()
                         and path["Volume"].gt(0).all()
                         and path["High"].ge(path[["Open", "Close", "Low"]].max(axis=1)).all()
                         and path["Low"].le(path[["Open", "Close", "High"]].min(axis=1)).all()
                         and path["Close"].pct_change().abs().fillna(0).lt(0.50).all())
                if not valid:
                    with connection() as conn:
                        conn.execute(
                            """UPDATE long_option_direction_predictions
                               SET resolution_state='invalid_data', resolved_at=?, resolution_note=?
                               WHERE prediction_id=? AND resolution_state='pending'""",
                            (recorded.isoformat(), "missing_price_or_possible_split", row["prediction_id"]),
                        )
                    invalid += 1
                    continue
                stock_ret = prices[1] / prices[0] - 1.0
                spy_ret = prices[3] / prices[2] - 1.0
                side = {"C": 1, "P": -1, "WAIT": 0}[row["direction"]]
                cost = equity_round_trip_cost() if side else 0.0
                net = side * stock_ret - cost if side else None
                excess = side * (stock_ret - spy_ret) - cost if side else None
                with connection() as conn:
                    conn.execute(
                        """UPDATE long_option_direction_predictions
                           SET resolution_state='resolved', resolved_at=?, entry_date=?, exit_date=?,
                               entry_open=?, exit_close=?, underlying_return=?, signed_net=?,
                               signed_spy_excess=?
                           WHERE prediction_id=? AND resolution_state='pending'""",
                        (recorded.isoformat(), entry_date.date().isoformat(), exit_date.date().isoformat(),
                         prices[0], prices[1], stock_ret, net, excess, row["prediction_id"]),
                    )
                resolved += 1
    return {"examined": len(pending), "resolved": resolved, "invalid_data": invalid,
            "pending_or_immature": len(pending) - resolved - invalid, "symbols_read": len(by_symbol)}


def _group_stats(rows: list[dict]) -> dict[str, Any]:
    values = [float(row["signed_net"]) for row in rows if row["signed_net"] is not None]
    excess = [float(row["signed_spy_excess"]) for row in rows if row["signed_spy_excess"] is not None]
    return {"n": len(values), "hit_net": mean(v > 0 for v in values) if values else None,
            "mean_signed_net": mean(values) if values else None,
            "mean_signed_spy_excess": mean(excess) if excess else None}


def scorecard(*, horizon: int = PRIMARY_HORIZON) -> dict[str, Any]:
    """Read-only: opening the board never fetches prices or resolves outcomes."""
    if horizon not in HORIZONS:
        raise ValueError("unsupported horizon")
    with connection() as conn:
        rows = [dict(row) for row in conn.execute(
            """SELECT signal_date, direction, mode, resolution_state, signed_net,
                      signed_spy_excess, underlying_return
               FROM long_option_direction_predictions
               WHERE horizon_days=? AND model_version=? ORDER BY signal_date, direction""",
            (horizon, MODEL_VERSION),
        )]
    live = [row for row in rows if row["mode"] == "live"]
    settled = [row for row in live if row["resolution_state"] == "resolved"]
    directional = [row for row in settled if row["direction"] in {"C", "P"}]
    waiting = [row for row in settled if row["direction"] == "WAIT"]
    distinct_dates = len({row["signal_date"] for row in directional})
    cost = equity_round_trip_cost()
    policy_values = [float(row["signed_net"]) if row["direction"] != "WAIT" else 0.0 for row in settled]
    always_call_values = [float(row["underlying_return"]) - cost for row in settled]
    paired_deltas = [policy - baseline for policy, baseline in zip(policy_values, always_call_values)]
    # Repeated symbols on the same date share market risk; no CI on a handful of days.
    ci = None
    if len(directional) >= 100 and distinct_dates >= 40:
        clusters: dict[str, list[float]] = defaultdict(list)
        for row in directional:
            clusters[row["signal_date"]].append(float(row["signed_net"]))
        keys = sorted(clusters)
        rng = random.Random(73)
        draws = [mean(value for day in (rng.choice(keys) for _ in keys) for value in clusters[day])
                 for _ in range(1000)]
        draws.sort()
        ci = [draws[24], draws[974]]
    return {
        "mode": "live_only", "model_version": MODEL_VERSION, "horizon_days": horizon,
        "recorded": len(live), "late_replay_excluded": len(rows) - len(live),
        "pending": sum(row["resolution_state"] == "pending" for row in live),
        "invalid_data": sum(row["resolution_state"] == "invalid_data" for row in live),
        "resolved": len(settled), "independent_signal_dates": len({row["signal_date"] for row in settled}),
        "recorded_signal_dates": len({row["signal_date"] for row in live}),
        "latest_signal_date": max((row["signal_date"] for row in live), default=None),
        "directional": _group_stats(directional),
        "coverage": len(directional) / len(settled) if settled else None,
        "matched_always_call": {
            "n": len(settled),
            "policy_mean_per_candidate": mean(policy_values) if policy_values else None,
            "always_call_mean_per_candidate": mean(always_call_values) if always_call_values else None,
            "delta_policy_minus_always_call": mean(paired_deltas) if paired_deltas else None,
        },
        "call": _group_stats([row for row in directional if row["direction"] == "C"]),
        "put": _group_stats([row for row in directional if row["direction"] == "P"]),
        "wait": {"n": len(waiting),
                 "mean_abs_underlying_move": mean(abs(float(row["underlying_return"])) for row in waiting)
                 if waiting else None},
        "ci95_mean_signed_net_by_date": ci,
        "evidence_status": "insufficient_live_sample" if ci is None else "descriptive_only",
        "note": "仅正股方向代理，非期权盈利概率；期权历史报价/IV缺失。旧回放不混入前向样本。",
    }
