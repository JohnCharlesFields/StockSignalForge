"""Auditable technical launch-signal scanner used by the web API."""

from __future__ import annotations

import math
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd
from market_data_service import download_daily_history
from research_universe_service import resolve_research_universe
from peer_earnings_signal_service import scan_peer_earnings_signals
from technical_signal_metrics import compute_daily_tunnel_score
from pullback_signal_service import detect_pullback_setup
from signal_calibration import calibrate


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _calibration_horizon() -> int:
    try:
        return max(1, int(os.getenv("VIBE_CALIBRATION_HORIZON", "8")))
    except (TypeError, ValueError):
        return 5


def _attach_calibration_and_log(signals: list[dict[str, Any]], run_id: str) -> dict[str, Any]:
    """Shadow-mode: attach calibrated probabilities and log events for OOS.

    This never changes ranking; it only annotates each signal with the
    empirically calibrated probability (falling back to the legacy sigmoid when
    no curve exists) and records the signal so its realized forward return can
    be measured later.
    """
    horizon = _calibration_horizon()
    as_of_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_events = os.getenv("VIBE_LOG_SIGNAL_EVENTS", "1").lower() not in {"0", "false", "no"}
    pending: list[dict[str, Any]] = []
    calibrated_count = 0
    for row in signals:
        launch_score = _finite(row.get("launch_score"))
        tunnel_native = _finite((row.get("daily_tunnel") or {}).get("score"))
        calib_launch = calibrate("launch", launch_score, horizon)
        calib_tunnel = calibrate("daily_tunnel", tunnel_native, horizon)
        row["calibration"] = {
            "horizon_days": horizon,
            "launch": calib_launch,
            "daily_tunnel": calib_tunnel,
        }
        row["calibrated_probability"] = calib_launch.get("p_calibrated")
        if calib_launch.get("source") == "calibrated":
            calibrated_count += 1
        if log_events:
            pending.append({
                "signal_type": "launch", "symbol": row.get("symbol"), "as_of_date": as_of_date,
                "horizon_days": horizon, "score": launch_score,
                "ref_price": row.get("last_price"), "run_id": run_id,
            })
            pending.append({
                "signal_type": "daily_tunnel", "symbol": row.get("symbol"), "as_of_date": as_of_date,
                "horizon_days": horizon, "score": tunnel_native,
                "ref_price": row.get("last_price"), "run_id": run_id,
            })
    logged = 0
    if pending:
        try:
            from app_database import signal_event_log_many

            logged = signal_event_log_many(pending)
        except Exception:
            logged = 0
    return {
        "horizon_days": horizon,
        "mode": "shadow",
        "n_signals": len(signals),
        "n_calibrated": calibrated_count,
        "events_logged": logged,
    }


def _ticker_frame(download: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if isinstance(download.columns, pd.MultiIndex):
        if ticker in download.columns.get_level_values(0):
            return download[ticker].dropna(how="all")
        if ticker in download.columns.get_level_values(-1):
            return download.xs(ticker, axis=1, level=-1).dropna(how="all")
    return download.dropna(how="all")


def _rsi(close: pd.Series, window: int = 14) -> float:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(window).mean().iloc[-1]
    loss = -delta.clip(upper=0).rolling(window).mean().iloc[-1]
    if not math.isfinite(_finite(loss)) or loss <= 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + gain / loss)


