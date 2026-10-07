"""SOXL-only overnight quantitative research and paper-trading service.

The module is deliberately isolated from broker execution.  It accepts only
SOXL, consumes Blue Ocean ATS one-minute BBO data, simulates fills at the
executable side of the quote, and persists research runs/trades in the local
SQLite database.

Historical and live paths share the same feature and decision functions.  This
keeps the historical replay useful as a test of the exact logic that will be
observed by the live paper process.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

import app_database


try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
except Exception:
    pass


SYMBOL = "SOXL"
DATASET = os.environ.get("SOXL_DATABENTO_DATASET", "OCEA.MEMOIR").strip() or "OCEA.MEMOIR"
SCHEMA = os.environ.get("SOXL_DATABENTO_SCHEMA", "bbo-1m").strip() or "bbo-1m"
AGENT_DIR = Path(__file__).resolve().parent
CACHE_DIR = AGENT_DIR / "data_cache" / "soxl_quant"
RESULT_DIR = AGENT_DIR / "runs" / "soxl_quant"


@dataclass(frozen=True)
class StrategyConfig:
    fast_ema: int = 12
    slow_ema: int = 60
    momentum_bars: int = 15
    signal_threshold: float = 0.45
    max_spread_bps: float = 35.0
    min_session_bar: int = 25
    flatten_before_end_bars: int = 8
    max_holding_bars: int = 60
    stop_vol_multiple: float = 2.5
    min_stop_pct: float = 0.006
    take_profit_r: float = 1.8
    risk_per_trade: float = 0.0075
    max_position_ratio: float = 0.35
    slippage_bps: float = 1.0
    min_signal_flip: float = 0.10


DEFAULT_CONFIG = StrategyConfig()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _databento_ready() -> bool:
    return bool(os.environ.get("DATABENTO_API_KEY", "").strip())


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _session_id(index: pd.DatetimeIndex, session_mode: str = "overnight") -> pd.Series:
    local = index.tz_convert("America/New_York")
    if session_mode == "regular_proxy":
        return pd.Series(local.strftime("%Y-%m-%d"), index=index)
    shifted = local - pd.Timedelta(hours=20)
    return pd.Series(shifted.strftime("%Y-%m-%d"), index=index)


def _cache_path(start: str, end: str) -> Path:
    key = hashlib.sha1(f"{DATASET}|{SCHEMA}|{SYMBOL}|{start}|{end}".encode("utf-8")).hexdigest()[:16]
    return CACHE_DIR / f"{SYMBOL}_{start[:10]}_{end[:10]}_{key}.parquet"


def _download_chunk(client: Any, start: str, end: str, *, retries: int = 4) -> pd.DataFrame:
    path = _cache_path(start, end)
    if path.exists():
        return pd.read_parquet(path)
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            store = client.timeseries.get_range(
                dataset=DATASET,
                schema=SCHEMA,
                symbols=[SYMBOL],
                stype_in="raw_symbol",
                start=start,
                end=end,
            )
            frame = store.to_df()
            if frame is not None and not frame.empty:
                frame.to_parquet(path)
                return frame
            return pd.DataFrame()
        except Exception as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"Databento chunk failed {start}..{end}: {last_error}")


def estimate_history_cost(start: str, end: str) -> dict[str, Any]:
    """Return Databento's estimate without downloading market data."""
    if not _databento_ready():
        raise RuntimeError("DATABENTO_API_KEY is not configured; refusing to estimate or download SOXL data")
    import databento as db

    client = db.Historical()
    kwargs = {
        "dataset": DATASET,
        "schema": SCHEMA,
        "symbols": [SYMBOL],
        "stype_in": "raw_symbol",
        "start": start,
        "end": end,
    }
    return {
        "dataset": DATASET,
        "schema": SCHEMA,
        "symbol": SYMBOL,
        "start": start,
        "end": end,
        "estimated_cost_usd": round(float(client.metadata.get_cost(**kwargs)), 6),
        "estimated_billable_bytes": int(client.metadata.get_billable_size(**kwargs)),
    }


