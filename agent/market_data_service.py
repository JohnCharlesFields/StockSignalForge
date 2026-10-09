"""Shared US-equity market-data routing with persistent cache and free-tier guards."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd
import requests
import yfinance as yf

_CACHE_ROOT = Path(os.environ.get("VIBE_MARKET_DATA_CACHE_DIR", "/app/agent/data_cache/market_data"))
_TWELVE_BASE = "https://api.twelvedata.com"
_FMP_BASE = "https://financialmodelingprep.com/stable"
_TIINGO_BASE = "https://api.tiingo.com"
_MASSIVE_BASE = "https://api.polygon.io"
_lock = threading.Lock()
_EXTERNAL_SCOPE = threading.local()

# US Eastern Time zone (ET).  The market closes at 16:00 ET.
# Use zoneinfo for true DST-aware Eastern Time (EST/EDT).
# Falls back to fixed EST (UTC-5) on platforms without IANA tzdata.
try:
    from zoneinfo import ZoneInfo
    _US_EASTERN = ZoneInfo("America/New_York")
except Exception:
    _US_EASTERN = timezone(timedelta(hours=-5), "EST")

# Weekdays: Monday=0 … Sunday=6.  US equity markets are closed Saturday & Sunday.
_US_MARKET_CLOSE_HOUR = 16
_US_MARKET_CLOSE_MINUTE = 0


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _safe_symbol(symbol: str) -> str:
    return str(symbol).strip().upper().replace(".", "_").replace("/", "_")


def external_data_allowed() -> bool:
    """Whether the current thread may call external market/news providers.

    Default is allowed so batch jobs and existing scripts keep their historical
    behavior. Page-read endpoints enter ``external_data_scope(False)`` so they
    only consume persisted database/cache data unless explicitly refreshed.
    """
    return bool(getattr(_EXTERNAL_SCOPE, "allowed", True))


@contextmanager
def external_data_scope(allowed: bool):
    previous = getattr(_EXTERNAL_SCOPE, "allowed", True)
    _EXTERNAL_SCOPE.allowed = bool(allowed)
    try:
        yield
    finally:
        _EXTERNAL_SCOPE.allowed = previous


def _cache_path(kind: str, symbol: str, suffix: str = "json") -> Path:
    path = _CACHE_ROOT / kind / f"{_safe_symbol(symbol)}.{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _fresh(path: Path, ttl_seconds: int) -> bool:
    return path.exists() and time.time() - path.stat().st_mtime < ttl_seconds


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError):
        return None


def _write_json(path: Path, payload: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _claim(provider: str, per_minute: int, per_day: int) -> bool:
    """Claim one guarded request slot without ever blocking a page request."""
    now = datetime.now(timezone.utc)
    path = _CACHE_ROOT / "_quota" / f"{provider}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        payload = _read_json(path) or {}
        minute = now.strftime("%Y-%m-%dT%H:%M")
        day = now.strftime("%Y-%m-%d")
        minute_count = int(payload.get("minute_count", 0)) if payload.get("minute") == minute else 0
        day_count = int(payload.get("day_count", 0)) if payload.get("day") == day else 0
        if minute_count >= per_minute or day_count >= per_day:
            return False
        _write_json(path, {
            "minute": minute,
            "minute_count": minute_count + 1,
            "day": day,
            "day_count": day_count + 1,
        })
    return True


def _drop_price_spikes(out: pd.DataFrame) -> pd.DataFrame:
    """Drop isolated bad-print bars (e.g. a 10x fat-finger that reverts next day).

    A bar whose Close is >2.5x (or <1/2.5x) BOTH neighbours is almost certainly a
    corrupt feed print, not a real move. One such bar otherwise blows up ATR,
    volume profile, pullback scores and calibration for that symbol. Endpoints
    (first/last bar) are never dropped (NaN neighbour -> not flagged)."""
    if len(out) < 3 or "Close" not in out:
        return out
    c = pd.to_numeric(out["Close"], errors="coerce")
    prev_c, next_c = c.shift(1), c.shift(-1)
    up = (prev_c > 0) & (next_c > 0) & (c > 2.5 * prev_c) & (c > 2.5 * next_c)
    dn = (prev_c > 0) & (next_c > 0) & (c * 2.5 < prev_c) & (c * 2.5 < next_c)
    bad = (up | dn).fillna(False)
    return out[~bad] if bool(bad.any()) else out


def _normalize_daily(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    result = frame.copy()
    result.index = pd.DatetimeIndex(pd.to_datetime(result.index)).tz_localize(None)
    result.index.name = "Date"
    for column in ("Open", "High", "Low", "Close", "Volume"):
        if column not in result:
            result[column] = 0.0 if column == "Volume" else float("nan")
        result[column] = pd.to_numeric(result[column], errors="coerce")
    out = result[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"]).sort_index()
    # Normalize to the calendar date (drop intraday time) so the SAME session
    # from different sources (grouped-daily date vs Massive aggs ms-timestamp)
    # collapses to one index and de-dups -- otherwise duplicate bars slip past
    # _merge_daily and corrupt day-change / HV / forward calcs.
    out.index = pd.DatetimeIndex(out.index).normalize()
    out.index.name = "Date"
    return _drop_price_spikes(out[~out.index.duplicated(keep="last")])


def _read_daily_cache(symbol: str) -> pd.DataFrame:
    path = _cache_path("ohlcv_daily", symbol, "csv")
    try:
        return _normalize_daily(pd.read_csv(path, index_col="Date", parse_dates=["Date"]))
    except (OSError, ValueError, KeyError):
        return pd.DataFrame()


def _write_daily_cache(symbol: str, frame: pd.DataFrame) -> None:
    if frame.empty:
        return
    path = _cache_path("ohlcv_daily", symbol, "csv")
    temp = path.with_suffix(".tmp")
    _normalize_daily(frame).to_csv(temp)
    temp.replace(path)


def _period_start(period: str) -> datetime:
    now = datetime.now(timezone.utc)
    value = str(period or "6mo").strip().lower()
    if value.endswith("y"):
        return now - timedelta(days=366 * int(value[:-1] or 1))
    if value.endswith("mo"):
        return now - timedelta(days=31 * int(value[:-2] or 1))
    if value.endswith("d"):
        return now - timedelta(days=int(value[:-1] or 1))
    return now - timedelta(days=186)


def _twelve_daily(symbol: str, start: datetime) -> pd.DataFrame:
    api_key = os.environ.get("TWELVE_DATA_API_KEY", "").strip()
    if not api_key or not _claim("twelvedata", _env_int("TWELVE_DATA_PER_MINUTE", 6), _env_int("TWELVE_DATA_PER_DAY", 700)):
        return pd.DataFrame()
    response = requests.get(
        f"{_TWELVE_BASE}/time_series",
        params={
            "symbol": symbol,
            "interval": "1day",
            "start_date": start.strftime("%Y-%m-%d"),
            "outputsize": 5000,
            "order": "asc",
            "apikey": api_key,
        },
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    values = payload.get("values") or []
    if not values:
        return pd.DataFrame()
    frame = pd.DataFrame(values)
    return _normalize_daily(pd.DataFrame({
        "Open": frame["open"].to_numpy(),
        "High": frame["high"].to_numpy(),
        "Low": frame["low"].to_numpy(),
        "Close": frame["close"].to_numpy(),
        "Volume": frame["volume"].to_numpy() if "volume" in frame else 0,
    }, index=pd.to_datetime(frame["datetime"])))


def _tiingo_daily(symbol: str, start: datetime) -> pd.DataFrame:
    """Daily OHLCV from Tiingo (primary per-symbol source; reliable + deep).

    Uses RAW open/high/low/close/volume to stay consistent with the rest of the
    cache (yfinance auto_adjust=False / Twelve Data raw). Tiingo is reachable
    directly (no proxy) and gives clean, split/dividend-aware data. Free tier:
    1000 req/day, 50 unique symbols/hour, 500 unique symbols/month -- enough for
    per-symbol live paths (holdings, watched names, benchmark), NOT bulk universe.
    """
    api_key = os.environ.get("TIINGO_API_KEY", "").strip()
    if not api_key or not _claim("tiingo", _env_int("TIINGO_PER_MINUTE", 20), _env_int("TIINGO_PER_DAY", 900)):
        return pd.DataFrame()
    response = requests.get(
        f"{_TIINGO_BASE}/tiingo/daily/{symbol}/prices",
        params={"startDate": start.strftime("%Y-%m-%d"), "resampleFreq": "daily", "token": api_key},
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    response.raise_for_status()
    values = response.json()
    if not isinstance(values, list) or not values:
        return pd.DataFrame()
    frame = pd.DataFrame(values)
    return _normalize_daily(pd.DataFrame({
        "Open": pd.to_numeric(frame.get("open"), errors="coerce").to_numpy(),
        "High": pd.to_numeric(frame.get("high"), errors="coerce").to_numpy(),
        "Low": pd.to_numeric(frame.get("low"), errors="coerce").to_numpy(),
        "Close": pd.to_numeric(frame.get("close"), errors="coerce").to_numpy(),
        "Volume": pd.to_numeric(frame.get("volume"), errors="coerce").to_numpy() if "volume" in frame else 0,
    }, index=pd.to_datetime(frame["date"]).dt.tz_localize(None)))


def _massive_get(path: str, **params: Any) -> Any:
    """Raw GET against Massive/Polygon with the shared rate-limit gate.

    Polygon/Massive PAID plans (Starter $29+) allow UNLIMITED API calls; the
    5/min cap is only the FREE tier. We self-throttle at 200/min by default to
    stay polite + survive transient 429s, far above the old free-tier 5/min that
    made bulk backfills crawl. Override via MASSIVE_PER_MINUTE if a 429 appears.
    """
    if not external_data_allowed():
        return None
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if not api_key:
        return None
    params["apiKey"] = api_key
    # Lightweight retry/backoff so a transient proxy/network blip (or a 429/5xx)
    # auto-recovers instead of surfacing "行情源暂不可用" to the page. Only the
    # HTTP call is retried (quota already claimed once); deterministic 4xx (except
    # 429) fail fast. Worst-case added latency ~1.8s, only on the failure path.
    attempts = _env_int("MASSIVE_RETRIES", 2) + 1
    last_exc: Exception | None = None
    for i in range(attempts):
        if not _claim("massive", _env_int("MASSIVE_PER_MINUTE", 200), _env_int("MASSIVE_PER_DAY", 50000)):
            return None
        try:
            r = requests.get(f"{_MASSIVE_BASE}{path}", params=params, timeout=20)
            if r.status_code in (429, 500, 502, 503, 504) and i < attempts - 1:
                delay = 0.6 * (2 ** i)
                if r.status_code == 429:
                    delay = max(15.0, delay)
                    retry_after = r.headers.get("Retry-After")
                    if retry_after:
                        try:
                            delay = max(delay, float(retry_after))
                        except ValueError:
                            try:
                                delay = max(delay, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
                            except (TypeError, ValueError):
                                pass
                    # Long provider pauses go to the daily fallback instead of
                    # blocking a worker or retrying before the allowed time.
                    if delay > 60:
                        r.raise_for_status()
                time.sleep(delay)
                continue
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc
            if i < attempts - 1:
                time.sleep(0.6 * (2 ** i))
                continue
            raise
    if last_exc is not None:
        raise last_exc
    return None


def _massive_aggs(symbol: str, start: datetime, adjusted: bool = True) -> pd.DataFrame:
    """Per-symbol daily OHLCV from Massive/Polygon aggregates.

    ``adjusted=False`` returns RAW (as-traded) prices to match the project's
    OHLCV cache convention (yfinance auto_adjust=False / Tiingo raw) when used as
    the primary price source. ``adjusted=True`` (default) for research where
    split/dividend-clean series are preferable.
    """
    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        payload = _massive_get(
            f"/v2/aggs/ticker/{symbol}/range/1/day/{start.strftime('%Y-%m-%d')}/{end}",
            adjusted="true" if adjusted else "false", sort="asc", limit=50000,
        )
    except Exception:
        return pd.DataFrame()
    results = (payload or {}).get("results") or []
    if not results:
        return pd.DataFrame()
    frame = pd.DataFrame(results)
    return _normalize_daily(pd.DataFrame({
        "Open": pd.to_numeric(frame.get("o"), errors="coerce").to_numpy(),
        "High": pd.to_numeric(frame.get("h"), errors="coerce").to_numpy(),
        "Low": pd.to_numeric(frame.get("l"), errors="coerce").to_numpy(),
        "Close": pd.to_numeric(frame.get("c"), errors="coerce").to_numpy(),
        "Volume": pd.to_numeric(frame.get("v"), errors="coerce").to_numpy(),
    }, index=pd.to_datetime(frame["t"], unit="ms")))


def massive_grouped_daily(date_str: str) -> dict[str, dict[str, float]]:
    """Whole-US-market OHLCV for one trading day in a SINGLE call (the key bulk win).

    Returns ``{TICKER: {open, high, low, close, volume}}``.  Empty dict on a
    holiday/weekend or if no key.  One Massive call covers ~12k tickers.
    """
    try:
        # Daily cache is raw OHLCV. Bulk and per-symbol requests must agree.
        payload = _massive_get(f"/v2/aggs/grouped/locale/us/market/stocks/{date_str}", adjusted="false")
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        reason = f"HTTP {status}" if status else type(exc).__name__
        raise RuntimeError(f"Massive grouped daily {date_str} unavailable: {reason}") from exc
    if payload is None:
        raise RuntimeError(f"Massive grouped daily {date_str} unavailable: missing key or local quota")
    out: dict[str, dict[str, float]] = {}
    for row in (payload or {}).get("results") or []:
        t = row.get("T")
        if not t:
            continue
        out[str(t).upper()] = {
            "open": float(row.get("o", 0) or 0), "high": float(row.get("h", 0) or 0),
            "low": float(row.get("l", 0) or 0), "close": float(row.get("c", 0) or 0),
            "volume": float(row.get("v", 0) or 0),
        }
    return out


def get_massive_options_snapshot(symbol: str, current_price: float | None = None) -> dict[str, Any]:
    """Current option-chain snapshot from Massive/Polygon.

    This is the short-term volatility source: current ATM IV, Greeks, OI and
    call/put ratios. It is not historical IV rank/percentile; those require
    persisting the daily ATM IV series.
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        return {"available": False, "source": "massive", "reason": "missing_symbol"}
    try:
        payload = _massive_get(
            f"/v3/snapshot/options/{sym}",
            limit=250,
            sort="expiration_date",
            order="asc",
        )
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 403:
            reason = "forbidden_or_plan_missing"
        elif status == 401:
            reason = "unauthorized"
        elif status:
            reason = f"http_{status}"
        else:
            reason = exc.__class__.__name__
        return {
            "available": False,
            "source": "massive",
            "reason": reason,
            "status_code": status,
            "note": "Massive option snapshot failed; check Options plan entitlement or API key.",
        }
    results = (payload or {}).get("results") or []
    if not isinstance(results, list) or not results:
        return {"available": False, "source": "massive", "reason": "empty_option_snapshot"}
    today = date.today()
    rows: list[dict[str, Any]] = []
    for item in results:
        details = item.get("details") or {}
        greeks = item.get("greeks") or {}
        day = item.get("day") or {}
        quote = item.get("last_quote") or item.get("latest_quote") or {}
        trade = item.get("last_trade") or item.get("latest_trade") or {}
        exp_raw = details.get("expiration_date")
        try:
            exp = date.fromisoformat(str(exp_raw)[:10])
        except (TypeError, ValueError):
            continue
        dte = (exp - today).days
        if dte < 1:
            continue
        try:
            strike = float(details.get("strike_price"))
        except (TypeError, ValueError):
            continue
        ctype = str(details.get("contract_type") or "").lower()
        if ctype not in {"call", "put"}:
            continue
        def f(value: Any) -> float | None:
            try:
                number = float(value)
                return number if number == number else None
            except (TypeError, ValueError):
                return None
        iv = f(item.get("implied_volatility"))
        rows.append({
            "contract": details.get("ticker"),
            "type": ctype,
            "expiry": exp.isoformat(),
            "dte": dte,
            "strike": strike,
            "iv": iv,
            "open_interest": f(item.get("open_interest")),
            "volume": f(day.get("volume")),
            "bid": f(quote.get("bid")) or f(quote.get("bid_price")),
            "ask": f(quote.get("ask")) or f(quote.get("ask_price")),
            "last": f(trade.get("price")) or f(day.get("close")),
            "delta": f(greeks.get("delta")),
            "gamma": f(greeks.get("gamma")),
            "theta": f(greeks.get("theta")),
            "vega": f(greeks.get("vega")),
        })
    if not rows:
        return {"available": False, "source": "massive", "reason": "no_valid_option_rows"}
    spot = current_price
    if spot is None or spot <= 0:
        try:
            frame, _src = get_daily_history(sym, period="1mo")
            spot = float(frame["Close"].dropna().iloc[-1]) if not frame.empty else None
        except Exception:
            spot = None
    expiries = sorted({(r["dte"], r["expiry"]) for r in rows}, key=lambda x: (0 if 7 <= x[0] <= 45 else 100) + abs(x[0] - 21))
    target_dte, target_expiry = expiries[0]
    same = [r for r in rows if r["expiry"] == target_expiry]

    def nearest(side: str) -> dict[str, Any] | None:
        valid = [r for r in same if r["type"] == side and r.get("iv") and r["iv"] > 0]
        if not valid:
            return None
        if spot and spot > 0:
            return min(valid, key=lambda r: abs(float(r["strike"]) - float(spot)))
        return valid[0]

    call = nearest("call")
    put = nearest("put")
    ivs = [r["iv"] for r in (call, put) if r and r.get("iv") and r["iv"] > 0]
    if not ivs:
        return {"available": False, "source": "massive", "reason": "missing_atm_iv", "expiry": target_expiry, "dte": target_dte}
    call_vol_values = [float(r["volume"]) for r in same if r["type"] == "call" and r.get("volume") is not None]
    put_vol_values = [float(r["volume"]) for r in same if r["type"] == "put" and r.get("volume") is not None]
    call_oi_values = [float(r["open_interest"]) for r in same if r["type"] == "call" and r.get("open_interest") is not None]
    put_oi_values = [float(r["open_interest"]) for r in same if r["type"] == "put" and r.get("open_interest") is not None]
    total_call_volume = sum(call_vol_values) if call_vol_values else None
    total_put_volume = sum(put_vol_values) if put_vol_values else None
    total_call_oi = sum(call_oi_values) if call_oi_values else None
    total_put_oi = sum(put_oi_values) if put_oi_values else None

    def ratio(num: float | None, den: float | None) -> float | None:
        return round(num / den, 4) if num is not None and den is not None and den > 0 else None

    def avg_field(field: str) -> float | None:
        vals = [float(r[field]) for r in (call, put) if r and r.get(field) is not None]
        return round(sum(vals) / len(vals), 6) if vals else None

    atm_iv = round(sum(float(v) for v in ivs) / len(ivs), 4)
    return {
        "available": True,
        "source": "massive:option_snapshot",
        "status": "ok",
        "expiry": target_expiry,
        "dte": target_dte,
        "atm_iv": atm_iv,
        "call_iv": round(float(call["iv"]), 4) if call and call.get("iv") else None,
        "put_iv": round(float(put["iv"]), 4) if put and put.get("iv") else None,
        "call_strike": call.get("strike") if call else None,
        "put_strike": put.get("strike") if put else None,
        "call_quote": call,
        "put_quote": put,
        "greeks": {
            "delta": avg_field("delta"),
            "gamma": avg_field("gamma"),
            "theta": avg_field("theta"),
            "vega": avg_field("vega"),
        },
        "call_put_ratio": ratio(total_call_volume, total_put_volume),
        "put_call_ratio": ratio(total_put_volume, total_call_volume),
        "call_put_oi_ratio": ratio(total_call_oi, total_put_oi),
        "call_volume": int(total_call_volume) if total_call_volume is not None else None,
        "put_volume": int(total_put_volume) if total_put_volume is not None else None,
        "call_open_interest": int(total_call_oi) if total_call_oi is not None else None,
        "put_open_interest": int(total_put_oi) if total_put_oi is not None else None,
        "activity_available": bool(call_vol_values or put_vol_values or call_oi_values or put_oi_values),
        "oi_available": bool(call_oi_values or put_oi_values),
        "contracts_sampled": len(rows),
        "expiry_contracts_sampled": len(same),
        "note": "Massive current option snapshot; IV rank/percentile needs persisted historical IV.",
    }


