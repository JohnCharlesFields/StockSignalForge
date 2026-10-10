"""Cache-only scoring and a separate durable experimental signal lifecycle."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from app_database import _DB_LOCK, connection, cache_get, cache_set
from market_calendar import most_recent_session, is_trading_day
from market_data_service import _read_daily_cache, external_data_scope
from v_swing_model import annotations, clean_bars, config, fit_models, score_day, settle_buy
from v_swing_validation import settle_sell

SNAPSHOT_KEY = "v_swing:snapshot:v1"
AUDIT_KEY = "v_swing:audit:v1"
JOB_KEY = "v_swing:job:v1"


def ensure_schema():
    with _DB_LOCK, connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS v_swing_signals (
                sig_date TEXT NOT NULL, pool TEXT NOT NULL, ticker TEXT NOT NULL, side TEXT NOT NULL,
                p REAL NOT NULL, entry REAL, stop REAL, target REAL, box_lo REAL, box_hi REAL,
                in_box INTEGER, earn_warn INTEGER, status TEXT NOT NULL, outcome TEXT,
                managed_stop REAL, model_version TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(sig_date,ticker,side)
            );
            CREATE INDEX IF NOT EXISTS idx_v_swing_open ON v_swing_signals(status,ticker,side,sig_date);
            CREATE TABLE IF NOT EXISTS v_swing_adapt_state (
                side TEXT PRIMARY KEY, mult REAL NOT NULL, last_week TEXT, audit_json TEXT NOT NULL
            );
        """)
        conn.commit()


def multipliers() -> dict:
    with connection() as conn:
        return {r["side"]: float(r["mult"]) for r in conn.execute("SELECT side,mult FROM v_swing_adapt_state")}


def adapt_weekly(session: str, cfg: dict) -> dict:
    week = datetime.fromisoformat(session).strftime("%G-W%V")
    with _DB_LOCK, connection() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM v_swing_signals WHERE status='closed' AND sig_date<=? ORDER BY sig_date DESC", (session,))]
        for side in ("buy", "sell"):
            old = conn.execute("SELECT * FROM v_swing_adapt_state WHERE side=?", (side,)).fetchone()
            if old and old["last_week"] == week:
                continue
            mult = float(old["mult"]) if old else 1.
            settled = [r for r in rows if r["side"] == side and sum(is_trading_day(d.date()) for d in pd.date_range(pd.Timestamp(r["sig_date"])+pd.Timedelta(days=1), session)) >= cfg["hold_sessions"]]
            recent = settled[:20]
            audit = {"session": session, "eligible_count": len(recent), "before": mult, "reason": "insufficient_mature_live_signals"}
            if len(recent) >= 20:
                outcomes = [json.loads(r["outcome"]) for r in recent]
                if side == "buy":
                    wins = float(np.mean([o["win"] for o in outcomes]))
                    audit["win_rate"] = wins
                    cutoff = (datetime.fromisoformat(session)-timedelta(days=28)).date().isoformat()
                    count = conn.execute("SELECT COUNT(*) FROM v_swing_signals WHERE side='buy' AND sig_date BETWEEN ? AND ?", (cutoff, session)).fetchone()[0]
                    if wins < .4:
                        mult, audit["reason"] = min(mult*1.1, 1.6), "recent_buy_win_below_40pct"
                    elif count < 2 and wins >= .5:
                        mult, audit["reason"] = max(mult*.9, .7), "few_new_buys_with_acceptable_mature_history"
                    else:
                        audit["reason"] = "unchanged"
                else:
                    mean = float(np.mean([o["subsequent_return"] for o in outcomes]))
                    audit["subsequent_return"] = mean
                    if mean > .03:
                        mult, audit["reason"] = min(mult*1.1, 1.6), "sell_reference_too_early"
                    else:
                        audit["reason"] = "unchanged"
            audit["after"] = mult
            conn.execute("INSERT INTO v_swing_adapt_state VALUES (?,?,?,?) ON CONFLICT(side) DO UPDATE SET mult=excluded.mult,last_week=excluded.last_week,audit_json=excluded.audit_json",
                         (side, mult, week, json.dumps(audit)))
        conn.commit()
    return multipliers()