def fetch_history(start: str, end: str, *, force: bool = False) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Load SOXL Blue Ocean BBO bars, caching the exact requested range."""
    if not _databento_ready():
        raise RuntimeError("DATABENTO_API_KEY is not configured; add it to agent/.env before running SOXL backtest")
    import databento as db

    client = db.Historical()
    available = client.metadata.get_dataset_range(DATASET)
    available_end = str(available.get("schema", {}).get(SCHEMA, {}).get("end") or available.get("end") or end)
    requested_end = end
    effective_end = min(pd.Timestamp(end, tz="UTC"), pd.Timestamp(available_end)).isoformat()
    path = _cache_path(start, effective_end)
    if path.exists() and not force:
        frame = pd.read_parquet(path)
        return frame, {
            "source": "cache",
            "path": str(path),
            "rows": len(frame),
            "requested_end": requested_end,
            "effective_end": effective_end,
            "available_end": available_end,
        }

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(effective_end)
    chunks: list[pd.DataFrame] = []
    cursor = start_ts
    while cursor < end_ts:
        chunk_end = min(cursor + pd.Timedelta(days=31), end_ts)
        chunks.append(_download_chunk(client, cursor.isoformat(), chunk_end.isoformat()))
        cursor = chunk_end
    frame = pd.concat([x for x in chunks if x is not None and not x.empty]).sort_index() if any(not x.empty for x in chunks) else pd.DataFrame()
    if not frame.empty:
        frame = frame[~frame.index.duplicated(keep="last")]
    if frame is None or frame.empty:
        raise RuntimeError(f"No {SYMBOL} data returned for {start}..{end}")
    frame.to_parquet(path)
    return frame, {
        "source": "databento",
        "path": str(path),
        "rows": len(frame),
        "requested_end": requested_end,
        "effective_end": effective_end,
        "available_end": available_end,
    }


def fetch_yfinance_proxy_history(start: str, end: str | None = None) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Fetch a clearly-labelled regular-session proxy for local research.

    Yahoo Finance does not provide the Blue Ocean overnight book used by the
    live path. This adapter is only for a quick local sanity backtest; it
    synthesizes a narrow BBO around 5-minute OHLCV closes and must not be
    interpreted as an overnight execution result.
    """
    import yfinance as yf

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    yf.set_tz_cache_location(str(CACHE_DIR / "yfinance_tz"))
    requested_end = pd.Timestamp(end or datetime.now(timezone.utc))
    requested_end = requested_end.tz_localize("UTC") if requested_end.tzinfo is None else requested_end.tz_convert("UTC")
    requested_start = pd.Timestamp(start, tz="UTC")
    # Yahoo limits 5-minute history. Keep the constraint visible in the result.
    effective_start = max(requested_start, requested_end - pd.Timedelta(days=59))
    raw = yf.download(
        SYMBOL,
        start=effective_start.strftime("%Y-%m-%d"),
        end=requested_end.strftime("%Y-%m-%d"),
        interval="5m",
        prepost=True,
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    if raw is None or raw.empty:
        raise RuntimeError(f"No yfinance 5m data returned for {SYMBOL}")
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = [str(col[0]) for col in raw.columns]
    raw.index = pd.DatetimeIndex(pd.to_datetime(raw.index, utc=True))
    close = pd.to_numeric(raw.get("Close"), errors="coerce")
    volume = pd.to_numeric(raw.get("Volume"), errors="coerce").fillna(0.0)
    local = raw.index.tz_convert("America/New_York")
    minute = local.hour * 60 + local.minute
    regular = (local.weekday < 5) & (minute >= 570) & (minute < 960)
    close = close[regular].dropna()
    volume = volume.reindex(close.index).fillna(0.0)
    if close.empty:
        raise RuntimeError(f"No regular-session yfinance bars returned for {SYMBOL}")
    spread_bps = max(1.0, float(os.environ.get("SOXL_PROXY_SPREAD_BPS", "8.0")))
    half = spread_bps / 20000.0
    frame = pd.DataFrame(index=close.index)
    frame["bid_px_00"] = close * (1.0 - half)
    frame["ask_px_00"] = close * (1.0 + half)
    frame["bid_sz_00"] = np.maximum(volume.to_numpy(dtype=float) / 2.0, 1.0)
    frame["ask_sz_00"] = np.maximum(volume.to_numpy(dtype=float) / 2.0, 1.0)
    return frame, {
        "source": "yfinance_regular_proxy",
        "rows": len(frame),
        "requested_start": start,
        "requested_end": end,
        "effective_start": effective_start.isoformat(),
        "effective_end": requested_end.isoformat(),
        "proxy_spread_bps": spread_bps,
        "warning": "Regular-session 5m OHLCV proxy; not Databento Blue Ocean overnight BBO.",
    }


def prepare_bars(raw: pd.DataFrame, config: StrategyConfig = DEFAULT_CONFIG, *, session_mode: str = "overnight") -> pd.DataFrame:
    """Normalize BBO bars and compute point-in-time features."""
    if raw is None or raw.empty:
        return pd.DataFrame()
    frame = raw.copy()
    idx = pd.DatetimeIndex(pd.to_datetime(frame.index, utc=True))
    frame.index = idx
    frame = frame.sort_index()

    required = ("bid_px_00", "ask_px_00", "bid_sz_00", "ask_sz_00")
    missing = [name for name in required if name not in frame.columns]
    if missing:
        raise ValueError(f"Missing BBO columns: {', '.join(missing)}")

    for col in required:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    if frame["ask_px_00"].dropna().median() > 1_000_000:
        frame["bid_px_00"] /= 1e9
        frame["ask_px_00"] /= 1e9

    frame["bid"] = frame["bid_px_00"]
    frame["ask"] = frame["ask_px_00"]
    frame["mid"] = (frame["bid"] + frame["ask"]) / 2.0
    frame["spread_bps"] = (frame["ask"] - frame["bid"]) / frame["mid"].replace(0.0, np.nan) * 10000.0
    depth = frame["bid_sz_00"] + frame["ask_sz_00"]
    frame["book_imbalance"] = (frame["bid_sz_00"] - frame["ask_sz_00"]) / depth.replace(0.0, np.nan)
    frame["session_id"] = _session_id(frame.index, session_mode)
    local = frame.index.tz_convert("America/New_York")
    if session_mode == "regular_proxy":
        frame["session_bar"] = [(ts.hour * 60 + ts.minute) - (9 * 60 + 30) for ts in local]
        session_minutes = 390
    else:
        frame["session_bar"] = [
            (ts.hour - 20) * 60 + ts.minute if ts.hour >= 20 else (ts.hour + 4) * 60 + ts.minute
            for ts in local
        ]
        session_minutes = 480
    frame["session_size"] = frame.groupby("session_id")["mid"].transform("size")
    frame["bars_to_end"] = (session_minutes - frame["session_bar"]).clip(lower=0)

    log_mid = np.log(frame["mid"].where(frame["mid"] > 0))
    frame["ret_1"] = log_mid.diff()
    # Reset cross-session returns to prevent the regular-session price gap from
    # being mistaken for overnight momentum.
    new_session = frame["session_id"].ne(frame["session_id"].shift(1))
    frame.loc[new_session, "ret_1"] = np.nan
    frame["vol_30"] = frame.groupby("session_id")["ret_1"].transform(lambda s: s.rolling(30, min_periods=15).std())
    frame["ema_fast"] = frame.groupby("session_id")["mid"].transform(
        lambda s: s.ewm(span=config.fast_ema, adjust=False, min_periods=config.fast_ema).mean()
    )
    frame["ema_slow"] = frame.groupby("session_id")["mid"].transform(
        lambda s: s.ewm(span=config.slow_ema, adjust=False, min_periods=config.slow_ema).mean()
    )
    frame["momentum"] = frame.groupby("session_id")["mid"].transform(
        lambda s: np.log(s / s.shift(config.momentum_bars))
    )

    vol = frame["vol_30"].replace(0.0, np.nan)
    trend_scale = vol * math.sqrt(max(1, config.slow_ema - config.fast_ema))
    momentum_scale = vol * math.sqrt(max(1, config.momentum_bars))
    trend_z = np.log(frame["ema_fast"] / frame["ema_slow"]) / trend_scale
    momentum_z = frame["momentum"] / momentum_scale
    frame["trend_component"] = np.tanh(trend_z.clip(-4.0, 4.0))
    frame["momentum_component"] = np.tanh(momentum_z.clip(-4.0, 4.0))
    frame["book_component"] = frame["book_imbalance"].clip(-1.0, 1.0).fillna(0.0)
    frame["signal_score"] = (
        0.45 * frame["trend_component"]
        + 0.35 * frame["momentum_component"]
        + 0.20 * frame["book_component"]
    )
    frame["stop_distance_pct"] = (
        frame["vol_30"] * math.sqrt(5.0) * config.stop_vol_multiple
    ).clip(lower=config.min_stop_pct, upper=0.04)
    return frame.replace([np.inf, -np.inf], np.nan).dropna(subset=["mid", "bid", "ask"])


def _config_grid() -> list[StrategyConfig]:
    """Small, interpretable grid used only on the training/validation period."""
    out: list[StrategyConfig] = []
    for (fast, slow), threshold, (hold, take_r) in itertools.product(
        ((8, 48), (12, 60), (20, 72)),
        (0.35, 0.50),
        ((45, 1.5), (90, 1.5), (90, 2.0)),
    ):
        if fast >= slow:
            continue
        out.append(
            StrategyConfig(
                fast_ema=fast,
                slow_ema=slow,
                signal_threshold=threshold,
                max_holding_bars=hold,
                take_profit_r=take_r,
            )
        )
    return out


def _fill_price(side: int, action: str, row: pd.Series, slippage_bps: float) -> float:
    """Executable quote-side fill plus adverse slippage."""
    slip = max(0.0, slippage_bps) / 10000.0
    if action == "entry":
        return float(row["ask"] * (1.0 + slip)) if side > 0 else float(row["bid"] * (1.0 - slip))
    return float(row["bid"] * (1.0 - slip)) if side > 0 else float(row["ask"] * (1.0 + slip))


def _max_drawdown(curve: Iterable[float]) -> float:
    arr = np.asarray(list(curve), dtype=float)
    if not len(arr):
        return 0.0
    peak = np.maximum.accumulate(arr)
    return float(np.min(arr / np.where(peak == 0.0, 1.0, peak) - 1.0))


def simulate(
    bars: pd.DataFrame,
    config: StrategyConfig,
    *,
    initial_capital: float = 2000.0,
    start_session: str | None = None,
    end_session: str | None = None,
    session_mode: str = "overnight",
) -> dict[str, Any]:
    """Run a no-look-ahead long/short paper simulation."""
    if bars.empty:
        return _empty_result(initial_capital)
    frame = prepare_bars(bars, config, session_mode=session_mode)
    if start_session:
        frame = frame[frame["session_id"] >= start_session]
    if end_session:
        frame = frame[frame["session_id"] <= end_session]
    if frame.empty:
        return _empty_result(initial_capital)

    capital = float(initial_capital)
    side = 0
    qty = 0
    entry_price = 0.0
    entry_time: pd.Timestamp | None = None
    entry_score = 0.0
    stop_price = 0.0
    target_price = 0.0
    holding = 0
    pending_entry = 0
    pending_exit: str | None = None
    trades: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    long_trades = short_trades = 0

    rows = list(frame.iterrows())
    for i, (ts, row) in enumerate(rows):
        same_session_as_previous = i > 0 and row["session_id"] == rows[i - 1][1]["session_id"]

        if side != 0 and not same_session_as_previous:
            prev_ts, prev = rows[i - 1]
            exit_price = _fill_price(side, "exit", prev, config.slippage_bps)
            pnl = side * qty * (exit_price - entry_price)
            capital += pnl
            trades.append(_trade_payload(side, qty, entry_time, prev_ts, entry_price, exit_price, pnl, "session_end", entry_score, holding))
            side = qty = holding = 0
            pending_exit = None

        if side != 0 and pending_exit:
            exit_price = _fill_price(side, "exit", row, config.slippage_bps)
            pnl = side * qty * (exit_price - entry_price)
            capital += pnl
            trades.append(_trade_payload(side, qty, entry_time, ts, entry_price, exit_price, pnl, pending_exit, entry_score, holding))
            side = qty = holding = 0
            pending_exit = None

        if side == 0 and pending_entry and same_session_as_previous:
            fill = _fill_price(pending_entry, "entry", row, config.slippage_bps)
            stop_pct = float(row.get("stop_distance_pct") or config.min_stop_pct)
            per_share_risk = max(fill * stop_pct, fill * config.min_stop_pct)
            risk_budget = capital * config.risk_per_trade
            qty_risk = int(risk_budget / per_share_risk) if per_share_risk > 0 else 0
            qty_notional = int(capital * config.max_position_ratio / fill) if fill > 0 else 0
            fill_qty = max(0, min(qty_risk, qty_notional))
            if fill_qty >= 1:
                side = pending_entry
                qty = fill_qty
                entry_price = fill
                entry_time = ts
                entry_score = float(row.get("signal_score") or 0.0)
                stop_price = fill - side * per_share_risk
                target_price = fill + side * per_share_risk * config.take_profit_r
                holding = 0
                if side > 0:
                    long_trades += 1
                else:
                    short_trades += 1
            pending_entry = 0

        if side != 0:
            holding += 1
            mid = float(row["mid"])
            score = float(row.get("signal_score") or 0.0)
            if side > 0 and mid <= stop_price:
                pending_exit = "stop"
            elif side < 0 and mid >= stop_price:
                pending_exit = "stop"
            elif side > 0 and mid >= target_price:
                pending_exit = "target"
            elif side < 0 and mid <= target_price:
                pending_exit = "target"
            elif side > 0 and score < -config.min_signal_flip:
                pending_exit = "signal_flip"
            elif side < 0 and score > config.min_signal_flip:
                pending_exit = "signal_flip"
            elif holding >= config.max_holding_bars:
                pending_exit = "time"
            elif int(row["bars_to_end"]) <= config.flatten_before_end_bars:
                pending_exit = "session_end"

        if side == 0 and pending_entry == 0 and pending_exit is None:
            score = _safe_float(row.get("signal_score"))
            spread = _safe_float(row.get("spread_bps"))
            eligible = (
                score is not None
                and spread is not None
                and int(row["session_bar"]) >= config.min_session_bar
                and int(row["bars_to_end"]) > config.flatten_before_end_bars + 1
                and spread <= config.max_spread_bps
                and pd.notna(row.get("stop_distance_pct"))
            )
            if eligible and score >= config.signal_threshold:
                pending_entry = 1
            elif eligible and score <= -config.signal_threshold:
                pending_entry = -1

        mark = capital
        if side != 0:
            mark += side * qty * (float(row["mid"]) - entry_price)
        equity_curve.append({"timestamp": ts.isoformat(), "equity": round(mark, 6)})

    if side != 0:
        ts, row = rows[-1]
        exit_price = _fill_price(side, "exit", row, config.slippage_bps)
        pnl = side * qty * (exit_price - entry_price)
        capital += pnl
        trades.append(_trade_payload(side, qty, entry_time, ts, entry_price, exit_price, pnl, "data_end", entry_score, holding))
        equity_curve[-1]["equity"] = round(capital, 6)

    return _summarize(initial_capital, capital, trades, equity_curve, long_trades, short_trades, frame)


def _trade_payload(
    side: int,
    qty: int,
    entry_time: pd.Timestamp | None,
    exit_time: pd.Timestamp,
    entry_price: float,
    exit_price: float,
    pnl: float,
    reason: str,
    score: float,
    holding: int,
) -> dict[str, Any]:
    notional = abs(entry_price * qty)
    return {
        "symbol": SYMBOL,
        "side": "LONG" if side > 0 else "SHORT",
        "qty": int(qty),
        "entry_time": entry_time.isoformat() if entry_time is not None else None,
        "exit_time": exit_time.isoformat(),
        "entry_price": round(entry_price, 4),
        "exit_price": round(exit_price, 4),
        "pnl": round(pnl, 4),
        "return_pct": round(pnl / notional, 6) if notional > 0 else 0.0,
        "exit_reason": reason,
        "entry_score": round(score, 4),
        "holding_bars": int(holding),
    }


def _summarize(
    initial: float,
    final: float,
    trades: list[dict[str, Any]],
    curve: list[dict[str, Any]],
    long_trades: int,
    short_trades: int,
    frame: pd.DataFrame,
) -> dict[str, Any]:
    pnls = [float(t["pnl"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    equity = [float(x["equity"]) for x in curve]
    session_close = frame.groupby("session_id").tail(1)
    daily_equity = pd.Series(
        [equity[frame.index.get_loc(ts)] for ts in session_close.index if ts in frame.index],
        index=session_close.index[: len(session_close)],
        dtype=float,
    )
    daily_ret = daily_equity.pct_change().dropna()
    sharpe = float(daily_ret.mean() / daily_ret.std() * math.sqrt(252.0)) if len(daily_ret) > 2 and daily_ret.std() > 0 else 0.0
    rng = np.random.default_rng(42)
    mean_ci: list[float | None] = [None, None]
    if len(daily_ret) >= 10:
        values = daily_ret.to_numpy(dtype=float)
        boot = np.asarray([rng.choice(values, size=len(values), replace=True).mean() for _ in range(3000)])
        mean_ci = [round(float(np.quantile(boot, 0.025)), 6), round(float(np.quantile(boot, 0.975)), 6)]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    side_stats: dict[str, Any] = {}
    for label in ("LONG", "SHORT"):
        selected = [t for t in trades if t.get("side") == label]
        selected_pnl = [float(t.get("pnl") or 0.0) for t in selected]
        selected_wins = [p for p in selected_pnl if p > 0]
        selected_losses = [p for p in selected_pnl if p < 0]
        side_stats[label] = {
            "trade_count": len(selected),
            "win_rate": round(len(selected_wins) / len(selected), 6) if selected else 0.0,
            "net_profit": round(sum(selected_pnl), 4),
            "profit_factor": round(sum(selected_wins) / abs(sum(selected_losses)), 4) if selected_losses else (999.0 if selected_wins else 0.0),
        }
    return {
        "initial_capital": round(initial, 2),
        "final_equity": round(final, 2),
        "net_profit": round(final - initial, 2),
        "total_return": round(final / initial - 1.0, 6) if initial > 0 else 0.0,
        "max_drawdown": round(_max_drawdown(equity), 6),
        "sharpe": round(sharpe, 4),
        "trade_count": len(trades),
        "long_trades": long_trades,
        "short_trades": short_trades,
        "win_rate": round(len(wins) / len(trades), 6) if trades else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0),
        "avg_trade_pnl": round(float(np.mean(pnls)), 4) if pnls else 0.0,
        "mean_session_return": round(float(daily_ret.mean()), 6) if len(daily_ret) else 0.0,
        "mean_session_return_ci95": mean_ci,
        "session_edge_significant": bool(mean_ci[0] is not None and mean_ci[0] > 0),
        "positive_session_rate": round(float((daily_ret > 0).mean()), 6) if len(daily_ret) else 0.0,
        "side_stats": side_stats,
        "sessions": int(frame["session_id"].nunique()),
        "start": frame.index.min().isoformat(),
        "end": frame.index.max().isoformat(),
        "trades": trades,
        "equity_curve": curve,
    }


def _empty_result(initial: float) -> dict[str, Any]:
    return {
        "initial_capital": initial,
        "final_equity": initial,
        "net_profit": 0.0,
        "total_return": 0.0,
        "max_drawdown": 0.0,
        "sharpe": 0.0,
        "trade_count": 0,
        "long_trades": 0,
        "short_trades": 0,
        "win_rate": 0.0,
        "profit_factor": 0.0,
        "avg_trade_pnl": 0.0,
        "sessions": 0,
        "trades": [],
        "equity_curve": [],
    }


def _selection_score(result: dict[str, Any]) -> float:
    count = int(result.get("trade_count") or 0)
    if count < 20:
        return -999.0 + count
    ret = float(result.get("total_return") or 0.0)
    dd = abs(float(result.get("max_drawdown") or 0.0))
    sharpe = max(-3.0, min(3.0, float(result.get("sharpe") or 0.0)))
    return ret - 0.75 * dd + 0.02 * sharpe


def _session_splits(frame: pd.DataFrame) -> dict[str, tuple[str, str]]:
    sessions = sorted(str(x) for x in frame["session_id"].dropna().unique())
    if len(sessions) < 30:
        raise RuntimeError(f"Need at least 30 sessions, received {len(sessions)}")
    train_end = max(1, int(len(sessions) * 0.60))
    validation_end = max(train_end + 1, int(len(sessions) * 0.80))
    validation_end = min(validation_end, len(sessions) - 1)
    return {
        "train": (sessions[0], sessions[train_end - 1]),
        "validation": (sessions[train_end], sessions[validation_end - 1]),
        "test": (sessions[validation_end], sessions[-1]),
        "development": (sessions[0], sessions[validation_end - 1]),
        "all": (sessions[0], sessions[-1]),
    }


def _always_long_benchmark(frame: pd.DataFrame, start_session: str, end_session: str, initial_capital: float) -> dict[str, Any]:
    subset = frame[(frame["session_id"] >= start_session) & (frame["session_id"] <= end_session)]
    capital = float(initial_capital)
    returns: list[float] = []
    for _, group in subset.groupby("session_id", sort=True):
        if len(group) < 2:
            continue
        entry = float(group.iloc[0]["ask"]) * 1.0001
        exit_ = float(group.iloc[-1]["bid"]) * 0.9999
        ret = exit_ / entry - 1.0
        returns.append(ret)
        capital *= 1.0 + 0.35 * ret
    return {
        "initial_capital": round(initial_capital, 2),
        "final_equity": round(capital, 2),
        "total_return": round(capital / initial_capital - 1.0, 6),
        "sessions": len(returns),
        "positive_session_rate": round(sum(x > 0 for x in returns) / len(returns), 6) if returns else 0.0,
    }


def run_yfinance_proxy_backtest(*, initial_capital: float = 2000.0, persist: bool = True) -> dict[str, Any]:
    """Run a short local sanity backtest using the regular-session proxy."""
    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start = (datetime.now(timezone.utc) - timedelta(days=59)).strftime("%Y-%m-%d")
    raw, source = fetch_yfinance_proxy_history(start, end)
    base = prepare_bars(raw, DEFAULT_CONFIG, session_mode="regular_proxy")
    splits = _session_splits(base)
    candidates: list[dict[str, Any]] = []
    for cfg in _config_grid():
        result = simulate(raw, cfg, initial_capital=initial_capital, start_session=splits["development"][0], end_session=splits["development"][1], session_mode="regular_proxy")
        candidates.append({"config": cfg, "result": result, "selection_score": _selection_score(result)})
    candidates.sort(key=lambda x: x["selection_score"], reverse=True)
    selected: StrategyConfig = candidates[0]["config"]
    train = simulate(raw, selected, initial_capital=initial_capital, start_session=splits["train"][0], end_session=splits["train"][1], session_mode="regular_proxy")
    validation = simulate(raw, selected, initial_capital=initial_capital, start_session=splits["validation"][0], end_session=splits["validation"][1], session_mode="regular_proxy")
    test = simulate(raw, selected, initial_capital=initial_capital, start_session=splits["test"][0], end_session=splits["test"][1], session_mode="regular_proxy")
    all_result = simulate(raw, selected, initial_capital=initial_capital, start_session=splits["all"][0], end_session=splits["all"][1], session_mode="regular_proxy")
    benchmark = _always_long_benchmark(base, splits["test"][0], splits["test"][1], initial_capital)
    payload = {
        "run_id": f"soxl_proxy_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}",
        "generated_at": _now_iso(),
        "mode": "yfinance_regular_proxy",
        "symbol": SYMBOL,
        "dataset": "YFINANCE",
        "schema": "5m_ohlcv_synthetic_bbo",
        "source": source,
        "initial_capital": initial_capital,
        "selected_config": asdict(selected),
        "splits": splits,
        "train": train,
        "validation": validation,
        "test": test,
        "all": all_result,
        "test_always_long_benchmark": benchmark,
        "test_excess_vs_always_long": round(float(test["total_return"]) - float(benchmark["total_return"]), 6),
        "candidate_count": len(candidates),
        "selection_note": "Parameters selected on the development interval; test interval was not used for selection. This is a regular-session proxy, not overnight BBO.",
        "limitations": [
            "This proxy uses yfinance 5-minute OHLCV and a synthetic BBO, not Databento Blue Ocean overnight quotes.",
            "The Yahoo 5-minute history window is limited and does not represent a full market cycle.",
            "Short fills assume SOXL borrow is available; real locate/SSR/account constraints are not modeled.",
            "Use Databento mode before interpreting results for the real-time overnight strategy.",
        ],
    }
    if persist:
        persist_backtest(payload)
    return payload


def run_research_backtest(
    *,
    start: str = "2025-08-24",
    end: str | None = None,
    initial_capital: float = 2000.0,
    force_download: bool = False,
    persist: bool = True,
    source: str | None = None,
) -> dict[str, Any]:
    """Tune on development data once, then report untouched holdout results."""
    requested_source = (source or os.environ.get("SOXL_BACKTEST_SOURCE", "databento")).strip().lower()
    if requested_source in {"yfinance", "yfinance_proxy", "proxy"}:
        return run_yfinance_proxy_backtest(initial_capital=initial_capital, persist=persist)
    end = end or (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    raw, source = fetch_history(start, end, force=force_download)
    base = prepare_bars(raw, DEFAULT_CONFIG)
    splits = _session_splits(base)

    candidates: list[dict[str, Any]] = []
    for cfg in _config_grid():
        result = simulate(
            raw,
            cfg,
            initial_capital=initial_capital,
            start_session=splits["development"][0],
            end_session=splits["development"][1],
        )
        candidates.append({"config": cfg, "result": result, "selection_score": _selection_score(result)})
    candidates.sort(key=lambda x: x["selection_score"], reverse=True)
    selected: StrategyConfig = candidates[0]["config"]

    train = simulate(raw, selected, initial_capital=initial_capital, start_session=splits["train"][0], end_session=splits["train"][1])
    validation = simulate(raw, selected, initial_capital=initial_capital, start_session=splits["validation"][0], end_session=splits["validation"][1])
    test = simulate(raw, selected, initial_capital=initial_capital, start_session=splits["test"][0], end_session=splits["test"][1])
    all_result = simulate(raw, selected, initial_capital=initial_capital, start_session=splits["all"][0], end_session=splits["all"][1])
    benchmark = _always_long_benchmark(base, splits["test"][0], splits["test"][1], initial_capital)

    run_id = f"soxl_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    payload = {
        "run_id": run_id,
        "generated_at": _now_iso(),
        "mode": "historical_holdout",
        "symbol": SYMBOL,
        "dataset": DATASET,
        "schema": SCHEMA,
        "source": source,
        "initial_capital": initial_capital,
        "selected_config": asdict(selected),
        "splits": splits,
        "train": train,
        "validation": validation,
        "test": test,
        "all": all_result,
        "test_always_long_benchmark": benchmark,
        "test_excess_vs_always_long": round(float(test["total_return"]) - float(benchmark["total_return"]), 6),
        "candidate_count": len(candidates),
        "selection_note": "Parameters selected on train+validation only; test interval was not used for selection.",
        "limitations": [
            "Blue Ocean ATS only covers 20:00-04:00 ET, not the regular US session.",
            "Short fills assume SOXL borrow is available; real locate/SSR/account constraints are not modeled.",
            "BBO-1m bars cannot model queue position or sub-minute price path.",
            "Historical coverage starts 2025-08-24, so the holdout spans less than a full market cycle.",
        ],
    }
    if persist:
        persist_backtest(payload)
    return payload


def _ensure_tables() -> None:
    app_database.ensure_database()
    with app_database.connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS soxl_quant_runs (
                run_id TEXT PRIMARY KEY,
                generated_at TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_soxl_quant_runs_generated
            ON soxl_quant_runs(generated_at DESC);

            CREATE TABLE IF NOT EXISTS soxl_paper_trades (
                trade_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                side TEXT NOT NULL,
                qty INTEGER NOT NULL,
                entry_time TEXT,
                exit_time TEXT,
                entry_price REAL,
                exit_price REAL,
                pnl REAL,
                return_pct REAL,
                exit_reason TEXT,
                entry_score REAL,
                holding_bars INTEGER,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_soxl_paper_trades_exit
            ON soxl_paper_trades(exit_time DESC);
            """
        )
        conn.commit()


def persist_backtest(payload: dict[str, Any]) -> None:
    _ensure_tables()
    run_id = str(payload["run_id"])
    stamp = _now_iso()
    with app_database.connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO soxl_quant_runs(run_id, generated_at, mode, status, payload_json) VALUES (?, ?, ?, ?, ?)",
            (run_id, str(payload.get("generated_at") or stamp), str(payload.get("mode") or "historical_holdout"), "completed", json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
        )
        for trade in payload.get("test", {}).get("trades", []):
            trade_id = hashlib.sha1(f"{run_id}|{trade.get('entry_time')}|{trade.get('exit_time')}|{trade.get('side')}".encode("utf-8")).hexdigest()
            conn.execute(
                """
                INSERT OR REPLACE INTO soxl_paper_trades(
                    trade_id, run_id, side, qty, entry_time, exit_time, entry_price,
                    exit_price, pnl, return_pct, exit_reason, entry_score,
                    holding_bars, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id,
                    run_id,
                    trade.get("side"),
                    int(trade.get("qty") or 0),
                    trade.get("entry_time"),
                    trade.get("exit_time"),
                    trade.get("entry_price"),
                    trade.get("exit_price"),
                    trade.get("pnl"),
                    trade.get("return_pct"),
                    trade.get("exit_reason"),
                    trade.get("entry_score"),
                    trade.get("holding_bars"),
                    json.dumps(trade, ensure_ascii=False, separators=(",", ":")),
                    stamp,
                ),
            )
        conn.commit()

    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    (RESULT_DIR / f"{run_id}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def latest_backtest(*, include_curve: bool = True, trade_limit: int = 100) -> dict[str, Any] | None:
    _ensure_tables()
    with app_database.connection() as conn:
        row = conn.execute("SELECT payload_json FROM soxl_quant_runs WHERE status='completed' ORDER BY generated_at DESC LIMIT 1").fetchone()
    if not row:
        return None
    payload = json.loads(row["payload_json"])
    for key in ("train", "validation", "test", "all"):
        block = payload.get(key)
        if not isinstance(block, dict):
            continue
        block["trades"] = list(block.get("trades") or [])[-max(0, trade_limit):]
        if not include_curve:
            block.pop("equity_curve", None)
    return payload


def list_paper_trades(limit: int = 100) -> list[dict[str, Any]]:
    _ensure_tables()
    with app_database.connection() as conn:
        rows = conn.execute(
            "SELECT payload_json FROM soxl_paper_trades ORDER BY exit_time DESC LIMIT ?",
            (max(1, min(int(limit), 1000)),),
        ).fetchall()
    return [json.loads(row["payload_json"]) for row in rows]


_JOB_LOCK = threading.RLock()
_JOB: dict[str, Any] = {"status": "idle", "progress": 0, "message": "尚未运行"}


def backtest_job_status() -> dict[str, Any]:
    with _JOB_LOCK:
        return dict(_JOB)


def start_backtest_job(*, force_download: bool = False, initial_capital: float = 2000.0, source: str = "databento") -> dict[str, Any]:
    with _JOB_LOCK:
        if _JOB.get("status") == "running":
            return dict(_JOB)
        job_id = uuid.uuid4().hex
        _JOB.clear()
        _JOB.update({"job_id": job_id, "status": "running", "progress": 5, "message": "正在读取 SOXL 隔夜分钟行情", "started_at": _now_iso()})

    def worker() -> None:
        try:
            with _JOB_LOCK:
                _JOB.update({"progress": 20, "message": "正在执行开发区间参数筛选"})
            payload = run_research_backtest(force_download=force_download, initial_capital=initial_capital, persist=True, source=source)
            with _JOB_LOCK:
                _JOB.update({"status": "completed", "progress": 100, "message": "历史回放与未见区间验证完成", "finished_at": _now_iso(), "run_id": payload["run_id"], "result": payload})
        except Exception as exc:
            with _JOB_LOCK:
                _JOB.update({"status": "error", "progress": 100, "message": str(exc), "finished_at": _now_iso()})

    threading.Thread(target=worker, daemon=True, name="soxl-backtest").start()
    return backtest_job_status()


class SoxlLivePaperEngine:
    """Databento live observer. It records state but never sends broker orders."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._client: Any = None
        self._state: dict[str, Any] = {
            "status": "stopped",
            "symbol": SYMBOL,
            "mode": "paper",
            "broker_execution": False,
            "message": "实时模拟尚未启动",
            "bars_received": 0,
            "initial_capital": float(os.environ.get("SOXL_PAPER_INITIAL_CAPITAL", "2000")),
            "cash": float(os.environ.get("SOXL_PAPER_INITIAL_CAPITAL", "2000")),
            "equity": float(os.environ.get("SOXL_PAPER_INITIAL_CAPITAL", "2000")),
            "position": None,
            "realized_pnl": 0.0,
            "trade_count": 0,
            "data_source_configured": _databento_ready(),
        }
        self._bars: list[dict[str, Any]] = []
        self._raw_bars: list[dict[str, Any]] = []
        self._pending_entry = 0
        self._pending_exit: str | None = None
        self._position: dict[str, Any] | None = None
        self._run_id = f"soxl_live_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
            state["recent_bars"] = list(self._bars[-120:])
            return state

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._state.get("status") in {"connecting", "running"}:
                return self.status()
            if not _databento_ready():
                self._state.update({
                    "status": "error",
                    "message": "未启动：尚未配置 Databento 行情凭证",
                    "error": "DATABENTO_API_KEY is not configured; paper simulation stays disabled",
                    "data_source_configured": False,
                    "last_update": _now_iso(),
                })
                return self.status()
            self._state.update({"status": "connecting", "message": "正在连接 Databento Blue Ocean 实时流", "started_at": _now_iso(), "error": None})
            self._thread = threading.Thread(target=self._run, daemon=True, name="soxl-live-paper")
            self._thread.start()
        return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            client = self._client
            self._state.update({"status": "stopping", "message": "正在停止实时模拟"})
        if client is not None:
            try:
                client.stop()
            except Exception:
                pass
        with self._lock:
            self._state.update({"status": "stopped", "message": "实时模拟已停止", "stopped_at": _now_iso()})
        return self.status()

    def _on_record(self, record: Any) -> None:
        name = type(record).__name__
        if "Bbo" not in name and "Cmbp" not in name:
            return
        ts_ns = int(getattr(record, "ts_event", 0) or getattr(record, "ts_recv", 0) or 0)
        bid = _safe_float(getattr(record, "bid_px_00", None))
        ask = _safe_float(getattr(record, "ask_px_00", None))
        if bid is None or ask is None:
            return
        if ask > 1_000_000:
            bid /= 1e9
            ask /= 1e9
        bar = {
            "timestamp": datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).isoformat() if ts_ns else _now_iso(),
            "bid": round(bid, 4),
            "ask": round(ask, 4),
            "mid": round((bid + ask) / 2.0, 4),
            "spread_bps": round((ask - bid) / ((bid + ask) / 2.0) * 10000.0, 2) if bid + ask > 0 else None,
        }
        raw_bar = {
            "timestamp": bar["timestamp"],
            "bid_px_00": bid,
            "ask_px_00": ask,
            "bid_sz_00": float(getattr(record, "bid_sz_00", 0) or 0),
            "ask_sz_00": float(getattr(record, "ask_sz_00", 0) or 0),
        }
        with self._lock:
            self._bars.append(bar)
            self._bars = self._bars[-600:]
            self._raw_bars.append(raw_bar)
            self._raw_bars = self._raw_bars[-600:]
            self._advance_paper(raw_bar)
            self._state.update({"status": "running", "message": "SOXL 实时模拟行情已连接", "bars_received": int(self._state.get("bars_received") or 0) + 1, "last_bar": bar, "last_update": _now_iso()})

    def _active_config(self) -> StrategyConfig:
        try:
            latest = latest_backtest(include_curve=False, trade_limit=0)
            if latest and latest.get("mode") != "historical_holdout":
                return DEFAULT_CONFIG
            values = latest.get("selected_config") if latest else None
            if isinstance(values, dict):
                return StrategyConfig(**{k: values[k] for k in asdict(DEFAULT_CONFIG) if k in values})
        except Exception:
            pass
        return DEFAULT_CONFIG

    def _advance_paper(self, raw_bar: dict[str, Any]) -> None:
        """Advance the paper account by one completed BBO minute."""
        if len(self._raw_bars) < 20:
            return
        config = self._active_config()
        raw = pd.DataFrame(self._raw_bars)
        raw.index = pd.to_datetime(raw.pop("timestamp"), utc=True)
        frame = prepare_bars(raw, config)
        if frame.empty:
            return
        ts = frame.index[-1]
        row = frame.iloc[-1]
        cash = float(self._state.get("cash") or 0.0)

        if self._position and self._pending_exit:
            pos = self._position
            fill = _fill_price(int(pos["side"]), "exit", row, config.slippage_bps)
            pnl = int(pos["side"]) * int(pos["qty"]) * (fill - float(pos["entry_price"]))
            cash += pnl
            trade = _trade_payload(int(pos["side"]), int(pos["qty"]), pd.Timestamp(pos["entry_time"]), ts, float(pos["entry_price"]), fill, pnl, self._pending_exit, float(pos["entry_score"]), int(pos["holding_bars"]))
            self._persist_live_trade(trade)
            self._position = None
            self._pending_exit = None

        if self._position is None and self._pending_entry:
            fill = _fill_price(self._pending_entry, "entry", row, config.slippage_bps)
            stop_pct = float(row.get("stop_distance_pct") or config.min_stop_pct)
            risk_per_share = max(fill * stop_pct, fill * config.min_stop_pct)
            qty = min(
                int(cash * config.risk_per_trade / risk_per_share) if risk_per_share > 0 else 0,
                int(cash * config.max_position_ratio / fill) if fill > 0 else 0,
            )
            if qty > 0:
                self._position = {
                    "side": self._pending_entry,
                    "qty": qty,
                    "entry_price": fill,
                    "entry_time": ts.isoformat(),
                    "entry_score": float(row.get("signal_score") or 0.0),
                    "holding_bars": 0,
                    "stop_price": fill - self._pending_entry * risk_per_share,
                    "target_price": fill + self._pending_entry * risk_per_share * config.take_profit_r,
                }
            self._pending_entry = 0

        if self._position:
            pos = self._position
            pos["holding_bars"] = int(pos["holding_bars"]) + 1
            side = int(pos["side"])
            mid = float(row["mid"])
            score = float(row.get("signal_score") or 0.0)
            if (side > 0 and mid <= float(pos["stop_price"])) or (side < 0 and mid >= float(pos["stop_price"])):
                self._pending_exit = "stop"
            elif (side > 0 and mid >= float(pos["target_price"])) or (side < 0 and mid <= float(pos["target_price"])):
                self._pending_exit = "target"
            elif (side > 0 and score < -config.min_signal_flip) or (side < 0 and score > config.min_signal_flip):
                self._pending_exit = "signal_flip"
            elif int(pos["holding_bars"]) >= config.max_holding_bars:
                self._pending_exit = "time"
            elif int(row["bars_to_end"]) <= config.flatten_before_end_bars:
                self._pending_exit = "session_end"
        else:
            score = _safe_float(row.get("signal_score"))
            spread = _safe_float(row.get("spread_bps"))
            eligible = (
                score is not None
                and spread is not None
                and int(row["session_bar"]) >= config.min_session_bar
                and int(row["bars_to_end"]) > config.flatten_before_end_bars + 1
                and spread <= config.max_spread_bps
                and pd.notna(row.get("stop_distance_pct"))
            )
            if eligible and score >= config.signal_threshold:
                self._pending_entry = 1
            elif eligible and score <= -config.signal_threshold:
                self._pending_entry = -1

        equity = cash
        if self._position:
            equity += int(self._position["side"]) * int(self._position["qty"]) * (float(row["mid"]) - float(self._position["entry_price"]))
        self._state.update({
            "cash": round(cash, 2),
            "equity": round(equity, 2),
            "realized_pnl": round(cash - float(self._state["initial_capital"]), 2),
            "position": dict(self._position) if self._position else None,
            "pending_action": "EXIT" if self._pending_exit else ("LONG" if self._pending_entry > 0 else "SHORT" if self._pending_entry < 0 else None),
            "signal_score": round(float(row.get("signal_score") or 0.0), 4),
            "signal": "LONG" if float(row.get("signal_score") or 0.0) >= config.signal_threshold else "SHORT" if float(row.get("signal_score") or 0.0) <= -config.signal_threshold else "FLAT",
            "config": asdict(config),
        })

    def _persist_live_trade(self, trade: dict[str, Any]) -> None:
        _ensure_tables()
        trade_id = hashlib.sha1(f"{self._run_id}|{trade.get('entry_time')}|{trade.get('exit_time')}|{trade.get('side')}".encode("utf-8")).hexdigest()
        with app_database.connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO soxl_paper_trades(
                    trade_id, run_id, side, qty, entry_time, exit_time, entry_price,
                    exit_price, pnl, return_pct, exit_reason, entry_score,
                    holding_bars, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (trade_id, self._run_id, trade["side"], trade["qty"], trade["entry_time"], trade["exit_time"], trade["entry_price"], trade["exit_price"], trade["pnl"], trade["return_pct"], trade["exit_reason"], trade["entry_score"], trade["holding_bars"], json.dumps(trade, ensure_ascii=False, separators=(",", ":")), _now_iso()),
            )
            conn.commit()
        self._state["trade_count"] = int(self._state.get("trade_count") or 0) + 1

    def _on_exception(self, exc: Exception) -> None:
        with self._lock:
            self._state.update({"status": "error", "message": "Databento 实时流不可用，历史回放仍可使用", "error": str(exc)[:300], "last_update": _now_iso()})

    def _run(self) -> None:
        try:
            import databento as db

            client = db.Live()
            with self._lock:
                self._client = client
            client.subscribe(dataset=DATASET, schema=SCHEMA, symbols=[SYMBOL], stype_in="raw_symbol")
            client.add_callback(self._on_record, self._on_exception)
            client.start()
            client.block_for_close()
        except Exception as exc:
            self._on_exception(exc)
        finally:
            with self._lock:
                self._client = None
                if self._state.get("status") not in {"error", "stopped"}:
                    self._state.update({"status": "stopped", "message": "Databento 实时流已关闭", "stopped_at": _now_iso()})


LIVE_ENGINE = SoxlLivePaperEngine()


def service_status() -> dict[str, Any]:
    latest = latest_backtest(include_curve=False, trade_limit=20)
    return {
        "symbol": SYMBOL,
        "allowed_symbols": [SYMBOL],
        "paper_only": True,
        "broker_execution": False,
        "dataset": DATASET,
        "schema": SCHEMA,
        "data_source_configured": _databento_ready(),
        "session": "20:00-04:00 America/New_York",
        "backtest_job": backtest_job_status(),
        "live": LIVE_ENGINE.status(),
        "latest_backtest": latest,
    }
