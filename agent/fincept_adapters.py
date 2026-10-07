"""Small Fincept-inspired data adapters for optional market-data fallback.

This module intentionally re-implements only the thin public-data adapters we
need instead of importing FinceptTerminal directly.  Keep it dependency-light:
requests + pandas only, with cache and explicit source metadata.
"""

from __future__ import annotations

import csv
import email.utils
import io
import json
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests


_CACHE_ROOT = Path(os.environ.get("VIBE_MARKET_DATA_CACHE_DIR", "/app/agent/data_cache/market_data"))
_CBOE_BASE = "https://cdn.cboe.com/api/global/delayed_quotes"
_CBOE_VIX_BASE = "https://cdn.cboe.com/api/global/us_indices/daily_prices"
_GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
_SEC_BASE = "https://data.sec.gov"
_SEC_COMPANY_TICKERS = "https://www.sec.gov/files/company_tickers.json"
_USER_AGENT = os.environ.get("VIBE_HTTP_USER_AGENT", "Vibe-Trading/0.1 research data adapter")


def _enabled(name: str, default: bool = True) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _cache_path(kind: str, key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(key).upper()).strip("_") or "UNKNOWN"
    path = _CACHE_ROOT / "fincept_adapters" / kind / f"{safe}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _fresh(path: Path, ttl_seconds: int) -> bool:
    return path.exists() and time.time() - path.stat().st_mtime < ttl_seconds


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def _write_json(path: Path, payload: Any) -> None:
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _request_text(url: str, timeout: int = 20) -> str:
    response = requests.get(url, headers={"User-Agent": _USER_AGENT, "Accept": "*/*"}, timeout=timeout)
    response.raise_for_status()
    return response.text


def _request_json(url: str, timeout: int = 20) -> dict[str, Any]:
    response = requests.get(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"}, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def _option_endpoint_symbol(symbol: str) -> str:
    symbol = str(symbol or "").strip().upper().replace("^", "")
    return f"_{symbol}" if symbol in {"VIX", "VX", "SPX", "SPEU", "NDX", "NDXE", "RUT", "RUTE"} else symbol


def _parse_occ_like_option(option_symbol: str) -> tuple[str, str, str, float] | None:
    text = str(option_symbol or "").strip().upper()
    match = re.match(r"^(?P<root>[A-Z.\-]+)(?P<expiry>\d{6})(?P<type>[CP])(?P<strike>\d+)$", text)
    if not match:
        return None
    raw_strike = match.group("strike").lstrip("0") or "0"
    try:
        strike = float(raw_strike) / 1000.0
    except ValueError:
        return None
    option_type = "call" if match.group("type") == "C" else "put"
    return match.group("root"), match.group("expiry"), option_type, strike


def _expiry_ymd(expiry_yymmdd: str) -> str:
    try:
        return datetime.strptime(expiry_yymmdd, "%y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        if result == result and result not in {float("inf"), float("-inf")}:
            return result
    except (TypeError, ValueError):
        pass
    return default


def _to_iv(value: Any) -> float:
    iv = _to_float(value)
    return iv / 100.0 if iv > 5 else iv


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _parse_rss_date(raw: str) -> str:
    if not raw:
        return ""
    try:
        return email.utils.parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat()
    except Exception:
        return raw


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(text or ""))).strip()