def update_lifecycle(scores: dict, histories: dict, session: str, version: str, supported: bool, cfg: dict) -> dict:
    """No paper fill on the signal bar; replay settlements stay out of this table."""
    now = datetime.now(timezone.utc).isoformat()
    with _DB_LOCK, connection() as conn:
        def record_signal(score, side, status):
            plan = score.get("plan") or {}
            conn.execute("INSERT INTO v_swing_signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                         "ON CONFLICT(sig_date,ticker,side) DO UPDATE SET p=excluded.p,updated_at=excluded.updated_at,"
                         "status=CASE WHEN v_swing_signals.status='open' THEN 'open' ELSE excluded.status END",
                         (session, json.dumps(score.get("source_pool_ids") or ["v_swing_annotations"]), score["symbol"], side, score[f"{side}_p"],
                          score["close"] if side == "sell" else plan.get("entry"), None if side == "sell" else plan.get("stop"),
                          None if side == "sell" else plan.get("target"), plan.get("box_lo"), plan.get("box_hi"),
                          int(bool(plan.get("in_box"))), int(bool(score.get("earn_warn"))), status, None,
                          None if side == "sell" else plan.get("stop"), version, now))

        opened = [dict(r) for r in conn.execute("SELECT * FROM v_swing_signals WHERE status='open' AND sig_date<=?", (session,))]
        for row in opened:
            frame = histories.get(row["ticker"])
            if frame is None or frame.empty:
                continue
            plan = {k: row[k] for k in ("entry", "stop", "target")}
            outcome = settle_buy(frame, row["sig_date"], plan, cfg) if row["side"] == "buy" else settle_sell(frame, row["sig_date"], cfg)
            if outcome["status"] == "settled" or outcome["status"] == "skipped":
                conn.execute("UPDATE v_swing_signals SET status=?,outcome=?,updated_at=? WHERE sig_date=? AND ticker=? AND side=?",
                             ("closed" if outcome["status"] == "settled" else "skipped", json.dumps(outcome), now, row["sig_date"], row["ticker"], row["side"]))
            elif outcome.get("managed_stop") is not None:
                conn.execute("UPDATE v_swing_signals SET managed_stop=?,updated_at=? WHERE sig_date=? AND ticker=? AND side=?",
                             (outcome["managed_stop"], now, row["sig_date"], row["ticker"], row["side"]))
            score = scores.get(row["ticker"], {})
            if outcome["status"] == "pending" and score.get("status") == "scored" and row["side"] == "buy":
                score["frozen_plan"] = {**plan, "managed_stop": outcome.get("managed_stop", row["managed_stop"])}
                score["lifecycle"] = "continuing"
                risk = row["entry"]-row["stop"] if row["side"] == "buy" else 0
                score["r_multiple"] = (score["close"]-row["entry"])/risk if risk > 0 else None
                conn.execute("UPDATE v_swing_signals SET p=?,updated_at=? WHERE sig_date=? AND ticker=? AND side=?",
                             (score[f"{row['side']}_p"], now, row["sig_date"], row["ticker"], row["side"]))
        active = conn.execute("SELECT COUNT(*) FROM v_swing_signals WHERE side='buy' AND status='open'").fetchone()[0]
        for score in sorted(scores.values(), key=lambda r: r.get("buy_p") or 0, reverse=True):
            score["today_buy"] = False
            if score.get("status") != "scored":
                continue
            if score.get("buy") and score.get("sell"):
                score["lifecycle"] = "conflicting_signals"
                if supported:
                    for side in ("buy", "sell"):
                        record_signal(score, side, "conflicting_watch")
                continue
            if not supported:
                score["lifecycle"] = "validation_not_supported"
                continue
            for side in ("buy", "sell"):
                if not score[side]:
                    continue
                cutoff = (datetime.fromisoformat(session)-timedelta(days=cfg["lifecycle_days"])).date().isoformat()
                old = conn.execute("SELECT * FROM v_swing_signals WHERE ticker=? AND side=? AND status='open' AND sig_date BETWEEN ? AND ? ORDER BY sig_date DESC LIMIT 1", (score["symbol"], side, cutoff, session)).fetchone()
                if old:
                    conn.execute("UPDATE v_swing_signals SET p=?,updated_at=? WHERE sig_date=? AND ticker=? AND side=?",
                                 (score[f"{side}_p"], now, old["sig_date"], old["ticker"], side))
                    score["lifecycle"] = "new" if old["sig_date"] == session else "continuing"
                    score["today_buy"] = side == "buy" and old["sig_date"] == session
                    if side == "buy":
                        score["frozen_plan"] = {k: old[k] for k in ("entry", "stop", "target", "managed_stop")}
                    risk = old["entry"]-old["stop"] if side == "buy" else 0
                    score["r_multiple"] = (score["close"]-old["entry"])/risk if risk > 0 else None
                    continue
                if side == "buy" and active >= cfg["max_open_buys"]:
                    score["lifecycle"] = "concentration_watch"
                    record_signal(score, side, "concentration_watch")
                    continue
                record_signal(score, side, "open")
                if side == "buy":
                    saved = conn.execute("SELECT entry,stop,target,managed_stop FROM v_swing_signals WHERE sig_date=? AND ticker=? AND side='buy'", (session, score["symbol"])).fetchone()
                    score["frozen_plan"] = dict(saved)
                score.update(lifecycle="new", today_buy=side == "buy")
                if side == "buy":
                    active += 1
        conn.commit()
        rows = [dict(r) for r in conn.execute("SELECT * FROM v_swing_signals ORDER BY sig_date,ticker,side")]
    return {"rows": rows, "open_buys": active}


