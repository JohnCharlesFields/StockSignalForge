"""Bounded GilData daily gap filling; existing bars and calibration stay intact."""
from __future__ import annotations

import math
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone

import pandas as pd

import gildata_shadow_service as gil
import market_data_service as market
from app_database import cache_get, cache_set, reference_evidence_append
from market_calendar import is_trading_day, most_recent_session, previous_trading_day

_PRICE_COLUMNS = {"Open": "开盘价(元)", "High": "最高价(元)", "Low": "最低价(元)", "Close": "收盘价(元)"}
SYNC_VERSION = 2


def enabled() -> bool:
    return os.getenv("GILDATA_DAILY_PRIMARY", "0") == "1" and bool(os.getenv("GILDATA_MCP_TOKEN", "").strip())


def _plan(symbol: str, session: str) -> dict:
    old = market._read_daily_cache(symbol)
    if old.empty:
        return {"status": "rejected", "reason": "history_anchor_missing_or_future"}
    if not isinstance(old.index, pd.DatetimeIndex) or old.index.hasnans or old.index.tz is not None:
        return {"status": "rejected", "reason": "invalid_history_index"}
    target = pd.Timestamp(session)
    before = old.loc[old.index < target]
    if target in old.index:
        return {"status": "cached"}
    if len(before) < 2 or (not old.empty and old.index.max() > target):
        return {"status": "rejected", "reason": "history_anchor_missing_or_future"}
    anchors = before.tail(2)
    if any(not is_trading_day(day.date()) for day in anchors.index):
        return {"status": "rejected", "reason": "non_session_anchor"}
    if anchors.index[0].date() != previous_trading_day(anchors.index[1].date()):
        return {"status": "rejected", "reason": "non_consecutive_anchors"}
    missing = []
    current = anchors.index[-1].date() + timedelta(days=1)
    while current <= target.date():
        if is_trading_day(current):
            missing.append(current.isoformat())
        current += timedelta(days=1)
    if len(missing) > 10:
        return {"status": "rejected", "reason": "history_gap_exceeds_10_sessions"}
    return {"status": "pending", "anchors": [day.date().isoformat() for day in anchors.index], "missing": missing}


def parse_quotes(results: list[dict], symbols: list[str], dates: set[str]) -> tuple[dict, dict]:
    aliases = {value: symbol for symbol in symbols for value in (symbol, symbol.replace("-", "."))}
    quotes, errors = {}, {}
    for raw in gil._table(results, "美股日行情"):
        symbol = aliases.get(str(raw.get("证券代码") or "").upper())
        day = raw.get("交易日期")
        if not symbol or day not in dates:
            continue
        key = (symbol, day)
        bar = {column: gil._number(raw.get(field)) for column, field in _PRICE_COLUMNS.items()}
        volume = gil._number(raw.get("成交量(万股)"))
        previous = gil._number(raw.get("昨收价(元)"))
        if (raw.get("币种") != "USD" or any(value is None or value <= 0 for value in bar.values())
                or volume is None or volume <= 0 or previous is None or previous <= 0):
            errors[key] = "invalid_currency_price_volume_or_previous_close"
            continue
        if not bar["Low"] <= min(bar["Open"], bar["Close"]) <= max(bar["Open"], bar["Close"]) <= bar["High"]:
            errors[key] = "invalid_ohlc_bounds"
            continue
        bar.update(Volume=volume * 10000, previous_close=previous, warnings=[])
        if not math.isfinite(bar["Volume"]):
            errors[key] = "invalid_scaled_volume"
            continue
        turnover = gil._number(raw.get("成交额(万元)"))
        if turnover is not None and not bar["Low"] * bar["Volume"] * .99 <= turnover * 10000 <= bar["High"] * bar["Volume"] * 1.01:
            bar["warnings"].append("turnover_inconsistent_not_used")
        if key in quotes and quotes[key] != bar:
            errors[key] = "conflicting_duplicate_bar"
        else:
            quotes[key] = bar
    return {key: bar for key, bar in quotes.items() if key not in errors}, errors