def get_cboe_vix_latest(index_name: str = "VIX", ttl_seconds: int | None = None) -> dict[str, Any]:
    """Return latest CBOE volatility index close from the official CDN CSV."""
    if not _enabled("FINCEPT_CBOE_ENABLED", True):
        return {"available": False, "source": "cboe:disabled", "reason": "FINCEPT_CBOE_ENABLED is disabled"}
    ttl_seconds = ttl_seconds if ttl_seconds is not None else int(os.environ.get("CBOE_VIX_CACHE_TTL_SECONDS", "900") or 900)
    index = str(index_name or "VIX").upper()
    filenames = {
        "VIX": "VIX_History.csv",
        "VIX3M": "VIX3M_History.csv",
        "VVIX": "VVIX_History.csv",
        "SKEW": "SKEW_History.csv",
        "VXN": "VXN_History.csv",
        "VXO": "VXO_History.csv",
    }
    filename = filenames.get(index)
    if not filename:
        return {"available": False, "source": "cboe", "reason": f"unsupported index {index}"}
    cache = _cache_path("cboe_vix", index)
    if _fresh(cache, ttl_seconds):
        cached = _read_json(cache)
        if isinstance(cached, dict):
            cached["cache_hit"] = True
            return cached
    try:
        text = _request_text(f"{_CBOE_VIX_BASE}/{filename}")
        rows = list(csv.DictReader(io.StringIO(text)))
        rows = [row for row in rows if any((value or "").strip() for value in row.values())]
        if not rows:
            raise ValueError("empty CBOE CSV")
        latest = rows[-1]
        close_key = next((key for key in latest if str(key).strip().upper() in {"CLOSE", "VIX CLOSE"}), "")
        date_key = next((key for key in latest if str(key).strip().upper() in {"DATE", "TRADE DATE"}), "")
        value = _to_float(latest.get(close_key), 0.0)
        if value <= 0:
            raise ValueError("missing close value")
        payload = {
            "available": True,
            "value": round(value, 2),
            "index": index,
            "as_of_date": latest.get(date_key, ""),
            "source": f"cboe:{filename}",
            "source_type": "real_vix",
            "cache_hit": False,
            "raw": latest,
        }
        _write_json(cache, payload)
        return payload
    except Exception as exc:
        cached = _read_json(cache)
        if isinstance(cached, dict):
            cached["cache_hit"] = True
            cached["stale"] = True
            cached["warning"] = f"CBOE refresh failed: {str(exc)[:160]}"
            return cached
        return {"available": False, "source": "cboe", "reason": str(exc)[:240]}


def get_cboe_option_chain(symbol: str, expiry: str | None = None, ttl_seconds: int | None = None) -> dict[str, Any]:
    """Return CBOE delayed option chain mapped to yfinance-like columns."""
    if not _enabled("FINCEPT_CBOE_ENABLED", True):
        return {"available": False, "source": "cboe:disabled", "reason": "FINCEPT_CBOE_ENABLED is disabled"}
    ttl_seconds = ttl_seconds if ttl_seconds is not None else int(os.environ.get("CBOE_OPTION_CHAIN_CACHE_TTL_SECONDS", "1800") or 1800)
    clean_symbol = str(symbol or "").strip().upper()
    if not clean_symbol:
        return {"available": False, "source": "cboe", "reason": "empty symbol"}
    cache = _cache_path("cboe_options", f"{clean_symbol}_{expiry or 'ALL'}")
    if _fresh(cache, ttl_seconds):
        cached = _read_json(cache)
        if isinstance(cached, dict):
            cached["cache_hit"] = True
            return cached
    try:
        endpoint_symbol = _option_endpoint_symbol(clean_symbol)
        payload = _request_json(f"{_CBOE_BASE}/options/{endpoint_symbol}.json")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        options = data.get("options") if isinstance(data, dict) else []
        if not isinstance(options, list) or not options:
            raise ValueError("empty CBOE option chain")

        calls: list[dict[str, Any]] = []
        puts: list[dict[str, Any]] = []
        expiries: set[str] = set()
        for row in options:
            if not isinstance(row, dict):
                continue
            parsed = _parse_occ_like_option(str(row.get("option") or row.get("contract_symbol") or ""))
            if not parsed:
                continue
            _, expiry_raw, option_type, strike = parsed
            expiry_ymd = _expiry_ymd(expiry_raw)
            if not expiry_ymd:
                continue
            expiries.add(expiry_ymd)
            if expiry and expiry_ymd != expiry:
                continue
            mapped = {
                "contractSymbol": row.get("option"),
                "lastTradeDate": row.get("last_trade_time"),
                "strike": strike,
                "lastPrice": _to_float(row.get("last")),
                "bid": _to_float(row.get("bid")),
                "ask": _to_float(row.get("ask")),
                "change": _to_float(row.get("change")),
                "percentChange": _to_float(row.get("percent_change") or row.get("percentChange")),
                "volume": _to_int(row.get("volume")),
                "openInterest": _to_int(row.get("open_interest")),
                "impliedVolatility": _to_iv(row.get("iv")),
                "theoreticalPrice": _to_float(row.get("theo")),
                "delta": _to_float(row.get("delta")),
                "gamma": _to_float(row.get("gamma")),
                "theta": _to_float(row.get("theta")),
                "vega": _to_float(row.get("vega")),
                "currency": "USD",
                "source": "cboe:delayed_options",
            }
            if option_type == "call":
                calls.append(mapped)
            else:
                puts.append(mapped)
        result = {
            "available": bool(calls or puts),
            "source": "cboe:delayed_options",
            "cache_hit": False,
            "symbol": clean_symbol,
            "expiry": expiry,
            "expiries": sorted(expiries),
            "metadata": {
                "current_price": data.get("current_price") if isinstance(data, dict) else None,
                "iv30": _to_iv(data.get("iv30")) if isinstance(data, dict) else None,
                "last_trade_time": data.get("last_trade_time") if isinstance(data, dict) else None,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            },
            "calls": calls,
            "puts": puts,
        }
        if not result["available"]:
            result["reason"] = f"CBOE chain has no contracts for expiry {expiry}"
        _write_json(cache, result)
        return result
    except Exception as exc:
        cached = _read_json(cache)
        if isinstance(cached, dict):
            cached["cache_hit"] = True
            cached["stale"] = True
            cached["warning"] = f"CBOE refresh failed: {str(exc)[:160]}"
            return cached
        return {"available": False, "source": "cboe", "reason": str(exc)[:240]}