def run_scoring(symbols: list[str], report: dict, source_pools: dict | None = None) -> dict:
    cfg = config()
    if not cfg["enabled"]:
        return {"status": "disabled"}
    ensure_schema()
    session = most_recent_session().isoformat()
    all_symbols = sorted(set(symbols) | set(annotations()) | {"LLY"})
    histories = {s: clean_bars(_read_daily_cache(s), session) for s in all_symbols}
    from v_swing_model import training_panel
    panel, data_audit = training_panel(histories, session, cfg)
    training_hash = hashlib.sha256(pd.util.hash_pandas_object(panel, index=False).values.tobytes()).hexdigest()
    if report.get("training_hash") != training_hash:
        raise ValueError("validation_training_input_changed")
    if report.get("session") != session or report.get("config_hash") != hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest():
        raise ValueError("validation_session_or_config_changed")
    mult = adapt_weekly(session, cfg)
    models = fit_models(panel, cfg, mult)
    version = hashlib.sha256(json.dumps({"config": cfg, "training_hash": training_hash, "multipliers": mult}, sort_keys=True).encode()).hexdigest()[:16]
    scores = {s: score_day(models, s, f, session, cfg) for s, f in histories.items()}
    for s, score in scores.items():
        score["source_pool_ids"] = (source_pools or {}).get(s, [])
        # Earnings are cache-only; unknown is not a claim of no event risk.
        try:
            from priority_board_service import _earnings_brief
            with external_data_scope(False):
                raw = _earnings_brief(s)
            date = raw.get("next_date") or raw.get("date")
            days = (datetime.fromisoformat(date[:10])-datetime.fromisoformat(session)).days if isinstance(date, str) else None
            score.update(earn_warn=days is not None and 0 <= days <= cfg["earnings_warning_days"], earnings_unknown=days is None)
        except (ValueError, TypeError):
            score.update(earn_warn=False, earnings_unknown=True)
    lifecycle = update_lifecycle(scores, histories, session, version, report["pattern_supported"], cfg)
    chart_map = {}
    for event in report.get("forward_audit", {}).get("events", []):
        chart_map.setdefault(event["symbol"], []).append({"time": event["signal_date"], "side": event["side"].upper(),
            "price": (event.get("plan") or {}).get("entry") or float(histories[event["symbol"]].loc[event["signal_date"], "Close"]),
            "code": "v-swing-replay", "reason": "实验性时序回放；非实盘成交", "text": "波段买" if event["side"] == "buy" else "波段卖"})
    for row in lifecycle["rows"]:
        if row["status"] in ("concentration_watch", "conflicting_watch"):
            continue
        chart_map.setdefault(row["ticker"], []).append({"time": row["sig_date"], "side": row["side"].upper(), "price": row["entry"],
            "code": "v-swing-forward", "reason": "实验性前向信号；并非实际成交", "text": "波段买" if row["side"] == "buy" else "波段卖"})
    # One snapshot read serves all candidates; no per-row model calls on page load.
    snapshot = {"status": "completed", "session": session, "generated_at": datetime.now(timezone.utc).isoformat(),
                "model_version": version, "pattern_supported": report["pattern_supported"], "experimental": True,
                "multipliers": mult, "scores": scores, "charts": chart_map, "open_buys": lifecycle["open_buys"],
                "today_buys": sum(bool(r.get("today_buy")) for r in scores.values()),
                "profit_calibration": report["forward_audit"].get("profit_calibration"),
                "data_audit": data_audit, "thresholds": models["thresholds"], "rank_today_first": cfg["rank_today_first"]}
    from priority_board_service import single_stock_signal_read, _liquidity_tier, _adv, _deep_oversold
    from portfolio_timing_service import portfolio_timing_gate
    with external_data_scope(False):
        gate = portfolio_timing_gate()
    added = []
    for symbol, score in scores.items():
        if not score.get("today_buy"):
            continue
        with external_data_scope(False):
            read = single_stock_signal_read(symbol, frame=histories[symbol])
        cp = read.get("calibrated_win_rate")
        if cp is None:
            score["ranking_caveat"] = "原收益校准缺失，仅在个股图表展示，不编造榜单概率"
            continue
        added.append({"symbol": symbol, "current_price": score["close"], "price_as_of": session,
                      "calibrated_probability": cp, "calibration_source": read.get("calibration_source"),
                      "priority_score": cp, "ranking_score": cp, "confidence_badge": "experimental",
                      "relative_strength_20d": read.get("relative_strength_20d") or 0,
                      "regime_multiplier": gate.get("gross_exposure_multiplier", 0),
                      "portfolio_regime": gate.get("regime"), "portfolio_regime_cn": gate.get("regime_cn"),
                      "raw_launch_score": read.get("raw_launch_score") or 0,
                      "daily_tunnel_score": read.get("daily_tunnel_score") or 0, "hv": read.get("hv"),
                      "liquidity": _liquidity_tier(_adv(histories[symbol])), "deep_oversold": _deep_oversold(histories[symbol].Close),
                      "earnings": {"near_earnings": score.get("earn_warn")}, "reason": "v-swing 新实验买点；收益概率沿用原校准",
                      "source_universe_ids": (source_pools or {}).get(symbol, []),
                      "source_universe_labels": [] if (source_pools or {}).get(symbol) else ["v-swing 原始标注池"],
                      "detail_url": f"/single-stock-overnight?symbol={symbol}"})
    snapshot["new_buy_candidates"] = added
    cache_set(AUDIT_KEY, report)
    cache_set(SNAPSHOT_KEY, snapshot)
    return snapshot