def _score_frame(ticker: str, frame: pd.DataFrame, threshold: float) -> dict[str, Any] | None:
    if frame.empty or "Close" not in frame or len(frame) < 25:
        return None
    close = frame["Close"].dropna().astype(float)
    volume = frame.get("Volume", pd.Series(index=frame.index, dtype=float)).fillna(0).astype(float)
    if len(close) < 25:
        return None
    last = _finite(close.iloc[-1])
    ret_5 = _finite(close.pct_change(5).iloc[-1])
    ret_10 = _finite(close.pct_change(10).iloc[-1])
    ret_20 = _finite(close.pct_change(20).iloc[-1])
    vol_ratio = _finite(volume.iloc[-1] / max(_finite(volume.tail(20).mean()), 1.0), 1.0)
    rolling_high = _finite(close.shift(1).tail(20).max(), last)
    breakout = _finite(last / rolling_high - 1.0) if rolling_high else 0.0
    mean20 = _finite(close.tail(20).mean(), last)
    std20 = _finite(close.tail(20).std(), 0.0)
    bb_position = 0.5 if std20 <= 0 else _finite((last - (mean20 - 2 * std20)) / (4 * std20), 0.5)
    rsi14 = _finite(_rsi(close), 50.0)

    momentum = max(-1.0, min(1.0, 3.2 * ret_5 + 1.8 * ret_20))
    volume_boost = max(-0.4, min(0.8, (vol_ratio - 1.0) / 2.0))
    breakout_boost = max(-0.4, min(0.8, breakout * 12.0))
    rsi_balance = max(-0.3, min(0.3, (rsi14 - 50.0) / 100.0))
    base_score = max(0.0, min(1.0, 0.5 + 0.28 * momentum + 0.20 * volume_boost + 0.22 * breakout_boost + 0.10 * rsi_balance))
    daily_tunnel = compute_daily_tunnel_score(frame)
    recent_frame = frame.tail(35).reset_index()
    recent_frame = recent_frame.assign(Date=lambda data: data.iloc[:, 0].astype(str))
    for column in ("Open", "High", "Low"):
        if column not in recent_frame:
            recent_frame[column] = recent_frame["Close"]
    if "Volume" not in recent_frame:
        recent_frame["Volume"] = 0
    recent_ohlcv = recent_frame[["Date", "Open", "High", "Low", "Close", "Volume"]].to_dict(orient="records")
    pullback_rejection, pullback_confirmation = detect_pullback_setup(recent_ohlcv, ticker)
    tunnel_strength = _finite(daily_tunnel.get("score")) / 100.0
    score = max(0.0, min(1.0, 0.78 * base_score + 0.22 * tunnel_strength))
    if score >= max(0.72, threshold):
        signal, signal_cn = "strong_buy", "强启动观察"
    elif score >= threshold:
        signal, signal_cn = "buy", "启动观察"
    elif score >= max(0.35, threshold - 0.15):
        signal, signal_cn = "watch", "跟踪"
    else:
        signal, signal_cn = "neutral", "等待"
    reasons = []
    if ret_5 > 0.03:
        reasons.append("5日动量增强")
    if vol_ratio > 1.3:
        reasons.append("成交量放大")
    if breakout > 0:
        reasons.append("突破20日高点")
    cycle_state = str(daily_tunnel.get("cycle_state") or "")
    if cycle_state == "down_to_up_turn":
        reasons.append(f"日隧道协商 {daily_tunnel['score']:.1f} 分，处于下行转上行拐点")
    elif tunnel_strength >= 0.70:
        reasons.append(f"日隧道协商 {daily_tunnel['score']:.1f} 分，上行周期买盘延续")
    elif tunnel_strength < 0.40:
        reasons.append(f"日隧道协商 {daily_tunnel['score']:.1f} 分，支撑偏弱")
    if not reasons:
        reasons.append("等待量价进一步确认")
    return {
        "symbol": ticker,
        "launch_score": round(score, 4),
        "base_launch_score": round(base_score, 4),
        "daily_tunnel": daily_tunnel,
        "pullback_rejection": pullback_rejection,
        "pullback_confirmation": pullback_confirmation,
        "signal": signal,
        "signal_cn": signal_cn,
        "last_price": round(last, 2),
        "ret_5d": round(ret_5, 4),
        "ret_10d": round(ret_10, 4),
        "ret_20d": round(ret_20, 4),
        "vol_ratio": round(vol_ratio, 3),
        "rsi_14": round(rsi14, 2),
        "bb_position": round(bb_position, 3),
        "roc_10": round(ret_10, 4),
        "breakout_20": round(breakout, 4),
        "reason": "、".join(reasons),
    }