def cboe_chain_to_frames(chain: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Convert a chain payload to yfinance-like calls/puts DataFrames."""
    calls = pd.DataFrame(chain.get("calls") or [])
    puts = pd.DataFrame(chain.get("puts") or [])
    for frame in (calls, puts):
        if frame.empty:
            continue
        for column in ("strike", "lastPrice", "bid", "ask", "change", "percentChange", "volume", "openInterest", "impliedVolatility"):
            if column not in frame:
                frame[column] = 0.0
            frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
    return calls, puts


def get_google_news_rss(
    query: str,
    max_items: int = 10,
    period: str = "7d",
    language: str = "en-US",
    region: str = "US",
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """Return Google News RSS search results in the event-scanner schema."""
    if not _enabled("FINCEPT_NEWS_ENABLED", True):
        return {"available": False, "source": "google_news_rss:disabled", "reason": "FINCEPT_NEWS_ENABLED is disabled"}
    ttl_seconds = ttl_seconds if ttl_seconds is not None else int(os.environ.get("GOOGLE_NEWS_CACHE_TTL_SECONDS", "1800") or 1800)
    clean_query = str(query or "").strip()
    if not clean_query:
        return {"available": False, "source": "google_news_rss", "reason": "empty query", "items": []}
    cache = _cache_path("google_news_rss", f"{clean_query}_{period}_{language}_{region}_{max_items}")
    if _fresh(cache, ttl_seconds):
        cached = _read_json(cache)
        if isinstance(cached, dict):
            cached["cache_hit"] = True
            return cached
    try:
        search_query = f"{clean_query} when:{period}" if period else clean_query
        params = urllib.parse.urlencode({"q": search_query, "hl": language, "gl": region, "ceid": f"{region}:en"})
        text = _request_text(f"{_GOOGLE_NEWS_RSS}?{params}")
        root = ET.fromstring(text)
        items: list[dict[str, Any]] = []
        for item in root.findall(".//item")[: max(0, int(max_items))]:
            source_node = item.find("source")
            items.append(
                {
                    "title": _strip_html(item.findtext("title") or ""),
                    "url": (item.findtext("link") or "").strip(),
                    "published_at": _parse_rss_date(item.findtext("pubDate") or ""),
                    "summary": _strip_html(item.findtext("description") or ""),
                    "publisher": _strip_html(source_node.text if source_node is not None else ""),
                    "source": "google_news_rss",
                }
            )
        result = {
            "available": True,
            "source": "google_news_rss",
            "cache_hit": False,
            "query": clean_query,
            "period": period,
            "count": len(items),
            "items": items,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_json(cache, result)
        return result
    except Exception as exc:
        cached = _read_json(cache)
        if isinstance(cached, dict):
            cached["cache_hit"] = True
            cached["stale"] = True
            cached["warning"] = f"Google News refresh failed: {str(exc)[:160]}"
            return cached
        return {"available": False, "source": "google_news_rss", "reason": str(exc)[:240], "items": []}


def get_sec_company_tickers(ttl_seconds: int | None = None) -> dict[str, Any]:
    """Return SEC ticker to CIK mapping from the official company_tickers.json."""
    if not _enabled("FINCEPT_SEC_ENABLED", True):
        return {"available": False, "source": "sec:disabled", "reason": "FINCEPT_SEC_ENABLED is disabled"}
    ttl_seconds = ttl_seconds if ttl_seconds is not None else int(os.environ.get("SEC_COMPANY_TICKERS_TTL_SECONDS", "86400") or 86400)
    cache = _cache_path("sec", "company_tickers")
    if _fresh(cache, ttl_seconds):
        cached = _read_json(cache)
        if isinstance(cached, dict):
            cached["cache_hit"] = True
            return cached
    try:
        raw = _request_json(_SEC_COMPANY_TICKERS)
        by_ticker: dict[str, dict[str, Any]] = {}
        by_cik: dict[str, str] = {}
        for entry in raw.values():
            if not isinstance(entry, dict):
                continue
            ticker = str(entry.get("ticker") or "").upper().replace(".", "-")
            cik_raw = str(entry.get("cik_str") or "").strip()
            if not ticker or not cik_raw:
                continue
            cik = cik_raw.lstrip("0").zfill(10)
            by_ticker[ticker] = {"ticker": ticker, "cik": cik, "name": entry.get("title") or ""}
            by_cik[cik] = ticker
        result = {
            "available": bool(by_ticker),
            "source": "sec:company_tickers",
            "cache_hit": False,
            "by_ticker": by_ticker,
            "by_cik": by_cik,
            "count": len(by_ticker),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_json(cache, result)
        return result
    except Exception as exc:
        cached = _read_json(cache)
        if isinstance(cached, dict):
            cached["cache_hit"] = True
            cached["stale"] = True
            cached["warning"] = f"SEC ticker refresh failed: {str(exc)[:160]}"
            return cached
        return {"available": False, "source": "sec:company_tickers", "reason": str(exc)[:240]}


def get_sec_recent_filings(symbol: str, limit: int = 20, forms: list[str] | None = None, ttl_seconds: int | None = None) -> dict[str, Any]:
    """Return recent SEC filings for a ticker using official EDGAR submissions."""
    if not _enabled("FINCEPT_SEC_ENABLED", True):
        return {"available": False, "source": "sec:disabled", "reason": "FINCEPT_SEC_ENABLED is disabled", "filings": []}
    ttl_seconds = ttl_seconds if ttl_seconds is not None else int(os.environ.get("SEC_FILINGS_CACHE_TTL_SECONDS", "3600") or 3600)
    ticker = str(symbol or "").upper().replace(".", "-")
    if not ticker:
        return {"available": False, "source": "sec:submissions", "reason": "empty symbol", "filings": []}
    form_filter = {str(x).upper() for x in forms or [] if str(x).strip()}
    cache = _cache_path("sec_filings", f"{ticker}_{'-'.join(sorted(form_filter)) or 'ALL'}_{limit}")
    if _fresh(cache, ttl_seconds):
        cached = _read_json(cache)
        if isinstance(cached, dict):
            cached["cache_hit"] = True
            return cached
    try:
        mapping = get_sec_company_tickers()
        cik = ((mapping.get("by_ticker") or {}).get(ticker) or {}).get("cik")
        if not cik:
            raise ValueError(f"CIK not found for {ticker}")
        payload = _request_json(f"{_SEC_BASE}/submissions/CIK{cik}.json")
        recent = ((payload.get("filings") or {}).get("recent") or {}) if isinstance(payload, dict) else {}
        forms_raw = recent.get("form") or []
        filings: list[dict[str, Any]] = []
        for idx, form in enumerate(forms_raw):
            form_text = str(form or "").upper()
            if form_filter and form_text not in form_filter:
                continue
            accession = str((recent.get("accessionNumber") or [""])[idx] or "")
            filing_date = str((recent.get("filingDate") or [""])[idx] or "")
            report_date = str((recent.get("reportDate") or [""])[idx] or "")
            primary_doc = str((recent.get("primaryDocument") or [""])[idx] or "")
            filings.append(
                {
                    "symbol": ticker,
                    "cik": cik,
                    "form": form_text,
                    "filing_date": filing_date,
                    "report_date": report_date,
                    "accession_number": accession,
                    "primary_document": primary_doc,
                    "description": str((recent.get("primaryDocDescription") or [""])[idx] or ""),
                    "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{primary_doc}" if accession and primary_doc else "",
                    "source": "sec:submissions",
                }
            )
            if len(filings) >= max(0, int(limit)):
                break
        result = {
            "available": True,
            "source": "sec:submissions",
            "cache_hit": False,
            "symbol": ticker,
            "cik": cik,
            "company_name": payload.get("name") if isinstance(payload, dict) else "",
            "count": len(filings),
            "filings": filings,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_json(cache, result)
        return result
    except Exception as exc:
        cached = _read_json(cache)
        if isinstance(cached, dict):
            cached["cache_hit"] = True
            cached["stale"] = True
            cached["warning"] = f"SEC filings refresh failed: {str(exc)[:160]}"
            return cached
        return {"available": False, "source": "sec:submissions", "symbol": ticker, "reason": str(exc)[:240], "filings": []}
