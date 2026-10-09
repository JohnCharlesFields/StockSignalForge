"""Forward-verification loop (the closed decision chain, not a backtest slice).

Each day the board's recommendations are logged as PREDICTIONS *before* the
outcome (walk-forward, no look-ahead). At horizon they are resolved against
realized forward returns and scored vs the predicted calibrated win-rate:

  * realized hit-rate vs predicted win-rate (is "56% predicted" actually ~56%?)
  * realized net-of-cost excess vs each stock's own baseline + cluster-bootstrap CI
  * Brier score + skill (proper scoring rule for probability predictions)
  * reliability buckets (calibration-in-the-live-sample)
  * open positions mark-to-market (provisional; not a verdict)

Scientific notes baked in:
  - The unit is the N-day forward excess, so a 1-day check is provisional only.
  - The edge is ~1%/10d -> need hundreds of resolved picks for the live CI to
    tighten; the scorecard reports n + "significant?" honestly and does NOT
    over-react to a few days. Cluster by trading day (same-day picks are
    correlated through the market).
"""

from __future__ import annotations

import math
import random
from datetime import date, datetime, timezone
from statistics import mean
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

import cost_model
import exit_model
from app_database import (
    cache_get, cache_set,
    latest_home_dashboard_snapshot,
    prediction_resolve, predictions_count, predictions_log_many,
    predictions_open, predictions_pending, predictions_resolved,
    priority_candidate_recent, priority_candidate_slices_for_date,
    priority_candidate_slices_log,
    signal_calibration_list,
)

_SCORECARD_CACHE_KEY = "prediction_scorecard:v3"


def _bust_scorecard_cache() -> None:
    """Expire every scorecard cache combo (scope x mode) after a write."""
    for scope in ("board", "all"):
        for mode in ("live", "backfill", "any"):
            for version in ("legacy", "recorded_open_v3"):
                cache_set(f"{_SCORECARD_CACHE_KEY}:{scope}:{mode}:{version}", {}, ttl_seconds=1)