def validate_missing(symbol: str, plan: dict, quotes: dict, old: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    days = plan["anchors"] + plan["missing"]
    if any((symbol, day) not in quotes for day in days):
        raise ValueError("requested_session_or_overlap_missing")
    price_error, volume_error = 0., 0.
    for day in plan["anchors"]:
        if pd.Timestamp(day) not in old.index:
            raise ValueError("anchor_changed")
        bar, cached = quotes[symbol, day], old.loc[pd.Timestamp(day)]
        for column in _PRICE_COLUMNS:
            value = float(cached[column])
            if not math.isfinite(value) or value <= 0 or abs(bar[column] - value) > max(.02, value * .001):
                raise ValueError("overlap_price_basis_mismatch")
            price_error = max(price_error, abs(bar[column] / value - 1))
        volume = float(cached["Volume"])
        if not math.isfinite(volume) or volume <= 0 or abs(bar["Volume"] - volume) > max(100., volume * .02):
            raise ValueError("overlap_volume_unit_mismatch")
        volume_error = max(volume_error, abs(bar["Volume"] / volume - 1))
    previous_close = float(old.loc[pd.Timestamp(plan["anchors"][-1]), "Close"])
    rows = []
    warnings = []
    for day in plan["missing"]:
        bar = quotes[symbol, day]
        if abs(bar["previous_close"] - previous_close) > max(.02, previous_close * .001):
            raise ValueError("previous_close_basis_mismatch")
        if abs(bar["Close"] / previous_close - 1) > .20:
            raise ValueError("large_gap_requires_corporate_action_review")
        previous_close = bar["Close"]
        warnings.extend(bar["warnings"])
        if pd.Timestamp(day) in old.index:
            cached = old.loc[pd.Timestamp(day)]
            for column in (*_PRICE_COLUMNS, "Volume"):
                tolerance = max(100., bar[column] * .02) if column == "Volume" else max(.02, bar[column] * .001)
                if not math.isfinite(float(cached[column])) or abs(float(cached[column]) - bar[column]) > tolerance:
                    raise ValueError("concurrent_bar_mismatch")
        else:
            rows.append({"Date": day, **{column: bar[column] for column in (*_PRICE_COLUMNS, "Volume")}})
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame["Date"] = pd.to_datetime(frame["Date"])
        frame = frame.set_index("Date")
    return frame, {"overlap_dates": plan["anchors"], "overlap_price_error": price_error,
                   "overlap_volume_error": volume_error, "warnings": sorted(set(warnings)),
                   "adjustment": "recent_overlap_checked_not_historical_certification"}


def sync_daily(symbols: list[str], session: str, *, max_seconds: float = 150, batch_size: int = 20,
               workers: int = 2, execute: bool = True) -> dict:
    session = gil._date(session)
    if not is_trading_day(date.fromisoformat(session)) or session > most_recent_session().isoformat():
        raise ValueError("session_not_completed")
    if not enabled() or not market.external_data_allowed():
        return {"status": "disabled_or_cache_only", "symbols_written": 0, "sync_version": SYNC_VERSION}
    if not 1 <= batch_size <= 20 or not 1 <= workers <= 2 or not 0 < max_seconds <= 300:
        raise ValueError("invalid_bounded_sync_config")
    symbols = list(dict.fromkeys(gil._safe_symbol(symbol) for symbol in symbols))
    if len(symbols) > 5000:
        raise ValueError("daily_symbol_limit")
    started = time.monotonic()
    plans, rejected = {}, {}
    cached, cooled = 0, 0
    for symbol in symbols:
        try:
            plan = _plan(symbol, session)
        except Exception as exc:
            # A damaged local cache must not cancel unrelated provider batches.
            rejected[symbol] = f"cache_planning_{type(exc).__name__}"
            continue
        if plan["status"] == "cached":
            cached += 1
        elif plan["status"] != "pending":
            rejected[symbol] = plan.get("reason")
        elif execute and cache_get(f"gildata:daily_attempt:v1:{session}:{symbol}"):
            cooled += 1
        else:
            plans[symbol] = plan
    missing = list(plans)
    batches = [missing[index:index + batch_size] for index in range(0, len(missing), batch_size)]
    written, validated, fulfilled, added, calls = [], [], [], 0, 0
    circuit_open = False

    def request(batch):
        remaining = max_seconds - (time.monotonic() - started)
        if remaining < 10:
            return None
        days = {day for symbol in batch for day in plans[symbol]["anchors"] + plans[symbol]["missing"]}
        names = ",".join(symbol.replace("-", ".") for symbol in batch)
        response, _ = gil._query("FinQuery", (
            f"仅查询美股代码{names}从{min(days)}至{session}逐交易日的美元日行情："
            "证券代码、交易日期、币种、开盘价、最高价、最低价、收盘价、昨收价及成交量（标明单位）。"
            "使用实际未复权成交价格；不要区间统计、解释、其他股票或其他日期，缺失请留空。"
        ), read_timeout=min(35, int(remaining - 8)), max_attempts=1)
        return parse_quotes(response, batch, days)

    # Two queries at a time; no unbounded queue can outlive the process watchdog.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for offset in range(0, len(batches), workers):
            if circuit_open or time.monotonic() - started > max_seconds - 10:
                break
            futures = {pool.submit(request, batch): batch for batch in batches[offset:offset + workers]}
            for future in as_completed(futures):
                batch = futures[future]
                calls += 1
                try:
                    result = future.result()
                    if result is None:
                        continue
                    quotes, _ = result
                except Exception as exc:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    if status in (401, 403, 429):
                        circuit_open = True
                    for symbol in batch:
                        rejected[symbol] = type(exc).__name__
                        if execute:
                            cache_set(f"gildata:daily_attempt:v1:{session}:{symbol}", {"reason": type(exc).__name__}, ttl_seconds=600)
                    continue
                for symbol in batch:
                    try:
                        # Re-read before committing; existing dates always win.
                        old = market._read_daily_cache(symbol)
                        fresh, validation = validate_missing(symbol, plans[symbol], quotes, old)
                        validated.append(symbol)
                        if fresh.empty:
                            cached += 1
                            fulfilled.append(symbol)
                            continue
                        if not execute:
                            continue
                        evidence = {"source": "gildata:FinQuery:daily", "as_of": session,
                                    "fetched_at": datetime.now(timezone.utc).isoformat(), "validation": validation,
                                    "bars": fresh.reset_index().astype({"Date": str}).to_dict("records"), "status": "validated"}
                        reference_evidence_append(symbol, "daily_ohlcv", session, evidence)
                        market._write_daily_cache(symbol, market._merge_daily(old, fresh))
                        check = market._read_daily_cache(symbol)
                        if (not fresh.index.isin(check.index).all()
                                or not all(math.isclose(float(check.loc[day, column]), float(fresh.loc[day, column]), rel_tol=1e-9, abs_tol=1e-8)
                                           for day in fresh.index for column in (*_PRICE_COLUMNS, "Volume"))):
                            raise ValueError("cache_commit_verification_failed")
                        written.append(symbol)
                        added += len(fresh)
                        reference_evidence_append(symbol, "daily_ohlcv", session, {**evidence, "status": "written"})
                    except Exception as exc:
                        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                        rejected[symbol] = reason
                        if execute:
                            cache_set(f"gildata:daily_attempt:v1:{session}:{symbol}", {"reason": reason}, ttl_seconds=600)
    report = {"source": "gildata:FinQuery:daily", "session": session, "executed": execute, "sync_version": SYNC_VERSION,
              "status": "completed" if len(written) + cached == len(symbols) else "partial",
              "symbols_requested": len(symbols), "symbols_cached": cached, "symbols_written": len(written),
              "records_added": added, "written_symbols": written, "validated_symbols": validated,
              "rejected_count": len(rejected), "rejected": rejected, "cooldown_count": cooled,
              "rejection_reasons": dict(Counter(rejected.values())),
              "pending_count": len(set(missing) - set(written) - set(fulfilled) - set(rejected)), "calls": calls,
              "circuit_open": circuit_open,
              "elapsed_seconds": round(time.monotonic() - started, 2)}
    if execute:
        cache_set("gildata:daily_last_status", report)
    return report