def _bs_option_price(spot: float, strike: float, dte: int, sigma: float, option_type: str, r: float = 0.04) -> float:
    import math

    if spot <= 0 or strike <= 0 or dte <= 0 or sigma <= 0:
        return 0.0
    t = max(dte / 365.0, 1e-6)
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t

    def cdf(x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    if option_type.lower() == "call":
        return spot * cdf(d1) - strike * math.exp(-r * t) * cdf(d2)
    return strike * math.exp(-r * t) * cdf(-d2) - spot * cdf(-d1)


def _implied_vol_from_mid(spot: float, strike: float, dte: int, mid: float, option_type: str) -> float | None:
    if spot <= 0 or strike <= 0 or dte <= 0 or mid <= 0:
        return None
    lo, hi = 0.01, 5.0
    for _ in range(60):
        mid_sigma = (lo + hi) / 2.0
        price = _bs_option_price(spot, strike, dte, mid_sigma, option_type)
        if price > mid:
            hi = mid_sigma
        else:
            lo = mid_sigma
    out = (lo + hi) / 2.0
    return round(out, 4) if 0.01 <= out <= 5.0 else None


def _databento_candidate_sessions(max_days: int = 8) -> list[date]:
    today = datetime.now(timezone.utc).date()
    out: list[date] = []
    d = today - timedelta(days=1)
    while len(out) < max_days:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out


def _close_on_or_before(symbol: str, session: date, fallback: float | None) -> float | None:
    try:
        frame, _src = get_daily_history(symbol, period="1mo")
        if frame is None or frame.empty or "Close" not in frame:
            return fallback
        idx = pd.to_datetime(frame.index).date
        local = frame.copy()
        local["_session_date"] = idx
        local = local[local["_session_date"] <= session]
        if local.empty:
            return fallback
        value = float(pd.to_numeric(local["Close"], errors="coerce").dropna().iloc[-1])
        return value if value > 0 else fallback
    except Exception:
        return fallback


def get_databento_options_snapshot(symbol: str, current_price: float | None = None) -> dict[str, Any]:
    """Delayed OPRA fallback from Databento.

    Databento OPRA is raw market data, not a finished IV/Greeks product. We use
    definition to find near-month ATM contracts and cbbo-1m to get the latest
    bid/ask, then back out IV with Black-Scholes. This intentionally does not
    invent OI or volume; those require statistics/trades and may be unavailable
    or more expensive.
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        return {"available": False, "source": "databento:opra", "reason": "missing_symbol"}
    if not os.environ.get("DATABENTO_API_KEY", "").strip():
        return {"available": False, "source": "databento:opra", "reason": "missing_api_key"}
    if not current_price or current_price <= 0:
        return {"available": False, "source": "databento:opra", "reason": "missing_price"}

    cache_key = f"databento_opra_option_snapshot:v3:{sym}"
    path = _cache_path("databento_opra_option_snapshot_v3", sym, "json")
    if _fresh(path, 6 * 3600):
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(cached, dict):
                return {**cached, "cache_hit": True}
        except Exception:
            pass

    try:
        import databento as db
    except Exception as exc:
        return {"available": False, "source": "databento:opra", "reason": "sdk_unavailable", "detail": str(exc)[:160]}

    client = db.Historical()
    last_error = ""
    for session in _databento_candidate_sessions():
        spot_for_session = _close_on_or_before(sym, session, current_price)
        if not spot_for_session or spot_for_session <= 0:
            continue
        start = session.isoformat() + "T00:00"
        end = (session + timedelta(days=1)).isoformat() + "T00:00"
        try:
            defs = client.timeseries.get_range(
                dataset="OPRA.PILLAR",
                symbols=[f"{sym}.OPT"],
                schema="definition",
                start=start,
                end=end,
                stype_in="parent",
            ).to_df()
        except Exception as exc:
            last_error = f"{exc.__class__.__name__}: {str(exc)[:180]}"
            continue
        if defs is None or defs.empty:
            continue
        try:
            df = defs.copy()
            df["expiration_dt"] = pd.to_datetime(df["expiration"], errors="coerce").dt.date
            df["dte"] = df["expiration_dt"].apply(lambda x: (x - session).days if pd.notna(x) else None)
            df["strike_price"] = pd.to_numeric(df["strike_price"], errors="coerce")
            df = df[df["dte"].between(7, 45) & df["strike_price"].notna()]
            if df.empty:
                continue
            expiries = sorted(df[["expiration_dt", "dte"]].drop_duplicates().itertuples(index=False), key=lambda x: abs(int(x.dte) - 21))
            target_exp, target_dte = expiries[0].expiration_dt, int(expiries[0].dte)
            same = df[df["expiration_dt"] == target_exp]
            legs: dict[str, dict[str, Any]] = {}
            raw_symbols: list[str] = []
            for cls, side in (("C", "call"), ("P", "put")):
                subset = same[same["instrument_class"].astype(str).str.upper() == cls].copy()
                if subset.empty:
                    continue
                subset["dist"] = (subset["strike_price"] - float(spot_for_session)).abs()
                row = subset.sort_values("dist").iloc[0].to_dict()
                raw = str(row.get("raw_symbol") or row.get("symbol") or "")
                if not raw:
                    continue
                legs[side] = {
                    "contract": raw,
                    "type": side,
                    "expiry": target_exp.isoformat(),
                    "dte": target_dte,
                    "strike": float(row["strike_price"]),
                }
                raw_symbols.append(raw)
            if not raw_symbols:
                continue
        except Exception as exc:
            last_error = f"definition_parse_error: {str(exc)[:160]}"
            continue

        try:
            quote_start = session.isoformat() + "T19:55"
            quote_end = session.isoformat() + "T20:00"
            quotes = client.timeseries.get_range(
                dataset="OPRA.PILLAR",
                symbols=raw_symbols,
                schema="cbbo-1m",
                start=quote_start,
                end=quote_end,
                stype_in="raw_symbol",
            ).to_df()
        except Exception as exc:
            last_error = f"{exc.__class__.__name__}: {str(exc)[:180]}"
            continue
        if quotes is None or quotes.empty:
            continue

        for side, leg in legs.items():
            q = quotes[quotes["symbol"].astype(str) == leg["contract"]].tail(1)
            if q.empty:
                continue
            bid = float(q.iloc[0].get("bid_px_00")) if pd.notna(q.iloc[0].get("bid_px_00")) else None
            ask = float(q.iloc[0].get("ask_px_00")) if pd.notna(q.iloc[0].get("ask_px_00")) else None
            mid = None
            if bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid:
                mid = (bid + ask) / 2.0
            elif ask is not None and ask > 0:
                mid = ask
            elif bid is not None and bid > 0:
                mid = bid
            leg.update({"bid": bid, "ask": ask, "mid": round(mid, 4) if mid else None})

        parity_spot = None
        call_leg = legs.get("call") or {}
        put_leg = legs.get("put") or {}
        try:
            if (
                call_leg.get("mid") is not None
                and put_leg.get("mid") is not None
                and call_leg.get("strike") is not None
                and abs(float(call_leg["strike"]) - float(put_leg.get("strike") or 0)) < 1e-9
            ):
                t = max(float(call_leg.get("dte") or 0) / 365.0, 1e-6)
                parity_spot = float(call_leg["mid"]) - float(put_leg["mid"]) + float(call_leg["strike"]) * (2.718281828459045 ** (-0.04 * t))
        except Exception:
            parity_spot = None
        spot_for_iv = parity_spot if parity_spot and parity_spot > 0 else spot_for_session

        ivs: list[float] = []
        for side, leg in legs.items():
            iv = _implied_vol_from_mid(float(spot_for_iv), float(leg["strike"]), int(leg["dte"]), float(leg.get("mid") or 0), side)
            leg["iv"] = iv
            if iv is not None:
                ivs.append(iv)
        if not ivs:
            last_error = "missing_bid_ask_or_iv"
            continue

        atm_iv = round(sum(ivs) / len(ivs), 4)
        payload = {
            "available": True,
            "source": "databento:opra_delayed",
            "status": "ok",
            "status_cn": "已获取 Databento OPRA 延迟报价并反推 ATM IV",
            "dataset": "OPRA.PILLAR",
            "schema": "definition+cbbo-1m",
            "as_of_date": session.isoformat(),
            "underlying_price_for_iv": round(float(spot_for_iv), 4),
            "underlying_price_basis": "put_call_parity" if parity_spot else "daily_close_or_current",
            "current_price": round(float(current_price), 4) if current_price else None,
            "quote_window_utc": f"{session.isoformat()}T19:55/{session.isoformat()}T20:00",
            "expiry": legs.get("call", legs.get("put", {})).get("expiry"),
            "dte": legs.get("call", legs.get("put", {})).get("dte"),
            "atm_iv": atm_iv,
            "call_iv": legs.get("call", {}).get("iv"),
            "put_iv": legs.get("put", {}).get("iv"),
            "call_strike": legs.get("call", {}).get("strike"),
            "put_strike": legs.get("put", {}).get("strike"),
            "call_quote": legs.get("call"),
            "put_quote": legs.get("put"),
            "greeks": {},
            "activity_available": False,
            "oi_available": False,
            "call_put_ratio": None,
            "put_call_ratio": None,
            "call_put_oi_ratio": None,
            "call_volume": None,
            "put_volume": None,
            "call_open_interest": None,
            "put_open_interest": None,
            "contracts_sampled": int(len(defs)),
            "expiry_contracts_sampled": int(len(same)),
            "note": "Databento OPRA delayed quote fallback. IV is inferred from CBBO mid; OI/volume are not returned by this lightweight fallback.",
            "cache_hit": False,
        }
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return payload

    return {"available": False, "source": "databento:opra", "reason": "opra_unavailable", "detail": last_error}


def get_financials(symbol: str, quarters: int = 8) -> list[dict[str, Any]]:
    """Quarterly reported financials from Massive/Polygon (universe-wide on free).

    Returns newest-first ``[{filing_date, end_date, fiscal_period, revenue,
    diluted_eps}]``.  No analyst estimates (Massive has none) -- these are the
    as-reported actuals used for fundamental-momentum / earnings-actuals signals.
    """
    try:
        payload = _massive_get("/vX/reference/financials", ticker=symbol,
                               timeframe="quarterly", order="desc",
                               sort="period_of_report_date", limit=int(quarters))
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for r in (payload or {}).get("results") or []:
        inc = (r.get("financials") or {}).get("income_statement") or {}
        def _v(key: str) -> Optional[float]:
            cell = inc.get(key) or {}
            try:
                return float(cell.get("value")) if cell.get("value") is not None else None
            except (TypeError, ValueError):
                return None
        out.append({
            "filing_date": r.get("filing_date") or r.get("acceptance_datetime"),
            "end_date": r.get("end_date"),
            "fiscal_period": r.get("fiscal_period"),
            "fiscal_year": r.get("fiscal_year"),
            "revenue": _v("revenues"),
            "diluted_eps": _v("diluted_earnings_per_share") or _v("basic_earnings_per_share"),
        })
    return out


def get_ticker_reference(symbol: str) -> dict[str, Any]:
    """Company reference (name / industry / market cap) from Massive/Polygon
    reference. Permanent cache (static). Tries direct first, then the proxy
    (CBOE_PROXY -> FMP_PROXY) so it still works when direct TLS is flaky."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return {}
    path = _cache_path("ticker_reference", sym)
    cached = _read_json(path)
    if isinstance(cached, dict) and (cached.get("sic_description") or cached.get("name")):
        return cached
    if not external_data_allowed():
        return cached or {}
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if not api_key:
        return cached or {}
    proxy = (os.getenv("CBOE_PROXY") or os.getenv("FMP_PROXY") or "").strip()
    attempts = [None] + ([{"http": proxy, "https": proxy}] if proxy else [])
    for proxies in attempts:
        try:
            r = requests.get(f"{_MASSIVE_BASE}/v3/reference/tickers/{sym}",
                             params={"apiKey": api_key}, proxies=proxies, timeout=15)
            if r.status_code != 200:
                continue
            res = (r.json() or {}).get("results") or {}
            if res:
                out = {
                    "name": res.get("name"),
                    "sic_description": res.get("sic_description"),
                    "market_cap": res.get("market_cap"),
                    "total_employees": res.get("total_employees"),
                    "homepage_url": res.get("homepage_url"),
                }
                _write_json(path, out)
                return out
        except Exception:
            continue
    return cached or {}


def get_market_cap_snapshot(symbol: str, refresh: bool = False) -> dict[str, Any]:
    """Market cap is session-scoped, unlike the permanent ticker profile cache.

    Massive reference does not expose the market-cap observation date. Record
    when we fetched it and which completed session caused the refresh, without
    claiming that the provider value itself is point-in-time for that session.
    """
    from market_calendar import most_recent_session

    sym = str(symbol or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", sym):
        return {"available": False, "source": "unavailable"}
    required_session = most_recent_session().isoformat()
    path = _cache_path("market_cap_by_session", sym)
    cached = _read_json(path)
    from gildata_shadow_service import cached_equity, reference_enabled, refresh_references

    if reference_enabled():
        if refresh and external_data_allowed():
            refresh_references([sym], required_session)
        sample = cached_equity(sym) or {}
        cap = float(sample.get("market_cap_usd") or 0)
        if sample.get("as_of") == required_session and 0 < cap < 1e15:
            return {"available": True, "market_cap": round(cap), "source": "gildata:FinQuery:cached",
                    "fetched_at": sample.get("fetched_at"), "refreshed_for_session": required_session,
                    "data_as_of_date": required_session, "stale": False, "cache_hit": True}
    if isinstance(cached, dict) and cached.get("market_cap") and cached.get("refreshed_for_session") == required_session:
        return {**cached, "available": True, "stale": False, "cache_hit": True}

    if refresh and external_data_allowed():
        try:
            payload = _massive_get(f"/v3/reference/tickers/{sym}") or {}
            value = float(((payload.get("results") or {}).get("market_cap")))
            if 0 < value < 1e15:
                result = {
                    "available": True,
                    "market_cap": round(value),
                    "source": "massive:reference",
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "refreshed_for_session": required_session,
                    "data_as_of_date": None,
                    "stale": False,
                    "cache_hit": False,
                }
                _write_json(path, result)
                return result
        except (TypeError, ValueError, requests.RequestException):
            pass

    # A manually collected GilData observation is a dated backup, not a
    # reason to call its paid MCP endpoint from an ordinary page read.
    try:
        from gildata_shadow_service import cached_equity

        shadow = cached_equity(sym)
        cap = float((shadow or {}).get("market_cap_usd") or 0)
        if (shadow or {}).get("as_of") == required_session and 0 < cap < 1e15:
            return {
                "available": True,
                "market_cap": round(cap),
                "source": "gildata:FinQuery:cached",
                "fetched_at": None,
                "refreshed_for_session": required_session,
                "data_as_of_date": required_session,
                "stale": False,
                "cache_hit": True,
            }
    except (TypeError, ValueError, OSError):
        pass
    if isinstance(cached, dict) and cached.get("market_cap"):
        return {**cached, "available": True, "stale": True, "cache_hit": True}
    return {"available": False, "source": "unavailable", "stale": True}


def _merge_daily(old: pd.DataFrame, fresh: pd.DataFrame) -> pd.DataFrame:
    if old.empty:
        return _normalize_daily(fresh)
    if fresh.empty:
        return _normalize_daily(old)
    merged = pd.concat([old, fresh])
    return _normalize_daily(merged[~merged.index.duplicated(keep="last")])


def _is_us_trading_day(d: date) -> bool:
    """Return True if *d* is a regular US equity trading day (Mon–Fri)."""
    return d.weekday() < 5  # Saturday = 5, Sunday = 6


def _us_market_close_utc(d: date) -> datetime:
    """Return the scheduled cash-equity close for a given date, as UTC."""
    from market_calendar import session_close_et
    local_close = session_close_et(d)
    return local_close.astimezone(timezone.utc)


def get_latest_us_market_close_utc(now: datetime | None = None) -> datetime:
    """Return the UTC time of the most recent US regular-session close.

    When called *before* 16:00 ET on a trading day the latest close is the
    *previous* trading day's close.  After 16:00 ET (or on a weekend /
    holiday) the latest close is the current (or most recent) trading day's.

    Uses the holiday-aware NYSE calendar (market_calendar). This matters: a
    weekday holiday (e.g. Juneteenth) has NO bar, so a weekend-only rollback
    would expect a non-existent close and mark every cache 'stale' -> the data
    layer would then refetch on EVERY call. Holiday awareness keeps the cache
    'current' so reads stay cache-fast.
    """
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_et = now_utc.astimezone(_US_EASTERN)
    try:
        from market_calendar import is_trading_day, most_recent_session

        return _us_market_close_utc(most_recent_session(now_et))
    except Exception:
        # Fallback: weekday-only rollback (legacy behavior).
        candidate = now_et.date()
        if (now_et.hour < _US_MARKET_CLOSE_HOUR
                or (now_et.hour == _US_MARKET_CLOSE_HOUR and now_et.minute < _US_MARKET_CLOSE_MINUTE)):
            candidate -= timedelta(days=1)
        while not _is_us_trading_day(candidate):
            candidate -= timedelta(days=1)
        return _us_market_close_utc(candidate)


def is_daily_data_current(frame: pd.DataFrame, now: datetime | None = None) -> bool:
    """Check whether *frame* includes at least one bar on or after the latest US market close.

    Returns True when the frame's max date >= the calendar date of the most
    recent close, meaning the data already incorporates that session.  A
    one-day grace window is applied: prices for a close that happened a few
    hours ago may not yet be available from free data sources, so we only
    demand that the data cover the close date itself (not a specific hour).
    """
    if frame is None or frame.empty:
        return False
    latest_close_utc = get_latest_us_market_close_utc(now)
    latest_close_date = latest_close_utc.date()
    frame_max = pd.Timestamp(frame.index.max()).to_pydatetime().date()
    return frame_max >= latest_close_date


def get_daily_history(
    symbol: str,
    period: str = "6mo",
    start: str | None = None,
    end: str | None = None,
    allow_yfinance_fallback: bool = True,
    skip_massive: bool = False,
) -> tuple[pd.DataFrame, str]:
    """Return daily OHLCV using cache, Massive, Tiingo, then Twelve Data.

    The cache is automatically bypassed when it does not cover the most recent
    US market close (respecting ET timezone).  This guarantees that callers
    who need today's close data (e.g. the overnight-research cockpit) always
    get a fresh attempt before falling back to cache.
    """
    symbol = str(symbol).strip().upper()
    if not external_data_allowed():
        allow_yfinance_fallback = False
    daily_path = _cache_path("ohlcv_daily", symbol, "csv")
    cached = _read_daily_cache(symbol)
    requested_start = pd.Timestamp(start).to_pydatetime().replace(tzinfo=timezone.utc) if start else _period_start(period)
    requested_start_naive = pd.Timestamp(requested_start).tz_localize(None)
    cache_covers_range = not cached.empty and cached.index.min() <= requested_start_naive + timedelta(days=7)

    # Both TTL-based freshness AND market-close coverage must be satisfied.
    file_is_fresh = _fresh(daily_path, _env_int("VIBE_OHLCV_CACHE_TTL_SECONDS", 900))
    data_is_current = is_daily_data_current(cached) if not cached.empty else False
    cache_is_fresh = file_is_fresh and cache_covers_range and data_is_current

    refresh_start = requested_start
    if not cached.empty:
        refresh_start = max(requested_start, cached.index.max().to_pydatetime().replace(tzinfo=timezone.utc) - timedelta(days=7))
    source = "cache:ohlcv"
    fetch_start = requested_start if not cache_covers_range else refresh_start
    try:
        if cache_is_fresh:
            fresh = pd.DataFrame()
            source = "cache:fresh"
        elif allow_yfinance_fallback:
            # Interactive / per-symbol path only. Primary: Massive (Polygon
            # Starter = unlimited calls, direct, deep history) using RAW prices to
            # match the cache. Fall back to Tiingo -> Twelve Data. The BULK path
            # (allow_yfinance_fallback=False, e.g. the ~200-name board warm) is
            # NOT routed here; bulk reads the grouped-daily cache or yfinance.
            fresh = _massive_aggs(symbol, fetch_start, adjusted=False) if not skip_massive else pd.DataFrame()
            src_tag = "massive:incremental"
            if fresh.empty:
                fresh = _tiingo_daily(symbol, fetch_start)
                src_tag = "tiingo:incremental"
            if fresh.empty:
                fresh = _twelve_daily(symbol, fetch_start)
                src_tag = "twelvedata:incremental"
            if not fresh.empty:
                cached = _merge_daily(cached, fresh)
                _write_daily_cache(symbol, cached)
                source = src_tag
            elif not cached.empty:
                source = "cache:ohlcv"
        else:
            fresh = pd.DataFrame()  # bulk: serve cache; download_daily_history does yf bulk for misses
    except Exception:
        fresh = pd.DataFrame()

    # Last resort if the incremental refresh above didn't deliver and we still
    # need data: retry Massive over the FULL requested range (Polygon Starter =
    # unlimited calls, direct connection, 20s timeout). We deliberately do NOT
    # fall back to yfinance here -- the old no-timeout yf.download was the source
    # of the multi-hour daily-batch hang and is heavily rate-limited in-container.
    still_not_current = not cached.empty and not is_daily_data_current(cached)
    if (cached.empty or still_not_current) and allow_yfinance_fallback and not skip_massive:
        try:
            fresh = _massive_aggs(symbol, requested_start, adjusted=False)
            if not fresh.empty:
                cached = _merge_daily(cached, fresh)
                _write_daily_cache(symbol, cached)
                source = "massive:fallback"
        except Exception:
            if cached.empty:
                cached = pd.DataFrame()

    if cached.empty:
        return cached, source
    filtered = cached[cached.index >= requested_start_naive]
    if end:
        filtered = filtered[filtered.index <= pd.Timestamp(end)]
    return filtered, source


def download_daily_history(symbols: Iterable[str], period: str = "6mo") -> tuple[pd.DataFrame, dict[str, str]]:
    """Return a yfinance-compatible multi-symbol frame while gradually warming Twelve cache."""
    tickers = list(dict.fromkeys(str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()))
    frames: dict[str, pd.DataFrame] = {}
    sources: dict[str, str] = {}
    for symbol in tickers:
        frame, source = get_daily_history(symbol, period=period, allow_yfinance_fallback=False)
        if not frame.empty:
            frames[symbol] = frame
            sources[symbol] = source
    # Warm any cache misses from Massive per-symbol (unlimited calls, direct,
    # 20s timeout) instead of the old rate-limited/unreliable yfinance bulk.
    missing = [symbol for symbol in tickers if symbol not in frames]
    if missing and external_data_allowed():
        start_dt = _period_start(period)
        for symbol in missing:
            try:
                frame = _massive_aggs(symbol, start_dt, adjusted=False)
            except Exception:
                frame = pd.DataFrame()
            if not frame.empty:
                _write_daily_cache(symbol, frame)
                frames[symbol] = frame
                sources[symbol] = "massive:bulk-fallback"
    if not frames:
        return pd.DataFrame(), sources
    return pd.concat(frames, axis=1), sources


def _fmp_get(endpoint: str, symbol: str, ttl_seconds: int = 24 * 3600, **params: Any) -> Any:
    api_key = os.environ.get("FMP_API_KEY", "").strip()
    cache_key = f"{endpoint}_{symbol}_{'_'.join(f'{key}-{value}' for key, value in sorted(params.items()))}"
    path = _cache_path("fmp", cache_key)
    cached = _read_json(path)
    if cached is not None and _fresh(path, ttl_seconds):
        return cached
    if not external_data_allowed():
        return cached
    if not api_key or not _claim("fmp", _env_int("FMP_PER_MINUTE", 20), _env_int("FMP_PER_DAY", 180)):
        return cached
    # FMP is blocked on some networks (e.g. mainland China); route ONLY FMP
    # through the user's proxy when FMP_PROXY is set, leaving every other data
    # source (yfinance, etc.) on the direct connection.
    proxy = os.environ.get("FMP_PROXY", "").strip()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    response = requests.get(f"{_FMP_BASE}/{endpoint}", params={"symbol": symbol, "apikey": api_key, **params}, timeout=15, proxies=proxies)
    response.raise_for_status()
    payload = response.json()
    _write_json(path, payload)
    return payload


def get_company_fundamentals(symbol: str) -> dict[str, Any]:
    """Return cached FMP company profile and quarterly revenue growth when available."""
    result: dict[str, Any] = {"source": "fmp"}
    try:
        profiles = _fmp_get("profile", symbol) or []
        profile = profiles[0] if isinstance(profiles, list) and profiles else {}
        result.update({
            "company_name": profile.get("companyName"),
            "industry": profile.get("industry"),
            "sector": profile.get("sector"),
            "market_cap": profile.get("marketCap"),
            "price": profile.get("price"),
        })
    except Exception:
        pass
    try:
        statements = _fmp_get("income-statement", symbol, period="quarter", limit=5) or []
        if isinstance(statements, list) and len(statements) >= 5:
            latest = float(statements[0].get("revenue") or 0)
            prior = float(statements[4].get("revenue") or 0)
            result["revenue_yoy"] = latest / prior - 1.0 if prior else None
    except Exception:
        pass
    return result


def get_next_earnings(symbol: str) -> dict[str, Any]:
    """Next scheduled earnings date + most recent earnings surprise (FMP).

    Returns ``{available, next_date, days_until, eps_estimated,
    revenue_estimated, last_surprise_pct, last_report_date, estimate_tone}``.
    One cached FMP call (24h TTL). Used to flag/avoid holding or adding into the
    earnings landmine (the biggest overnight tail risk). ``estimate_tone`` is a
    transparent heuristic, not an earnings prediction.
    """
    out: dict[str, Any] = {"available": False}
    rows: list[Any] = []
    try:
        # Free tier caps 'limit' at 4; that still covers the next report + a
        # couple of recent ones for the latest surprise.
        rows = _fmp_get("earnings", symbol, limit=4) or []
    except Exception:
        rows = []
    if not isinstance(rows, list):
        rows = []
    today = date.today().isoformat()
    # Future = no actual EPS yet and date today-or-later; next = earliest of those.
    future = sorted(
        [r for r in rows if r.get("date") and str(r["date"]) >= today and r.get("epsActual") is None],
        key=lambda r: str(r["date"]),
    )
    past = sorted(
        [r for r in rows if r.get("epsActual") is not None],
        key=lambda r: str(r["date"]),
        reverse=True,
    )
    if future:
        nxt = future[0]
        try:
            d0 = date.fromisoformat(str(nxt["date"])[:10])
            days_until = (d0 - date.today()).days
        except (TypeError, ValueError):
            days_until = None
        out.update({
            "available": True,
            "next_date": str(nxt["date"])[:10],
            "days_until": days_until,
            "eps_estimated": nxt.get("epsEstimated"),
            "revenue_estimated": nxt.get("revenueEstimated") or nxt.get("revenueEstimate"),
            "time": nxt.get("time"),
            "source": "fmp",
        })
    if past:
        last = past[0]
        est, act = last.get("epsEstimated"), last.get("epsActual")
        surprise = None
        try:
            if est not in (None, 0):
                surprise = round((float(act) - float(est)) / abs(float(est)), 4)
        except (TypeError, ValueError):
            surprise = None
        out["available"] = True
        out["last_report_date"] = str(last.get("date"))[:10]
        out["last_surprise_pct"] = surprise
        out["last_eps_actual"] = act
        out["last_eps_estimated"] = est
        out.setdefault("source", "fmp")
    if not out.get("next_date") and external_data_allowed():
        try:
            ticker = yf.Ticker(symbol)
            dates = ticker.get_earnings_dates(limit=8)
            if dates is not None and not dates.empty:
                frame = dates.copy()
                frame.index = pd.to_datetime(frame.index).tz_localize(None)
                future_dates = frame[frame.index.date >= date.today()]
                if not future_dates.empty:
                    idx = future_dates.index.min()
                    row = future_dates.loc[idx]
                    eps_est = None
                    try:
                        eps_est = row.get("EPS Estimate")
                    except Exception:
                        eps_est = None
                    out.update({
                        "available": True,
                        "next_date": idx.date().isoformat(),
                        "days_until": (idx.date() - date.today()).days,
                        "eps_estimated": out.get("eps_estimated") if out.get("eps_estimated") is not None else eps_est,
                        "source": out.get("source") or "yfinance",
                    })
        except Exception:
            try:
                cal = yf.Ticker(symbol).calendar
                if isinstance(cal, pd.DataFrame) and not cal.empty:
                    raw = None
                    if "Earnings Date" in cal.index:
                        raw = cal.loc["Earnings Date"].iloc[0]
                    elif "Earnings Date" in cal.columns:
                        raw = cal["Earnings Date"].iloc[0]
                    if raw is not None:
                        d0 = pd.to_datetime(raw).date()
                        if d0 >= date.today():
                            out.update({
                                "available": True,
                                "next_date": d0.isoformat(),
                                "days_until": (d0 - date.today()).days,
                                "source": out.get("source") or "yfinance",
                            })
                elif isinstance(cal, dict):
                    raw = cal.get("Earnings Date") or cal.get("earningsDate")
                    if isinstance(raw, (list, tuple)):
                        raw = raw[0] if raw else None
                    if raw is not None:
                        d0 = pd.to_datetime(raw).date()
                        if d0 >= date.today():
                            out.update({
                                "available": True,
                                "next_date": d0.isoformat(),
                                "days_until": (d0 - date.today()).days,
                                "source": out.get("source") or "yfinance",
                            })
            except Exception:
                pass
    tone = "neutral"
    basis = ""
    try:
        next_eps = float(out.get("eps_estimated"))
        last_eps = float(out.get("last_eps_actual"))
        if last_eps != 0:
            eps_change = next_eps / abs(last_eps) - (1 if last_eps > 0 else -1)
            out["eps_estimate_vs_last_actual"] = round(eps_change, 4)
            if eps_change >= 0.05:
                tone = "positive"
                basis = f"下一季EPS预期较上次实际高 {eps_change:.1%}"
            elif eps_change <= -0.05:
                tone = "warning"
                basis = f"下一季EPS预期较上次实际低 {abs(eps_change):.1%}"
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    if tone == "neutral":
        surprise = out.get("last_surprise_pct")
        if isinstance(surprise, (int, float)):
            if surprise >= 0.10:
                tone = "positive"
                basis = f"上次EPS超预期 {surprise:.1%}，下一季预期需复核"
            elif surprise <= -0.10:
                tone = "warning"
                basis = f"上次EPS低于预期 {abs(surprise):.1%}，下一季预期需复核"
    out["estimate_tone"] = tone
    out["estimate_basis"] = basis or "未形成明确正/负预期信号"
    return out


def get_earnings_events(symbol: str) -> list[dict[str, Any]]:
    """Past reported-earnings rows with computed EPS surprise (FMP free, limit<=4).

    Returns ``[{date, eps_actual, eps_estimated, surprise}]`` for rows that have a
    realized EPS and a non-zero estimate.  Used by the PEAD signal validation.
    """
    try:
        rows = _fmp_get("earnings", symbol, limit=4) or []
    except Exception:
        rows = []
    out: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        return out
    for r in rows:
        act, est, dt = r.get("epsActual"), r.get("epsEstimated"), r.get("date")
        if act is None or est in (None, 0) or not dt:
            continue
        try:
            surprise = (float(act) - float(est)) / abs(float(est))
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        out.append({
            "date": str(dt)[:10],
            "eps_actual": float(act),
            "eps_estimated": float(est),
            "surprise": round(surprise, 4),
        })
    return out


def get_analyst_view(symbol: str) -> dict[str, Any]:
    """Latest analyst rating mix + price-target summary (FMP, 2 cached calls)."""
    out: dict[str, Any] = {"available": False}
    from gildata_shadow_service import cached_equity, reference_enabled, refresh_references
    from market_calendar import most_recent_session

    if reference_enabled():
        session = most_recent_session().isoformat()
        if external_data_allowed():
            refresh_references([symbol], session)
        sample = cached_equity(symbol) or {}
        if sample.get("as_of") == session and (sample.get("ratings") or sample.get("target_avg_usd") is not None):
            ratings = sample.get("ratings") or {}
            out.update(available=True, source="gildata:FinQuery", as_of=session,
                       rating_scheme="gildata_five_categories", ratings=ratings,
                       target_avg_usd=sample.get("target_avg_usd"),
                       target_count=sample.get("target_count"), target_window_days=sample.get("target_window_days"))
            # Group directional ratings only for the existing statistical display;
            # retain the provider's five original categories separately.
            if ratings:
                out.update(buy=ratings["buy"] + ratings["overweight"], hold=ratings["neutral"],
                           sell=ratings["sell"] + ratings["underweight"])
                _annotate_analyst_view(symbol, out)
            return out
    try:
        grades = _fmp_get("grades-historical", symbol, limit=1) or []
    except Exception:
        grades = []
    if isinstance(grades, list) and grades:
        g = grades[0]
        out.update({
            "available": True,
            "as_of": str(g.get("date"))[:10],
            "strong_buy": g.get("analystRatingsStrongBuy"),
            "buy": g.get("analystRatingsBuy"),
            "hold": g.get("analystRatingsHold"),
            "sell": g.get("analystRatingsSell"),
            "strong_sell": g.get("analystRatingsStrongSell"),
        })
    try:
        pt = _fmp_get("price-target-summary", symbol) or []
    except Exception:
        pt = []
    if isinstance(pt, list) and pt:
        p = pt[0]
        out["available"] = True
        out["target_avg_quarter"] = p.get("lastQuarterAvgPriceTarget")
        out["target_count_quarter"] = p.get("lastQuarterCount")
    _annotate_analyst_view(symbol, out)
    return out


def _wilson_lower_bound(successes: int, total: int, z: float = 1.96) -> float | None:
    if total <= 0:
        return None
    p = successes / total
    denom = 1.0 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * ((p * (1 - p) + z * z / (4 * total)) / total) ** 0.5
    return max(0.0, (centre - margin) / denom)


def _annotate_analyst_view(symbol: str, out: dict[str, Any]) -> None:
    def i(value: Any) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0

    buy = i(out.get("strong_buy")) + i(out.get("buy"))
    hold = i(out.get("hold"))
    sell = i(out.get("sell")) + i(out.get("strong_sell"))
    total = buy + hold + sell
    directional = buy + sell
    buy_dir = buy / directional if directional > 0 else None
    sell_dir = sell / directional if directional > 0 else None
    buy_lb = _wilson_lower_bound(buy, directional) if directional > 0 else None
    sell_lb = _wilson_lower_bound(sell, directional) if directional > 0 else None
    rating_signal = "neutral"
    if directional >= 5 and buy_lb is not None and buy_lb >= 0.75:
        rating_signal = "strong_bullish"
    elif directional >= 5 and sell_lb is not None and sell_lb >= 0.75:
        rating_signal = "strong_bearish"
    elif total >= 5 and buy > sell:
        rating_signal = "bullish"
    elif total >= 5 and sell > buy:
        rating_signal = "bearish"
    out["rating_stats"] = {
        "buy": buy,
        "hold": hold,
        "sell": sell,
        "total": total,
        "directional_count": directional,
        "directional_buy_share": round(buy_dir, 4) if buy_dir is not None else None,
        "directional_sell_share": round(sell_dir, 4) if sell_dir is not None else None,
        "wilson_buy_lower_95": round(buy_lb, 4) if buy_lb is not None else None,
        "wilson_sell_lower_95": round(sell_lb, 4) if sell_lb is not None else None,
        "signal": rating_signal,
        "method": "directional buy/sell consensus with Wilson 95% lower bound; hold is neutral",
    }

def classify_data_source(source: str) -> dict[str, Any]:
    """Map an OHLCV ``source`` tag to a data-quality tier (O2).

    Tiers: ``primary`` (live paid/official feed), ``cache`` (recently cached),
    ``fallback`` (yfinance best-effort, unofficial).  Used to flag calibration
    curves and signals that rest on degraded data.
    """
    value = str(source or "").lower()
    if value.startswith(("twelvedata", "fmp", "tiingo", "massive", "polygon", "databento")):
        return {"tier": "primary", "quality_score": 1.0, "is_fallback": False}
    if value.startswith("cache"):
        return {"tier": "cache", "quality_score": 0.7, "is_fallback": False}
    if value.startswith("yfinance"):
        return {"tier": "fallback", "quality_score": 0.4, "is_fallback": True}
    return {"tier": "unknown", "quality_score": 0.5, "is_fallback": False}


def assess_data_quality(symbol: str, frame: pd.DataFrame, source: str, *, stale_after_days: int = 7) -> dict[str, Any]:
    """Return a per-symbol data-quality assessment (tier + staleness)."""
    tier = classify_data_source(source)
    if frame is None or frame.empty:
        return {
            "symbol": str(symbol).upper(), "source": source, **tier,
            "bars": 0, "last_bar_date": None, "age_days": None, "is_stale": True,
        }
    last = pd.Timestamp(frame.index.max())
    age_days = int((pd.Timestamp.utcnow().tz_localize(None).normalize() - last.normalize()).days)
    return {
        "symbol": str(symbol).upper(), "source": source, **tier,
        "bars": int(len(frame)),
        "last_bar_date": last.strftime("%Y-%m-%d"),
        "age_days": age_days,
        "is_stale": age_days > int(stale_after_days),
    }


def aggregate_data_quality(sources: dict[str, str]) -> dict[str, Any]:
    """Summarise a batch of ``{symbol: source}`` tags into quality shares."""
    total = len(sources) or 1
    tiers: dict[str, int] = {"primary": 0, "cache": 0, "fallback": 0, "unknown": 0}
    for source in sources.values():
        tiers[classify_data_source(source)["tier"]] += 1
    fallback_share = tiers["fallback"] / total
    return {
        "symbols": len(sources),
        "tier_counts": tiers,
        "fallback_share": round(fallback_share, 4),
        "mean_quality_score": round(
            sum(classify_data_source(s)["quality_score"] for s in sources.values()) / total, 4
        ),
        "data_quality_limited": fallback_share >= 0.5,
    }


def point_in_time_earnings_configured() -> bool:
    """Whether a paid point-in-time earnings provider + key are configured (O2 seam)."""
    return bool(
        os.environ.get("EARNINGS_PIT_PROVIDER", "").strip()
        and os.environ.get("EARNINGS_PIT_API_KEY", "").strip()
    )


def get_point_in_time_earnings(symbol: str, as_of: str) -> dict[str, Any] | None:
    """Seam for a paid point-in-time earnings feed (O2). Returns None until configured.

    Free earnings sources (finnhub/alpha_vantage) return *restated* values, which
    leaks look-ahead into any historical peer-earnings backtest.  A paid PIT feed
    (e.g. Polygon, Tiingo, FMP premium) is required to get the earnings date and
    surprise *as they were known on ``as_of``*.

    To plug one in, set ``EARNINGS_PIT_PROVIDER`` + ``EARNINGS_PIT_API_KEY`` and
    implement the provider call here, returning at least
    ``{report_date, eps_surprise_pct, revenue_yoy, known_as_of}``.  Until then
    this returns None so callers transparently fall back to current behaviour.
    """
    if not point_in_time_earnings_configured():
        return None
    # Intentionally not implemented: requires a paid provider + key.
    raise NotImplementedError(
        "EARNINGS_PIT_PROVIDER is set but no PIT provider integration is implemented yet."
    )


def data_source_status() -> dict[str, Any]:
    from app_database import cache_get
    from gildata_shadow_service import reference_enabled
    try:
        import cost_model
        cost_assumptions = cost_model.cost_summary()
    except Exception:
        cost_assumptions = {}
    tiingo_configured = bool(os.environ.get("TIINGO_API_KEY", "").strip())
    massive_configured = bool(os.environ.get("MASSIVE_API_KEY", "").strip())
    # Daily OHLCV no longer uses yfinance (removed: no-timeout hang + rate limits).
    # Massive (Polygon Starter, unlimited/direct) is primary per-symbol AND bulk.
    ohlcv_priority = ["cache"]
    if os.environ.get("TWELVE_DATA_API_KEY", "").strip():
        ohlcv_priority.insert(0, "twelvedata")
    if tiingo_configured:
        ohlcv_priority.insert(0, "tiingo")
    if massive_configured:
        # Massive grouped-daily (bulk EOD) + per-symbol aggregates lead.
        ohlcv_priority.insert(0, "massive")
        ohlcv_priority.insert(0, "massive_grouped_daily")
    return {
        "ohlcv_priority": ohlcv_priority,
        "configuration_only": True,
        "gildata_reference": {
            "enabled": reference_enabled(),
            "last_refresh": cache_get("gildata:reference_last_status"),
            "primary_fields": ["dated_us_market_cap", "five_category_ratings", "100_day_target_mean", "annual_eps_estimates", "daily_vix"],
            "daily_quote_price_basis": "unspecified_display_only_not_historical_cache",
            "news": cache_get("gildata:news_validation") or {"status": "not_verified"},
            "research_enrichment": cache_get("gildata:research_last_status"),
            "news_supplement": cache_get("gildata:news_supplement_last_status"),
            "limitations": ["PE may be missing", "annual EPS is not next-quarter earnings", "news exact-date/symbol/provenance not verified", "no option quotes verified"],
        },
        "daily_cache_price_basis": "raw_unadjusted",
        "price_basis_note": "后续日线补洞统一使用原始价；既有历史缓存尚未逐笔核验。已配置密钥不等于授权有效或当前可用。",
        "provider_configuration": {
            name: "configured_unprobed" if os.environ.get(key, "").strip() else "not_configured"
            for name, key in {"massive": "MASSIVE_API_KEY", "tiingo": "TIINGO_API_KEY",
                              "twelvedata": "TWELVE_DATA_API_KEY", "fmp": "FMP_API_KEY",
                              "databento": "DATABENTO_API_KEY"}.items()
        },
        "fundamentals_priority": ["massive_financials", "fmp", "cache", "yfinance"] if massive_configured else ["fmp", "cache", "yfinance"],
        "options_priority": ["massive", "cboe", "yfinance"],
        "twelvedata_configured": bool(os.environ.get("TWELVE_DATA_API_KEY", "").strip()),
        "tiingo_configured": tiingo_configured,
        "massive_configured": massive_configured,
        "fmp_configured": bool(os.environ.get("FMP_API_KEY", "").strip()),
        "paid_pit_earnings_configured": point_in_time_earnings_configured(),
        "source_quality_tiers": {
            "twelvedata": "primary",
            "tiingo": "primary",
            "massive": "primary",
            "polygon": "primary",
            "databento": "primary",
            "fmp": "primary",
            "cache": "cache",
            "yfinance": "fallback",
        },
        "cost_assumptions": cost_assumptions,
        "cache_root": str(_CACHE_ROOT),
    }