def _finite(v: Any, d: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else d
    except (TypeError, ValueError):
        return d


def _repair_text(value: Any) -> Any:
    """Best-effort repair for UTF-8 text that was decoded as latin-1/cp1252."""
    if isinstance(value, str):
        try:
            repaired = value.encode("latin1").decode("utf-8")
            # Only use the repair when it plausibly produced CJK text.
            if any("\u4e00" <= ch <= "\u9fff" for ch in repaired):
                return repaired
        except Exception:
            pass
        return value
    if isinstance(value, list):
        return [_repair_text(x) for x in value]
    if isinstance(value, dict):
        return {k: _repair_text(v) for k, v in value.items()}
    return value


def _playbook_prediction_fields(pick: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten soft playbook labels into the forward-verification ledger."""
    e = pick.get("playbook_enhancements") or {}
    labels = e.get("labels") if isinstance(e.get("labels"), list) else []
    tag_ids = []
    for label in labels:
        if isinstance(label, dict) and label.get("id"):
            tag_ids.append(str(label.get("id")))
    for key in ("market_liquid_rs_top40", "stock_stronger_than_industry"):
        if e.get(key) and key not in tag_ids:
            tag_ids.append(key)
    return {
        "market_liquid_rs_top40": bool(e.get("market_liquid_rs_top40")),
        "stock_stronger_than_industry": bool(e.get("stock_stronger_than_industry")),
        "playbook_score": e.get("playbook_score"),
        "market_rs_score": e.get("market_rs_score"),
        "playbook_tags": tag_ids,
    }


def _calibration_audit() -> Dict[str, Any]:
    """Expose active curve provenance so scorecard users can spot weak data lineage."""
    try:
        curves = [
            c for c in signal_calibration_list(include_curve=True)
            if c.get("signal_type") == "pullback_hv" and c.get("is_active")
        ]
    except Exception as exc:
        return {"available": False, "error": str(exc)}

    items = []
    warnings = []
    for c in curves:
        curve = c.get("curve") or {}
        data_quality = curve.get("data_quality") or {}
        provenance = curve.get("provenance") or {}
        source = c.get("source")
        missing = []
        for key in ("price_source", "option_source", "survivorship_controlled", "point_in_time_universe"):
            if provenance.get(key) is None and data_quality.get(key) is None:
                missing.append(key)
        if source in {None, "", "replay"}:
            warnings.append(f"pullback_hv/{c.get('horizon_days')} active curve source is '{source or 'unknown'}'")
        if missing:
            warnings.append(f"pullback_hv/{c.get('horizon_days')} missing provenance: {', '.join(missing)}")
        items.append({
            "signal_type": c.get("signal_type"),
            "horizon_days": c.get("horizon_days"),
            "source": source,
            "event_count": c.get("event_count"),
            "cluster_count": c.get("cluster_count"),
            "validated": bool(curve.get("validation", {}).get("validated")),
            "spread": curve.get("validation", {}).get("spread"),
            "price_source": provenance.get("price_source") or data_quality.get("price_source"),
            "option_source": provenance.get("option_source") or data_quality.get("option_source"),
            "survivorship_controlled": provenance.get("survivorship_controlled") or data_quality.get("survivorship_controlled"),
            "point_in_time_universe": provenance.get("point_in_time_universe") or data_quality.get("point_in_time_universe"),
        })
    return {
        "available": bool(items),
        "items": items,
        "warnings": warnings,
        "note": "校准曲线来源审计：source/provenance 不完整时，胜率只能视为研究概率，不能当作已验证实盘胜率。",
    }


def _fallback_board_from_latest_snapshot(limit: int = 1000) -> Dict[str, Any]:
    """Build a non-blocking candidate slice from the latest dashboard snapshot.

    This is intentionally lightweight: no market-data downloads and no external
    calls. It keeps the forward ledger alive even when the richer priority-board
    cache is cold.
    """
    snapshot = latest_home_dashboard_snapshot() or {}
    rows = (((snapshot.get("payload") or {}).get("rows")) or [])
    picks: List[Dict[str, Any]] = []
    for i, row in enumerate(rows[: max(1, int(limit))]):
        sym = str(row.get("symbol") or row.get("ticker") or "").upper()
        if not sym:
            continue
        probability = row.get("unified_probability") or row.get("research_probability") or row.get("probability")
        score = row.get("overall_score") or row.get("sort_score") or row.get("opportunity_score") or 0
        price = row.get("current_price") or row.get("spot") or row.get("last_price")
        picks.append({
            "symbol": sym,
            "current_price": _finite(price),
            "priority_score": _finite(score),
            "relative_strength_20d": _finite(row.get("relative_strength_20d")),
            "calibrated_probability": _finite(probability, 0.5),
            "reason": _repair_text(row.get("short_reason") or row.get("decision") or row.get("reason") or "来自最近三维信号快照，未触发实时行情重算。"),
            "track": _repair_text(row.get("track") or row.get("source_pools")),
            "track_cn": _repair_text(row.get("track_cn")),
            "source_universe_ids": row.get("source_universe_ids") or [],
            "source_universe_labels": row.get("source_universe_labels") or [],
            "liquidity": row.get("liquidity"),
            "hv": row.get("hv"),
            "detail_url": f"/single-stock-overnight?symbol={sym}",
            "candidate_rank": i + 1,
        })
    picks.sort(key=lambda item: (_finite(item.get("calibrated_probability"), 0.5), _finite(item.get("priority_score"))), reverse=True)
    for i, item in enumerate(picks):
        item["candidate_rank"] = i + 1
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_id": snapshot.get("snapshot_id"),
        "data_as_of": (snapshot.get("payload") or {}).get("data_as_of"),
        "horizon_days": 10,
        "signal_basis": "pullback_hv_snapshot",
        "picks": picks,
        "source": "latest_home_dashboard_snapshot",
    }


def log_predictions(
    top_n: int = 30,
    *,
    log_all_candidates: bool = True,
    max_candidates: int = 1000,
    allow_recompute: bool = False,
    as_of_date: str | None = None,
) -> Dict[str, Any]:
    """Log today's board candidates before outcomes are known.

    ``top_n`` now means "mark these as board picks"; it is no longer the data
    boundary.  By default every scored candidate is written to the daily slice
    table and the forward ledger so later we can test rank buckets, not just the
    top recommendations.
    """
    from priority_board_service import CACHE_KEY as PRIORITY_BOARD_CACHE_KEY, compute_priority_board

    request_limit = max(int(top_n or 0), int(max_candidates or 0)) if log_all_candidates else int(top_n or 30)
    cached_board = cache_get(PRIORITY_BOARD_CACHE_KEY)
    if isinstance(cached_board, dict) and cached_board.get("picks"):
        board = {**cached_board, "picks": (cached_board.get("picks") or [])[:request_limit], "source": "priority_board_cache"}
    elif allow_recompute:
        board = compute_priority_board(limit=request_limit)
        board["source"] = "priority_board_recompute"
    else:
        board = _fallback_board_from_latest_snapshot(request_limit)
    picks = board.get("picks") or []
    horizon = int(board.get("horizon_days") or 10)
    prov = board.get("calibration_provenance") or {}
    curve_source = prov.get("source") or board.get("signal_basis") or "pullback_hv"
    if as_of_date is None:
        from market_calendar import most_recent_session
        as_of_date = most_recent_session().isoformat()
    as_of = str(as_of_date)
    from market_calendar import is_trading_day, most_recent_session
    if not is_trading_day(date.fromisoformat(as_of)) or as_of != most_recent_session().isoformat():
        raise ValueError("forecast date must be the latest completed US session")
    if board.get("data_as_of") != as_of:
        raise ValueError("board price date must match forecast date")
    rows = []
    skipped_dates = 0
    for i, p in enumerate(picks):
        if p.get("price_as_of") != as_of:
            skipped_dates += 1
            continue
        rank = i + 1
        rows.append({
            "as_of_date": as_of, "symbol": p.get("symbol"), "rank": rank,
            "signal_type": "pullback_hv", "calibrated_prob": p.get("calibrated_probability"),
            "entry_ref_price": p.get("current_price"), "horizon_days": horizon,
            "curve_source": curve_source, "is_board_pick": rank <= int(top_n or 0),
            "evaluation_version": "recorded_open_v3",
            "forecast": {"snapshot_id": board.get("snapshot_id"), "price_as_of": p.get("price_as_of") or board.get("data_as_of"),
                         "model_version": board.get("parameter_version", "legacy_ranking"), "features": p,
                         "probability_target": "legacy_close_to_open_own_baseline"},
            **_playbook_prediction_fields(p),
        })
        p["candidate_rank"] = rank
        p["is_board_pick"] = rank <= int(top_n or 0)
    written = predictions_log_many(rows)
    slice_written = priority_candidate_slices_log(
        as_of_date=as_of,
        rows=[p for p in picks if p.get("price_as_of") == as_of],
        horizon_days=horizon,
        signal_type="pullback_hv",
        board_top_n=top_n,
    )
    _bust_scorecard_cache()
    return {
        "as_of_date": as_of,
        "logged_new": written,
        "slice_rows_written": slice_written,
        "candidates": len(rows),
        "skipped_unverified_price_dates": skipped_dates,
        "board_top_n": int(top_n or 0),
        "horizon_days": horizon,
        "scope": "all_candidates" if log_all_candidates else "board_top_n",
        "source": board.get("source") or "priority_board",
    }


def _spy_ret_asof(spy_close: "pd.Series", as_of_ts: "pd.Timestamp", window: int = 20) -> float:
    sub = spy_close[spy_close.index <= as_of_ts]
    if len(sub) < window + 1:
        return 0.0
    return float(sub.iloc[-1] / sub.iloc[-(window + 1)] - 1.0)


def backfill_predictions(
    lookback_trading_days: int = 30,
    *,
    top_n: int = 30,
    max_symbols: int = 200,
    min_bars: int = 60,
) -> Dict[str, Any]:
    """Seed the ledger with an as-of-historical *walk-forward* replay.

    For each of the last N US trading days we reconstruct what the board WOULD
    have scored using ONLY data up to that day -- a frame truncated to ``<= D``
    fed through the SAME validated read (`single_stock_signal_read`) the live
    board uses (same `_score_frame` / `_pullback_hv_score` / `calibrate`). Each
    cohort is logged with ``mode='backfill'``; `resolve_predictions` then scores
    the matured ones.

    Honesty (critical): the active calibration curve's window overlaps this
    period, so backfilled predictions are IN-SAMPLE -- they test calibration
    quality + rank-bucket monotonicity on realized prices, NOT a clean
    out-of-sample forward edge. They are tagged 'backfill' and NEVER mixed into
    the live forward scorecard. The genuine forward record still accrues from
    today via the daily batch.
    """
    from market_calendar import trading_days_back
    from market_data_service import get_daily_history
    from priority_board_service import single_stock_signal_read, _select_signals, PULLBACK_HV_HORIZON

    sel = _select_signals()
    horizon = int(sel.get("horizon", PULLBACK_HV_HORIZON))
    curve_source = sel.get("source") or sel.get("mode") or "pullback_hv"

    # Candidate universe = the symbols the board currently scans (latest snapshot).
    snapshot = latest_home_dashboard_snapshot() or {}
    snap_rows = (((snapshot.get("payload") or {}).get("rows")) or [])
    symbols, seen = [], set()
    for row in snap_rows:
        sym = str(row.get("symbol") or row.get("ticker") or "").upper()
        if sym and sym not in seen:
            seen.add(sym)
            symbols.append(sym)
        if len(symbols) >= max_symbols:
            break

    dates = trading_days_back(lookback_trading_days)
    date_ts = [pd.Timestamp(d) for d in dates]

    # Pre-fetch one frame per symbol (cache-first; a manual trigger may fetch).
    frames: Dict[str, pd.Series] = {}
    spy_frame, _ = get_daily_history("SPY", period="1y")
    spy_close = pd.to_numeric(spy_frame["Close"], errors="coerce").dropna() if spy_frame is not None and not spy_frame.empty else pd.Series(dtype=float)

    cohorts: Dict[str, List[Dict[str, Any]]] = {d.isoformat(): [] for d in dates}
    scored_calls = 0
    for sym in symbols:
        try:
            frame, _src = get_daily_history(sym, period="1y")
        except Exception:
            continue
        if frame is None or frame.empty or "Close" not in frame:
            continue
        close = pd.to_numeric(frame["Close"], errors="coerce").dropna()
        if close.empty:
            continue
        for d, dts in zip(dates, date_ts):
            sub = frame[frame.index <= dts]
            sub_close = close[close.index <= dts]
            if len(sub_close) < min_bars:
                continue
            spy_ret = _spy_ret_asof(spy_close, dts) if len(spy_close) else 0.0
            read = single_stock_signal_read(sym, frame=sub, spy_ret=spy_ret, selected=sel)
            scored_calls += 1
            if not read.get("available"):
                continue
            prob = read.get("calibrated_win_rate")
            if prob is None:
                continue
            cohorts[d.isoformat()].append({
                "symbol": sym,
                "calibrated_prob": float(prob),
                "entry_ref_price": float(sub_close.iloc[-1]),
            })

    # Rank each cohort by calibrated win-rate (the board's primary sort key),
    # tag the top_n as board picks, and log with mode='backfill'.
    logged = 0
    days_with_rows = 0
    for d in dates:
        items = cohorts[d.isoformat()]
        if not items:
            continue
        days_with_rows += 1
        items.sort(key=lambda x: x["calibrated_prob"], reverse=True)
        rows = []
        for i, it in enumerate(items):
            rank = i + 1
            rows.append({
                "as_of_date": d.isoformat(), "symbol": it["symbol"], "rank": rank,
                "signal_type": "pullback_hv", "calibrated_prob": it["calibrated_prob"],
                "entry_ref_price": it["entry_ref_price"], "horizon_days": horizon,
                "curve_source": curve_source, "is_board_pick": rank <= int(top_n or 0),
                "mode": "backfill",
            })
        logged += predictions_log_many(rows)

    _bust_scorecard_cache()
    return {
        "lookback_trading_days": int(lookback_trading_days),
        "dates": [d.isoformat() for d in dates],
        "symbols_scanned": len(symbols),
        "scored_calls": scored_calls,
        "days_with_rows": days_with_rows,
        "logged_new": logged,
        "horizon_days": horizon,
        "mode": "backfill",
        "note": "样本内 as-of 回放（校准曲线见过这段），用于核对校准质量与名次单调性；非干净样本外前向。",
    }


def priority_monitor(limit: int = 100, offset: int = 0, days: int = 30) -> Dict[str, Any]:
    """Latest full candidate slice plus multi-day lifecycle counters."""
    latest = priority_candidate_slices_for_date(limit=max(1, int(limit)), offset=max(0, int(offset)))
    recent = priority_candidate_recent(days=days)
    by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    for row in recent:
        by_symbol.setdefault(str(row.get("symbol") or "").upper(), []).append(row)

    rows = []
    for row in latest:
        sym = str(row.get("symbol") or "").upper()
        hist = sorted(by_symbol.get(sym, []), key=lambda x: str(x.get("as_of_date") or ""))
        ranks = [int(h.get("candidate_rank") or 999999) for h in hist]
        first_seen = hist[0].get("as_of_date") if hist else row.get("as_of_date")
        prev = hist[-2] if len(hist) >= 2 else None
        rank = int(row.get("candidate_rank") or 999999)
        prev_rank = int(prev.get("candidate_rank") or rank) if prev else None
        rank_delta = (prev_rank - rank) if prev_rank is not None else None
        payload = _repair_text(row.get("payload") or {})
        price = _finite(row.get("current_price"), _finite(payload.get("current_price")))
        invalidation = _finite(payload.get("invalidation_price"), 0.0)
        status = "NEW" if len(hist) <= 1 else "WATCHING"
        if rank_delta is not None and rank_delta >= 5:
            status = "IMPROVING"
        elif rank_delta is not None and rank_delta <= -5:
            status = "WEAKENING"
        if invalidation > 0 and price > 0 and price <= invalidation:
            status = "INVALIDATED"
        rows.append({
            "symbol": sym,
            "as_of_date": row.get("as_of_date"),
            "candidate_rank": rank,
            "is_board_pick": bool(row.get("is_board_pick")),
            "status": status,
            "first_seen": first_seen,
            "days_seen": len({h.get("as_of_date") for h in hist if h.get("as_of_date")}),
            "best_rank": min(ranks) if ranks else rank,
            "rank_delta": rank_delta,
            "current_price": price,
            "calibrated_probability": row.get("calibrated_prob"),
            "priority_score": row.get("priority_score"),
            "relative_strength_20d": row.get("relative_strength_20d"),
            "waiting_breakout_price": payload.get("waiting_breakout_price"),
            "invalidation_price": payload.get("invalidation_price"),
            "reason": _repair_text(payload.get("reason")),
            "track": _repair_text(payload.get("track")),
            "track_cn": _repair_text(payload.get("track_cn")),
            "source_universe_ids": _repair_text(payload.get("source_universe_ids") or []),
            "source_universe_labels": _repair_text(payload.get("source_universe_labels") or []),
            "liquidity": payload.get("liquidity"),
            "hv": payload.get("hv"),
            "detail_url": payload.get("detail_url"),
        })
    latest_date = latest[0].get("as_of_date") if latest else None
    return {
        "available": bool(latest),
        "as_of_date": latest_date,
        "limit": int(limit),
        "offset": int(offset),
        "lookback_days": int(days),
        "rows": rows,
        "summary": {
            "shown": len(rows),
            "tracked_symbols": len(by_symbol),
            "board_picks_shown": sum(1 for r in rows if r.get("is_board_pick")),
            "improving": sum(1 for r in rows if r.get("status") == "IMPROVING"),
            "watching": sum(1 for r in rows if r.get("status") == "WATCHING"),
            "new": sum(1 for r in rows if r.get("status") == "NEW"),
            "invalidated": sum(1 for r in rows if r.get("status") == "INVALIDATED"),
        },
    }


def _baseline_forward(close: pd.Series, horizon: int, open_: pd.Series | None = None) -> float:
    """Unconditional mean forward return, entry close[T] -> exit open[T+H] (exit_model)."""
    vals = exit_model.baseline_forward_returns(close, open_, horizon)
    return mean(vals) if vals else 0.0


def _beta_to_market(stock_close: pd.Series, spy_close: pd.Series, as_of_ts: pd.Timestamp, window: int = 60) -> float:
    """OLS beta of the stock vs SPY over the ~`window` trading days BEFORE entry
    (no look-ahead). Clamped to [0, 3]. Defaults to 1.0 on thin data."""
    try:
        s = stock_close[stock_close.index < as_of_ts].tail(window + 1).pct_change().dropna()
        m = spy_close[spy_close.index < as_of_ts].tail(window + 1).pct_change().dropna()
        df = pd.concat([s, m], axis=1, join="inner").dropna()
        if len(df) < 20:
            return 1.0
        sr, mr = df.iloc[:, 0], df.iloc[:, 1]
        var = float(mr.var(ddof=0))
        if var <= 0:
            return 1.0
        beta = float(((sr - sr.mean()) * (mr - mr.mean())).mean() / var)
        return max(0.0, min(3.0, beta))
    except Exception:
        return 1.0


def resolve_predictions(limit: int = 4000) -> Dict[str, Any]:
    """Resolve predictions whose horizon has fully elapsed; score win/excess +
    beta-adjusted alpha (forward return minus beta x SPY's forward, net of cost --
    strips out the market move so we see the genuine stock-specific edge)."""
    from market_data_service import get_daily_history

    pending = predictions_pending(limit=limit)
    today = date.today()
    rt_cost = cost_model.equity_round_trip_cost()
    resolved = immature = skipped = 0
    spy_frame = pd.DataFrame()
    try:
        spy_frame, _spy_src = get_daily_history("SPY", period="2y")
        spy_close = pd.to_numeric(spy_frame["Close"], errors="coerce").dropna()
        spy_open = exit_model.aligned_open(spy_frame, spy_close)
    except Exception:
        spy_close = pd.Series(dtype=float)
        spy_open = None

    # Group matured pending rows BY SYMBOL so each symbol's 2y frame is fetched
    # exactly once (backfill creates many rows per symbol across trading days;
    # the old per-row fetch re-downloaded the same series dozens of times). One
    # fetch per symbol roughly halves network on live too.
    by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    for p in pending:
        try:
            as_of = date.fromisoformat(str(p["as_of_date"])[:10])
        except (TypeError, ValueError):
            skipped += 1
            continue
        horizon = int(p["horizon_days"])
        if int(np.busday_count(as_of, today)) < horizon + 1:  # not enough bars elapsed
            immature += 1
            continue
        by_symbol.setdefault(str(p["symbol"]).upper(), []).append({**p, "_as_of": as_of, "_horizon": horizon})

    for symbol, rows in by_symbol.items():
        try:
            frame, _src = get_daily_history(symbol, period="2y")
            close = pd.to_numeric(frame["Close"], errors="coerce").dropna()
        except Exception:
            skipped += len(rows)
            continue
        if close.empty:
            skipped += len(rows)
            continue
        open_ = exit_model.aligned_open(frame, close)
        index = close.index
        # Cache each (as_of, horizon) baseline once per symbol.
        baseline_cache: Dict[Any, float] = {}
        for p in rows:
            horizon = p["_horizon"]
            if len(close) <= horizon:
                skipped += 1
                continue
            as_of_ts = pd.Timestamp(p["_as_of"])
            if p.get("evaluation_version") == "recorded_open_v3":
                from pullback_validation import resolve_recorded_forecast
                outcome = resolve_recorded_forecast(p, frame, spy_frame)
                if outcome is None:
                    immature += 1
                    continue
                prediction_resolve(p["prediction_id"], **outcome)
                resolved += 1
                continue
            entry_pos = [pos for pos, ts in enumerate(index) if ts >= as_of_ts]
            if not entry_pos:
                skipped += 1
                continue
            ei = entry_pos[0]
            xi = ei + horizon
            if xi >= len(close):
                immature += 1
                continue
            entry = float(close.iloc[ei])
            # Entry = close[T] (盘末买入), exit = open[T+H] (盘初卖出) per exit_model.
            fwd = exit_model.forward_return(close, open_, ei, horizon)
            if entry <= 0 or fwd is None:
                skipped += 1
                continue
            exit_ = entry * (1.0 + fwd)  # realized exit (open[T+H] in default mode)
            baseline_key = (as_of_ts, horizon)
            if baseline_key not in baseline_cache:
                historical = close.loc[:as_of_ts]
                baseline_cache[baseline_key] = _baseline_forward(historical, open_.reindex(historical.index) if open_ is not None else None, horizon)
            baseline = baseline_cache[baseline_key]
            excess = fwd - baseline
            net_excess = excess - rt_cost
            # Beta-adjusted alpha: forward return minus beta x SPY forward (net of
            # cost). Strips the market move so we see the stock-specific edge.
            beta_alpha = None
            if len(spy_close):
                sei = int(spy_close.index.searchsorted(as_of_ts))
                spy_fwd = exit_model.forward_return(spy_close, spy_open, sei, horizon)
                if spy_fwd is not None:
                    beta = _beta_to_market(close, spy_close, as_of_ts)
                    beta_alpha = (fwd - beta * spy_fwd) - rt_cost
            try:
                prediction_resolve(
                    p["prediction_id"], exit_price=exit_, forward_return=fwd, baseline_return=baseline,
                    excess_return=excess, net_excess=net_excess, win=1 if net_excess > 0 else 0,
                    beta_adjusted_alpha=beta_alpha,
                )
                resolved += 1
            except Exception:
                skipped += 1
    if resolved:
        _bust_scorecard_cache()
    return {"examined": len(pending), "resolved": resolved, "immature": immature,
            "skipped": skipped, "symbols_fetched": len(by_symbol)}


def _cluster_ci(by_cluster: Dict[str, List[float]], n_boot: int = 2000, seed: int = 42) -> tuple:
    clusters = [c for c in by_cluster if by_cluster[c]]
    if len(clusters) < 2:
        return (None, None)
    rng = random.Random(seed)
    ests = []
    for _ in range(n_boot):
        samp = [rng.choice(clusters) for _ in clusters]
        vals = [v for c in samp for v in by_cluster[c]]
        if vals:
            ests.append(mean(vals))
    if not ests:
        return (None, None)
    ests.sort()
    return (round(ests[int(0.025 * (len(ests) - 1))], 6), round(ests[int(0.975 * (len(ests) - 1))], 6))


def _bucket_stats(label: str, rows: List[Dict[str, Any]]) -> Dict[str, Any] | None:
    if not rows:
        return None
    by_day: Dict[str, List[float]] = {}
    by_day_alpha: Dict[str, List[float]] = {}
    alphas = []
    for r in rows:
        by_day.setdefault(str(r.get("as_of_date")), []).append(_finite(r.get("net_excess")))
        if r.get("beta_adjusted_alpha") is not None:
            alpha = _finite(r.get("beta_adjusted_alpha"))
            by_day_alpha.setdefault(str(r.get("as_of_date")), []).append(alpha)
            alphas.append(alpha)
    ci = _cluster_ci(by_day)
    alpha_ci = _cluster_ci(by_day_alpha) if alphas else (None, None)
    return {
        "label": label,
        "n": len(rows),
        "predicted_win_rate": round(mean([_finite(r.get("calibrated_prob")) for r in rows]), 4),
        "realized_win_rate": round(mean([int(r.get("win") or 0) for r in rows]), 4),
        "mean_net_excess": round(mean([_finite(r.get("net_excess")) for r in rows]), 6),
        "net_excess_ci": [ci[0], ci[1]],
        "mean_beta_alpha": round(mean(alphas), 6) if alphas else None,
        "beta_alpha_ci": [alpha_ci[0], alpha_ci[1]] if alphas else [None, None],
        "significant": bool(ci[0] is not None and ci[0] > 0),
    }


def prediction_scorecard(force: bool = False, board_only: bool = False,
                         mode: Optional[str] = "live", evaluation_version: str = "legacy") -> Dict[str, Any]:
    """Live forward track record: predicted vs realized + proper scoring.

    ``mode`` selects which predictions count: 'live' (real walk-forward, the
    honest forward record), 'backfill' (in-sample as-of replay used to seed the
    ledger), or None (both). Live and backfill are kept apart so the seeded
    in-sample replay never inflates the live forward stats.
    """
    cache_key = f"{_SCORECARD_CACHE_KEY}:{'board' if board_only else 'all'}:{mode or 'any'}:{evaluation_version}"
    if not force:
        cached = cache_get(cache_key)
        if isinstance(cached, dict) and cached.get("available") is not None:
            return {**cached, "cache_hit": True}

    counts = predictions_count(mode=mode, evaluation_version=evaluation_version)
    resolved = predictions_resolved(board_only=board_only, mode=mode)
    resolved = [r for r in resolved if r.get("evaluation_version", "legacy") == evaluation_version]
    result: Dict[str, Any] = {
        "available": bool(resolved), "counts": counts, "cache_hit": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode or "any",
        "evaluation_version": evaluation_version,
        "evaluation_note": "历史结算口径待审计" if evaluation_version == "legacy" else "记录后下一可交易开盘入场；事前基线。旧校准概率仅供参考，与新收益目标尚未校准。",
        "scope": "board_top_n" if board_only else "all_candidates",
        "calibration_audit": _calibration_audit(),
        "method_note": (
            "实盘前向对账：每天看板推荐在出结果前落账(无 look-ahead)，到期(N交易日)才结算。"
            "胜=扣成本后跑赢该股自身基线。单位是 N 日超额，1 天涨跌只是临时盯市不作数。"
            "历史口径与记录后开盘口径分组统计；首次预测已冻结。重叠持有需要更多交易日验证，不能按记录笔数估算独立样本数。"
        ),
    }

    if resolved:
        from pullback_validation import block_interval
        n = len(resolved)
        preds = [_finite(r["calibrated_prob"]) for r in resolved]
        wins = [int(r["win"]) for r in resolved]
        nets = [_finite(r["net_excess"]) for r in resolved]
        realized_hit = mean(wins)
        predicted_mean = mean(preds)
        # cluster by trading day (same-day picks correlated through the market)
        by_day_excess: Dict[str, List[float]] = {}
        for r in resolved:
            by_day_excess.setdefault(str(r["as_of_date"]), []).append(_finite(r["net_excess"]))
        ci_lo, ci_hi = _cluster_ci(by_day_excess)
        # Beta-adjusted alpha dimension: market-neutral edge (forward − beta×SPY
        # forward, net of cost). CI excluding 0 = a genuine stock-specific edge
        # after stripping the market move.
        betas = [_finite(r["beta_adjusted_alpha"]) for r in resolved if r.get("beta_adjusted_alpha") is not None]
        if betas:
            by_day_alpha: Dict[str, List[float]] = {}
            for r in resolved:
                if r.get("beta_adjusted_alpha") is not None:
                    by_day_alpha.setdefault(str(r["as_of_date"]), []).append(_finite(r["beta_adjusted_alpha"]))
            ba_lo, ba_hi = _cluster_ci(by_day_alpha)
            result["beta_alpha"] = {
                "n": len(betas),
                "mean_net_alpha": round(mean(betas), 6),
                "win_rate": round(mean([1 if b > 0 else 0 for b in betas]), 4),
                "ci": [ba_lo, ba_hi],
                "significant": bool(ba_lo is not None and ba_lo > 0),
                "evidence_direction": "negative" if ba_hi is not None and ba_hi < 0 else "positive" if ba_lo is not None and ba_lo > 0 else "inconclusive",
                "note": "扣成本后的SPY beta调整收益；仍可能包含行业、风格风险。单日聚类区间未完全处理跨日重叠，需分块验证。",
            }
        brier = mean([(p - w) ** 2 for p, w in zip(preds, wins)])
        base = realized_hit
        brier_ref = base * (1 - base) if 0 < base < 1 else 0.25
        skill = 1 - brier / brier_ref if brier_ref > 0 else None
        # reliability buckets on the predicted probability
        edges = [0.0, 0.50, 0.53, 0.56, 1.01]
        labels = ["<50%", "50-53%", "53-56%", ">56%"]
        buckets = []
        for b in range(len(edges) - 1):
            grp = [(p, w) for p, w in zip(preds, wins) if edges[b] <= p < edges[b + 1]]
            if grp:
                buckets.append({
                    "label": labels[b], "n": len(grp),
                    "predicted": round(mean([g[0] for g in grp]), 4),
                    "realized": round(mean([g[1] for g in grp]), 4),
                })
        # Rank-bucket monotonicity: do better-ranked candidates actually win more?
        # This only means something because we now log ALL candidates, not just top-N.
        rank_edges = [(1, 3), (4, 10), (11, 30), (31, 10_000)]
        rank_labels = ["Top1-3", "4-10", "11-30", "31+ (尾部)"]
        rank_buckets = []
        for (lo, hi), lbl in zip(rank_edges, rank_labels):
            grp = [r for r in resolved if lo <= int(_finite(r.get("rank"), 99999)) <= hi]
            if grp:
                rank_buckets.append({
                    "label": lbl, "n": len(grp),
                    "predicted_win_rate": round(mean([_finite(r["calibrated_prob"]) for r in grp]), 4),
                    "realized_win_rate": round(mean([int(r["win"]) for r in grp]), 4),
                    "mean_net_excess": round(mean([_finite(r["net_excess"]) for r in grp]), 6),
                })
        # Monotone if board picks (Top1-3) realize a higher hit-rate than the tail.
        rank_monotone = None
        if len(rank_buckets) >= 2:
            rank_monotone = bool(rank_buckets[0]["realized_win_rate"] >= rank_buckets[-1]["realized_win_rate"])
        enhancement_buckets = []
        bucket_specs = [
            ("市场+流动性+RS前40%", [r for r in resolved if int(_finite(r.get("market_liquid_rs_top40"))) == 1]),
            ("强于行业ETF", [r for r in resolved if int(_finite(r.get("stock_stronger_than_industry"))) == 1]),
            ("两项同时满足", [
                r for r in resolved
                if int(_finite(r.get("market_liquid_rs_top40"))) == 1
                and int(_finite(r.get("stock_stronger_than_industry"))) == 1
            ]),
            ("未命中软增强", [
                r for r in resolved
                if int(_finite(r.get("market_liquid_rs_top40"))) != 1
                and int(_finite(r.get("stock_stronger_than_industry"))) != 1
            ]),
        ]
        for label, grp in bucket_specs:
            stat = _bucket_stats(label, grp)
            if stat:
                enhancement_buckets.append(stat)
        result.update({
            "n_resolved": n, "trading_days": len(by_day_excess),
            "predicted_win_rate": round(predicted_mean, 4),
            "realized_win_rate": round(realized_hit, 4),
            "mean_net_excess": round(mean(nets), 6),
            "net_excess_ci": [ci_lo, ci_hi],
            "overlap_adjusted_ci": block_interval(by_day_excess, max(r["horizon_days"] for r in resolved)),
            "mean_net_return": mean([r["net_return"] if r.get("net_return") is not None else r["forward_return"] - (r["excess_return"] - r["net_excess"]) for r in resolved]),
            "significant": bool(ci_lo is not None and ci_lo > 0),
            "evidence_direction": "negative" if ci_hi is not None and ci_hi < 0 else "positive" if ci_lo is not None and ci_lo > 0 else "inconclusive",
            "evaluation_cohorts": [dict(version=v, **(_bucket_stats(v, [r for r in resolved if r.get("evaluation_version", "legacy") == v]) or {}))
                                   for v in sorted({r.get("evaluation_version", "legacy") for r in resolved})],
            "horizon_cohorts": [dict(horizon_days=h, **(_bucket_stats(str(h), [r for r in resolved if r["horizon_days"] == h]) or {}))
                                for h in sorted({r["horizon_days"] for r in resolved})],
            "brier": round(brier, 4), "brier_skill_score": round(skill, 4) if skill is not None else None,
            "reliability_buckets": buckets,
            "rank_buckets": rank_buckets,
            "rank_monotone": rank_monotone,
            "enhancement_buckets": enhancement_buckets,
            "enhancement_note": (
                "软增强标签只参与前向对账与排序观察，不直接替代 pullback_hv 校准概率；"
                "样本不足或标签为空时，不应据此判定有效。"
            ),
        })
        if evaluation_version == "recorded_open_v3":
            result.update(predicted_win_rate=None, brier=None, brier_skill_score=None, reliability_buckets=[])

    # Provisional mark-to-market on open board picks (monitoring only).
    try:
        from market_data_service import get_daily_history
        open_rows = predictions_open(board_only=board_only, mode=mode, evaluation_version=evaluation_version)
        mtm_vals = []
        for r in open_rows[:80]:
            try:
                # Cache-only: MTM is provisional monitoring, never block on fetches.
                frame, _s = get_daily_history(str(r["symbol"]), period="3mo", allow_yfinance_fallback=False)
                close = pd.to_numeric(frame["Close"], errors="coerce").dropna()
                entry = _finite(r["entry_ref_price"])
                if entry > 0 and len(close):
                    mtm_vals.append(float(close.iloc[-1]) / entry - 1.0)
            except Exception:
                continue
        result["open_positions"] = {
            "count": len(open_rows),
            "avg_return_so_far": round(mean(mtm_vals), 4) if mtm_vals else None,
            "note": "临时盯市（截至今日），未到期，不计入统计结论。",
        }
    except Exception:
        result["open_positions"] = {"count": 0, "avg_return_so_far": None}

    cache_set(cache_key, result, ttl_seconds=3600)
    return result