def stock_snapshot(symbol: str) -> dict:
    if not config()["enabled"]:
        return {"available": False, "status": "disabled", "markers": [], "score": {}}
    snapshot = cache_get(SNAPSHOT_KEY) or {}
    score = dict((snapshot.get("scores") or {}).get(symbol, {}))
    current = most_recent_session().isoformat()
    if score and snapshot.get("session") != current:
        score["today_buy"] = False
        if score.get("status") == "scored":
            score["status"] = "stale"
    return {"session": snapshot.get("session"), "generated_at": snapshot.get("generated_at"),
            "model_version": snapshot.get("model_version"), "pattern_supported": snapshot.get("pattern_supported"),
            "score": score, "markers": (snapshot.get("charts") or {}).get(symbol, [])[-120:],
            "experimental": True, "available": bool(score)}


def attach_board(board: dict) -> dict:
    cfg = config()
    if not cfg["enabled"]:
        return board
    snapshot = cache_get(SNAPSHOT_KEY) or {}
    picks = []
    current = most_recent_session().isoformat()
    source = list(board.get("picks") or [])
    present = {p["symbol"] for p in source}
    if cfg["rank_today_first"] and snapshot.get("session") == current and snapshot.get("pattern_supported"):
        fresh = {p["symbol"]: p for p in snapshot.get("new_buy_candidates", [])}
        source = [{**p, **fresh.get(p["symbol"], {})} for p in source]
        source.extend(p for p in fresh.values() if p["symbol"] not in present)
    for raw in source:
        pick = dict(raw)
        score = dict((snapshot.get("scores") or {}).get(pick["symbol"], {}))
        score["today_buy"] = bool(score.get("today_buy") and snapshot.get("session") == current
                                   and score.get("price_as_of") == current and snapshot.get("pattern_supported"))
        pick["v_swing"] = score
        picks.append(pick)
    if cfg["rank_today_first"]:
        picks.sort(key=lambda p: (bool(p["v_swing"].get("today_buy")),
                                 float(p.get("calibrated_probability") or 0), float(p.get("ranking_score") or p.get("priority_score") or 0)), reverse=True)
    return {**board, "picks": picks, "n_candidates": max(board.get("n_candidates") or 0, len(picks)),
            "v_swing_summary": {k: snapshot.get(k) for k in ("status", "session", "generated_at", "today_buys", "open_buys", "pattern_supported", "experimental")},
            "v_swing_ranking": cfg["rank_today_first"],
            "method_note": "最新完整交易日的新实验买点优先；同组按原收益校准概率降序。匹配强度不是盈利胜率。" if cfg["rank_today_first"] else board.get("method_note")}