def scan_launch_signals(
    universe: str = "spx",
    symbols: list[str] | None = None,
    threshold: float = 0.5,
    top: int = 30,
    progress: Callable[[str, float], None] | None = None,
    opportunity_map: dict[str, dict[str, Any]] | None = None,
    scan_mode: str = "technical",
    peer_group: str = "all",
    peer_cache_path: Path | None = None,
    cache_only: bool = False,
) -> dict[str, Any]:
    started = time.time()
    emit = progress or (lambda _phase, _pct: None)
    if scan_mode not in {"technical", "peer_earnings", "combined"}:
        raise ValueError(f"unsupported launch scan mode: {scan_mode}")
    dedicated_peer_pool = universe == "speculative_peer_earnings"
    if dedicated_peer_pool:
        scan_mode = "peer_earnings"
    emit("解析统一股票池", 0.08)
    resolved = None
    tickers = [str(symbol).strip().upper() for symbol in (symbols or []) if str(symbol).strip()]
    if not tickers:
        resolved = resolve_research_universe(universe)
        tickers = resolved["tickers"]
    rows = []
    opportunity_map = opportunity_map or {}
    if scan_mode != "peer_earnings":
        emit(f"批量拉取 {len(tickers)} 只股票行情", 0.18)
        download, data_sources = download_daily_history(tickers, period="6mo")
        emit("计算量价、突破与动量特征", 0.68)
        for ticker in tickers:
            row = _score_frame(ticker, _ticker_frame(download, ticker), threshold)
            if row:
                opportunity = opportunity_map.get(ticker, {})
                row["opportunity_score"] = opportunity.get("opportunity_score")
                row["opportunity_tier"] = opportunity.get("opportunity_tier")
                row["source_pools"] = opportunity.get("source_pools", [])
                if opportunity.get("pullback_rejection"):
                    row["pullback_rejection"] = opportunity.get("pullback_rejection")
                if opportunity.get("pullback_confirmation"):
                    row["pullback_confirmation"] = opportunity.get("pullback_confirmation")
                row["market_data_source"] = data_sources.get(ticker)
                rows.append(row)
    peer_result: dict[str, Any] | None = None
    if scan_mode in {"peer_earnings", "combined"}:
        emit("扫描同赛道财报时间差", 0.76)
        peer_result = scan_peer_earnings_signals(
            group_id=peer_group,
            progress=lambda phase, pct: emit(phase, 0.74 + 0.2 * pct),
            cache_path=peer_cache_path,
            target_symbols=tickers if not dedicated_peer_pool else None,
            include_dynamic_target_groups=not dedicated_peer_pool,
            restrict_targets=not dedicated_peer_pool,
            cache_only=cache_only,
        )
        peer_map = {str(item["symbol"]): item for item in peer_result.get("signals", [])}
        peer_mapping = {
            str(symbol): mapping
            for symbol, mapping in dict(peer_result.get("target_mapping") or {}).items()
        }
        row_map = {str(item["symbol"]): item for item in rows}
        for symbol, mapping in peer_mapping.items():
            row = row_map.get(symbol)
            if row is not None:
                row["peer_mapping"] = mapping
        for symbol, evidence in peer_map.items():
            row = row_map.get(symbol)
            if row is None:
                if scan_mode == "combined" and symbol not in set(tickers):
                    continue
                row = {
                    "symbol": symbol,
                    "launch_score": 0.0,
                    "signal": "neutral",
                    "signal_cn": "等待",
                    "last_price": _finite(evidence.get("target_latest_price")),
                    "ret_5d": 0.0,
                    "ret_10d": 0.0,
                    "ret_20d": 0.0,
                    "vol_ratio": 0.0,
                    "rsi_14": 50.0,
                    "bb_position": 0.5,
                    "roc_10": 0.0,
                    "breakout_20": 0.0,
                    "reason": "",
                    "opportunity_score": None,
                    "opportunity_tier": None,
                    "source_pools": [],
                    "daily_tunnel": evidence.get("daily_tunnel"),
                }
                rows.append(row)
                row_map[symbol] = row
            technical_score = _finite(row.get("launch_score"))
            relay_score = _finite(evidence.get("relay_score"))
            tunnel_strength = _finite((row.get("daily_tunnel") or {}).get("score")) / 100.0
            row["technical_launch_score"] = round(technical_score, 4)
            row["peer_mapping"] = peer_mapping.get(symbol) or row.get("peer_mapping")
            row["peer_earnings"] = evidence
            # Theme validation receives high weight; tape features still act
            # as a guardrail against blindly following a peer.
            row["launch_score"] = round(min(1.0, 0.42 * technical_score + 0.42 * relay_score + 0.12 * tunnel_strength + 0.04), 4)
            if row["launch_score"] >= max(0.72, threshold):
                row["signal"], row["signal_cn"] = "strong_buy", "强启动观察"
            elif row["launch_score"] >= threshold:
                row["signal"], row["signal_cn"] = "buy", "启动观察"
            elif row["launch_score"] >= max(0.35, threshold - 0.15):
                row["signal"], row["signal_cn"] = "watch", "跟踪"
            row["reason"] = "；".join(filter(None, [
                row.get("reason"),
                f"同行财报接力：{evidence['leader_symbol']} 已先发，{symbol} 尚待披露",
                f"相对滞涨差 {evidence['relative_lag_gap'] * 100:.1f}%",
                "下一次披露日待人工确认" if evidence.get("target_report_date_requires_manual_check") else None,
            ]))
    if scan_mode == "peer_earnings":
        rows = [row for row in rows if row.get("peer_earnings")]
    rows.sort(key=lambda item: item["launch_score"], reverse=True)
    eligible_rows = [row for row in rows if row["launch_score"] >= max(0.35, threshold - 0.15)]
    if scan_mode == "combined":
        selected = {row["symbol"]: row for row in eligible_rows[:top]}
        for row in rows:
            if row.get("peer_earnings"):
                selected[row["symbol"]] = row
        signals = sorted(selected.values(), key=lambda item: item["launch_score"], reverse=True)
    else:
        signals = eligible_rows[:top]
    emit("整理启动信号排名", 0.95)
    run_id = f"launch_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    calibration_summary = _attach_calibration_and_log(signals, run_id)
    return {
        "run_id": run_id,
        "scan_time": datetime.utcnow().isoformat() + "Z",
        "elapsed_seconds": round(time.time() - started, 3),
        "n_scanned": len(rows),
        "n_signals": len(signals),
        "threshold": threshold,
        "scan_mode": scan_mode,
        "peer_group": peer_group,
        "signals": signals,
        "peer_earnings": peer_result,
        "calibration": calibration_summary,
        "feature_stats": {
            "requested_tickers": len(tickers),
            "resolved_tickers": len(rows),
            "universe": universe if resolved else "custom",
        },
    }
