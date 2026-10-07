"""Underlying-driven Call/Put direction research for the priority board."""

from __future__ import annotations

import json
import math
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from consensus_signal_service import consensus_feature_frame
from market_data_service import _CACHE_ROOT, external_data_scope, get_daily_history, get_latest_us_market_close_utc
from option_volume_leaders_service import rank_non_seven_stocks
from scripts.backtest_leader_long_options import EASTERN, build_leader_snapshot
from wave_structure_service import detect_wave_structure

SNAPSHOT_PATH = _CACHE_ROOT / "long_option_screen" / "latest.json"
_lock = threading.Lock()
_job: dict[str, Any] = {"status": "idle", "processed": 0, "total": 0}


def read_snapshot() -> dict[str, Any]:
    if not SNAPSHOT_PATH.exists():
        return {"available": False, "snapshot": None, "reason": "尚无已完成筛选快照；点击运行筛选后生成。"}
    try:
        snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        as_of = snapshot.get("data_as_of")
        latest_close = get_latest_us_market_close_utc().astimezone(EASTERN).date().isoformat()
        return {
            "available": True,
            "snapshot": snapshot,
            "stale": not as_of or as_of != latest_close,
            "latest_completed_session": latest_close,
        }
    except (OSError, ValueError, TypeError) as exc:
        return {"available": False, "snapshot": None, "reason": f"快照无法读取：{type(exc).__name__}"}


def job_status() -> dict[str, Any]:
    with _lock:
        return dict(_job)


def _set_job(**changes: Any) -> None:
    with _lock:
        _job.update(changes)


def start_screen() -> dict[str, Any]:
    with _lock:
        if _job.get("status") == "running":
            return dict(_job)
        _job.clear()
        _job.update({"job_id": uuid.uuid4().hex, "status": "running", "processed": 0, "total": 0,
                     "started_at": datetime.now(timezone.utc).isoformat(), "stage": "准备股票池"})
        result = dict(_job)
    threading.Thread(target=_run_screen, name="long-option-screen", daemon=True).start()
    return result


def _daily_for_session(symbol: str, session: Any) -> pd.DataFrame:
    daily, _ = get_daily_history(symbol, period="2y", allow_yfinance_fallback=False)
    if daily is None or daily.empty or len(daily) < 220 or pd.Timestamp(daily.index[-1]).date() < session:
        daily, _ = get_daily_history(symbol, period="2y", allow_yfinance_fallback=True)
    return daily if daily is not None else pd.DataFrame()


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 4) if math.isfinite(number) else None


def _technical_context(daily: pd.DataFrame) -> dict[str, Any]:
    """Causal price levels only; neither these levels nor wave shapes set direction."""
    recent = daily.tail(60)
    if len(recent) < 20 or not {"High", "Low", "Close"}.issubset(recent.columns):
        return {}
    high = pd.to_numeric(recent["High"], errors="coerce")
    low = pd.to_numeric(recent["Low"], errors="coerce")
    if high.isna().any() or low.isna().any() or float(high.max()) <= float(low.min()):
        return {}
    high_pos, low_pos = int(high.values.argmax()), int(low.values.argmin())
    up_swing = low_pos < high_pos
    upper, lower = float(high.max()), float(low.min())
    span = upper - lower
    levels = {label: _finite(upper - span * ratio if up_swing else lower + span * ratio)
              for label, ratio in (("38.2", 0.382), ("50", 0.5), ("61.8", 0.618))}
    return {
        "range_sessions": len(recent), "range_high": _finite(upper), "range_low": _finite(lower),
        "swing_direction": "up" if up_swing else "down",
        "fib_retracement": levels,
        "breakout_price": _finite(high.tail(10).max()),
        "support_price": _finite(low.tail(10).min()),
        "wave_status": (wave := detect_wave_structure(daily))["status"],
        "wave_structure": wave,
    }


def _signal(symbol: str, benchmark: pd.DataFrame, session: Any) -> dict[str, Any]:
    daily = _daily_for_session(symbol, session)
    if daily.empty or "Close" not in daily:
        return {"status": "missing_daily_history", "side": None}
    daily = daily[pd.to_datetime(daily.index).date <= session].tail(280)
    if daily.empty:
        return {"status": "missing_daily_history", "side": None}
    base = {
        "spot": _finite(daily["Close"].iloc[-1]),
        "stock_data_as_of": pd.Timestamp(daily.index[-1]).date().isoformat(),
    }
    if len(daily) < 220:
        return {**base, "status": "missing_daily_history", "side": None}
    benchmark = benchmark[pd.to_datetime(benchmark.index).date <= session] if not benchmark.empty else benchmark
    features = consensus_feature_frame(daily, benchmark)
    if features.empty:
        return {**base, "status": "missing_signal", "side": None}
    row = features.iloc[-1]
    net = _finite(row.get("net_consensus"))
    if base["spot"] is None or base["spot"] <= 0 or net is None:
        return {**base, "status": "missing_signal", "side": None}
    side = "C" if net > 0.12 else "P" if net < -0.12 else None
    benchmark_as_of = (
        pd.Timestamp(benchmark.index[-1]).date().isoformat()
        if benchmark is not None and not benchmark.empty else None
    )
    return {
        **base,
        "status": "signal" if side else "no_direction",
        "side": side,
        "net_consensus": net,
        "signal_score": round(abs(net), 4),
        "bull_consensus": _finite(row.get("bull_consensus")),
        "bear_consensus": _finite(row.get("bear_consensus")),
        "hv20": _finite(row.get("hv20")),
        "relative_strength_20d": _finite(row.get("rs20")) if benchmark_as_of == session.isoformat() else None,
        "benchmark_data_as_of": benchmark_as_of,
        "technical_context": _technical_context(daily),
    }


def _screen_one(symbol: str, session: Any, benchmark: pd.DataFrame) -> dict[str, Any]:
    item: dict[str, Any] = {"symbol": symbol, "source": "equity:daily_ohlcv"}
    signal = _signal(symbol, benchmark, session)
    item.update(signal)
    if signal.get("stock_data_as_of") and signal["stock_data_as_of"] != session.isoformat():
        item["status"] = "stale_stock_data"
        item["side"] = None
    return item


def _run_screen() -> None:
    try:
        now = datetime.now(timezone.utc)
        session = get_latest_us_market_close_utc(now).astimezone(EASTERN).date()
        universe = build_leader_snapshot(_CACHE_ROOT, as_of=now)
        _set_job(stage="读取Cboe近一年期权成交量")
        option_volume = rank_non_seven_stocks(session)
        leaders = {row["symbol"]: row for row in option_volume["leaders"]} if option_volume.get("available") else {}
        symbols = sorted(set(universe["eligible_symbols"]) | set(leaders))
        _set_job(total=len(symbols), stage="读取正股信号")
        with external_data_scope(True):
            benchmark = _daily_for_session("SPY", session)
        benchmark = benchmark.tail(280) if benchmark is not None else pd.DataFrame()
        rows: list[dict[str, Any]] = []
        for index, symbol in enumerate(symbols, start=1):
            _set_job(stage=f"筛选 {symbol}")
            try:
                row = _screen_one(symbol, session, benchmark)
                if symbol in leaders:
                    row["option_volume_12m"] = leaders[symbol]["contracts_12m"]
                    row["universe_source"] = "cboe_option_volume_top10"
                else:
                    row["universe_source"] = "magnificent_seven_or_verified_sector_leader"
                rows.append(row)
            except Exception as exc:
                rows.append({"symbol": symbol, "status": "data_unavailable", "side": None,
                             "reason": f"{type(exc).__name__}: {str(exc)[:180]}"})
            _set_job(processed=index)
        rows.sort(key=lambda row: (row.get("status") != "signal", -(row.get("signal_score") or 0), row["symbol"]))
        snapshot = {
            "generated_at": datetime.now(timezone.utc).isoformat(), "data_as_of": session.isoformat(),
            "source": "equity:daily_ohlcv", "rows": rows, "universe_count": len(symbols),
            "verified_sector_groups": universe["complete_recent_groups"],
            "sector_group_count": universe["group_count"],
            "option_data_request_count": 0,
            "option_volume_research": option_volume,
            "note": "正股方向与形态研究；未建模真实合约权利金、IV及期权收益。",
        }
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = SNAPSHOT_PATH.with_suffix(".tmp")
        temp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temp.replace(SNAPSHOT_PATH)
        try:
            from long_option_shadow_service import record_snapshot, resolve_pending
            shadow = record_snapshot(snapshot)
            settled = resolve_pending()
            _set_job(shadow_recorded=shadow.get("inserted", 0), shadow_resolved=settled.get("resolved", 0))
        except Exception as exc:
            _set_job(shadow_warning=f"{type(exc).__name__}: {str(exc)[:180]}")
        _set_job(status="completed", stage="完成", finished_at=datetime.now(timezone.utc).isoformat())
    except Exception as exc:
        _set_job(status="failed", stage="失败", error=f"{type(exc).__name__}: {str(exc)[:240]}",
                 finished_at=datetime.now(timezone.utc).isoformat())
