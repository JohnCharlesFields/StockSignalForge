"""Scan S&P 500 news and produce event-driven launch candidates.

This script is designed for research reports. It uses no-key public data
sources by default and writes auditable CSV/JSON/Markdown artifacts.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import io
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fincept_adapters import get_google_news_rss
from market_data_service import download_daily_history


WIKI_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
YAHOO_RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline"
USER_AGENT = "Mozilla/5.0 Vibe-Trading Event Scanner/1.0"

POSITIVE_WORDS = {
    "beat": 0.30,
    "beats": 0.30,
    "raise": 0.25,
    "raises": 0.25,
    "raised": 0.25,
    "upgrade": 0.22,
    "upgraded": 0.22,
    "bullish": 0.18,
    "buy": 0.14,
    "wins": 0.18,
    "win": 0.14,
    "soars": 0.24,
    "soar": 0.22,
    "gains": 0.16,
    "surge": 0.24,
    "record": 0.20,
    "approval": 0.28,
    "approved": 0.28,
    "partnership": 0.18,
    "contract": 0.20,
    "buyback": 0.24,
    "launch": 0.16,
    "growth": 0.16,
    "boost": 0.16,
    "boosts": 0.16,
    "power": 0.12,
    "powers": 0.12,
    "well": 0.08,
    "profit": 0.14,
    "margin": 0.10,
}

NEGATIVE_WORDS = {
    "miss": -0.30,
    "misses": -0.30,
    "cut": -0.26,
    "cuts": -0.26,
    "downgrade": -0.22,
    "downgraded": -0.22,
    "bearish": -0.18,
    "sell": -0.14,
    "risk": -0.12,
    "risks": -0.12,
    "concern": -0.12,
    "concerns": -0.12,
    "probe": -0.25,
    "investigation": -0.28,
    "lawsuit": -0.24,
    "fraud": -0.40,
    "recall": -0.24,
    "delay": -0.18,
    "falls": -0.16,
    "plunge": -0.28,
    "warning": -0.24,
    "loss": -0.16,
    "weak": -0.14,
    "postpone": -0.18,
    "postpones": -0.18,
    "postponed": -0.18,
    "peaked": -0.16,
    "unusual": -0.08,
}

POSITIVE_PHRASES = {
    "raises price target": 0.34,
    "raised price target": 0.34,
    "price target raised": 0.34,
    "upgraded to buy": 0.32,
    "beats estimates": 0.34,
    "beats expectations": 0.34,
    "stock soars": 0.30,
    "stock surges": 0.30,
    "stock rises": 0.20,
    "wins fda approval": 0.36,
    "fda approval": 0.30,
    "strategic partnership": 0.22,
    "launches": 0.16,
    "powers new": 0.18,
    "best ai stock": 0.14,
    "settling well": 0.12,
    "sales boost": 0.16,
}

NEGATIVE_PHRASES = {
    "cuts price target": -0.34,
    "cut price target": -0.34,
    "downgraded to sell": -0.34,
    "misses estimates": -0.34,
    "misses expectations": -0.34,
    "stock falls": -0.22,
    "stock drops": -0.24,
    "stock plunges": -0.32,
    "postpones": -0.18,
    "postpones its": -0.20,
    "put options activity": -0.12,
    "has stock peaked": -0.18,
    "bearish": -0.16,
    "geopolitical tensions": -0.18,
    "license sprawl": -0.10,
}

EVENT_PATTERNS = [
    ("earnings", r"\b(earnings|eps|revenue|sales|profit|quarter|q[1-4])\b"),
    ("guidance", r"\b(guidance|forecast|outlook|expects|target)\b"),
    ("analyst_rating", r"\b(upgrade|downgrade|price target|initiates|rating)\b"),
    ("mna", r"\b(acquire|acquisition|merger|takeover|deal|stake)\b"),
    ("buyback", r"\b(buyback|repurchase|dividend)\b"),
    ("regulatory", r"\b(fda|sec|doj|ftc|regulator|approval|approved)\b"),
    ("legal", r"\b(lawsuit|probe|investigation|settlement|fraud)\b"),
    ("product", r"\b(launch|product|chip|ai|model|platform|contract)\b"),
    ("supply_chain", r"\b(supply|inventory|shipment|tariff|export)\b"),
    ("macro", r"\b(fed|rate|inflation|cpi|jobs|treasury|tariff|china)\b"),
]


@dataclass
class EventRow:
    event_id: str
    collected_at: str
    published_at: str
    effective_at: str
    symbol: str
    company: str
    sector: str
    event_type: str
    score: float
    sentiment_score: float
    impact_score: float
    confidence: float
    novelty: float
    relevance_score: float
    source: str
    url: str
    title: str
    summary: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def http_get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def cached_http_get(url: str, cache_dir: Optional[Path], ttl_minutes: int) -> str:
    if cache_dir is None or ttl_minutes <= 0:
        return http_get(url)

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.sha1(url.encode("utf-8")).hexdigest()
    cache_path = cache_dir / f"{cache_key}.xml"
    meta_path = cache_dir / f"{cache_key}.json"
    now = datetime.now(timezone.utc)

    if cache_path.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            fetched_at = parse_pubdate(str(meta.get("fetched_at", "")))
            age_minutes = (now - fetched_at).total_seconds() / 60.0
            if age_minutes <= ttl_minutes:
                return cache_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass

    text = http_get(url)
    cache_path.write_text(text, encoding="utf-8")
    meta_path.write_text(
        json.dumps({"url": url, "fetched_at": now.isoformat()}, ensure_ascii=False),
        encoding="utf-8",
    )
    return text


def load_sp500_universe(limit: Optional[int] = None, cache_dir: Optional[Path] = None) -> pd.DataFrame:
    cache_path = cache_dir / "sp500_universe.csv" if cache_dir else None
    if cache_path and cache_path.exists():
        try:
            df = pd.read_csv(cache_path)
            if {"symbol", "company", "sector"}.issubset(df.columns):
                return df.head(limit).reset_index(drop=True) if limit else df.reset_index(drop=True)
        except Exception:
            pass

    html_text = http_get(WIKI_SP500_URL)
    tables = pd.read_html(io.StringIO(html_text))
    df = tables[0].rename(columns={"Symbol": "symbol", "Security": "company", "GICS Sector": "sector"})
    df["symbol"] = df["symbol"].astype(str).str.replace(".", "-", regex=False)
    df = df[["symbol", "company", "sector"]].dropna()
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(cache_path, index=False, encoding="utf-8")
    if limit:
        df = df.head(limit)
    return df.reset_index(drop=True)


def parse_ticker_list(raw: str) -> List[str]:
    if not raw:
        return []
    return [x.strip().upper().replace(".", "-") for x in re.split(r"[,;\s]+", raw) if x.strip()]


def load_universe(args: argparse.Namespace, cache_dir: Optional[Path] = None) -> pd.DataFrame:
    if args.tickers:
        tickers = parse_ticker_list(args.tickers)
        return pd.DataFrame({"symbol": tickers, "company": tickers, "sector": "Unknown"})
    if args.universe_csv:
        df = pd.read_csv(args.universe_csv)
        if "symbol" not in df.columns:
            raise ValueError("universe csv must contain a symbol column")
        df["company"] = df.get("company", df["symbol"])
        df["sector"] = df.get("sector", "Unknown")
        return df[["symbol", "company", "sector"]].head(args.limit or len(df))
    return load_sp500_universe(args.limit, cache_dir=cache_dir)


def yahoo_rss_items(
    symbol: str,
    max_items: int,
    cache_dir: Optional[Path] = None,
    cache_ttl_minutes: int = 30,
) -> List[Dict[str, str]]:
    query = urllib.parse.urlencode({"s": symbol, "region": "US", "lang": "en-US"})
    url = f"{YAHOO_RSS_URL}?{query}"
    text = cached_http_get(url, cache_dir, cache_ttl_minutes)
    root = ET.fromstring(text)
    items = []
    for item in root.findall(".//item")[:max_items]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        published = (item.findtext("pubDate") or "").strip()
        description = re.sub(r"<[^>]+>", " ", item.findtext("description") or "")
        items.append(
            {
                "title": re.sub(r"\s+", " ", title),
                "url": link,
                "published_at": published,
                "summary": re.sub(r"\s+", " ", description).strip(),
                "source": "yahoo_rss",
            }
        )
    return items


def news_items_with_fallback(
    symbol: str,
    company: str,
    max_items: int,
    cache_dir: Optional[Path] = None,
    cache_ttl_minutes: int = 30,
) -> List[Dict[str, str]]:
    """Use Yahoo RSS first, then fill missing slots with Google News RSS."""
    items = yahoo_rss_items(symbol, max_items, cache_dir=cache_dir, cache_ttl_minutes=cache_ttl_minutes)
    if len(items) >= max_items:
        return items
    query = f"{company} OR {symbol} stock"
    fallback = get_google_news_rss(
        query,
        max_items=max_items - len(items),
        period="7d",
        ttl_seconds=max(cache_ttl_minutes * 60, 60),
    )
    if fallback.get("available"):
        items.extend(fallback.get("items") or [])
    seen: set[str] = set()
    deduped: List[Dict[str, str]] = []
    for item in items:
        key = str(item.get("url") or item.get("title") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= max_items:
            break
    return deduped


def parse_pubdate(raw: str) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    try:
        parsed = pd.to_datetime(raw, utc=True)
        return parsed.to_pydatetime()
    except Exception:
        return datetime.now(timezone.utc)


def effective_time(published: datetime) -> datetime:
    eastern_hour = (published - timedelta(hours=4)).hour
    if eastern_hour >= 16:
        return (published + timedelta(days=1)).replace(hour=13, minute=30, second=0, microsecond=0)
    return published


def classify_event(text: str) -> str:
    lower = text.lower()
    for event_type, pattern in EVENT_PATTERNS:
        if re.search(pattern, lower):
            return event_type
    return "other"


def company_aliases(symbol: str, company: str) -> List[str]:
    base = str(company).lower()
    aliases = {str(symbol).lower(), base}
    replacements = [
        " inc.",
        " inc",
        " corp.",
        " corp",
        " corporation",
        " co.",
        " co",
        " class a",
        " class b",
        " common stock",
    ]
    cleaned = base
    for item in replacements:
        cleaned = cleaned.replace(item, "")
    cleaned = cleaned.strip()
    if cleaned:
        aliases.add(cleaned)
        first = cleaned.split()[0]
        if len(first) >= 4:
            aliases.add(first)

    manual = {
        "AAPL": ["apple"],
        "MSFT": ["microsoft"],
        "NVDA": ["nvidia"],
        "GOOGL": ["alphabet", "google"],
        "GOOG": ["alphabet", "google"],
        "AMZN": ["amazon"],
        "META": ["meta", "facebook"],
        "TSLA": ["tesla"],
    }
    aliases.update(manual.get(str(symbol).upper(), []))
    return sorted(a for a in aliases if len(a) >= 2)


def relevance_score(symbol: str, company: str, title: str, summary: str) -> float:
    title_lower = title.lower()
    summary_lower = summary.lower()
    lower = f"{title_lower} {summary_lower}"
    score = 0.0
    ticker = str(symbol).lower().replace("-", ".")
    title_hit = False
    if re.search(rf"\b{re.escape(ticker)}\b", title_lower):
        score += 0.80
        title_hit = True
    for alias in company_aliases(symbol, company):
        pattern = rf"\b{re.escape(alias)}\b"
        if re.search(pattern, title_lower):
            score += 0.55 if " " in alias else 0.45
            title_hit = True
        elif re.search(pattern, summary_lower):
            score += 0.18

    mega_names = ["apple", "microsoft", "nvidia", "google", "alphabet", "amazon", "meta", "tesla"]
    mentioned = {name for name in mega_names if re.search(rf"\b{name}\b", lower)}
    own_aliases = set(company_aliases(symbol, company))
    other_mentions = [name for name in mentioned if name not in own_aliases]
    if len(other_mentions) >= 2:
        score -= 0.35
    elif len(other_mentions) >= 1:
        score -= 0.15 if title_hit else 0.25

    if "etf" in lower or "market by" in lower or "global forecast" in lower:
        score = min(score - 0.35, 0.25)
    shopping_noise = ["sleeper sofa", "coupon", "member benefits", "best deals", "gift card"]
    if any(term in lower for term in shopping_noise):
        score = min(score, 0.25)
    if not title_hit:
        score = min(score, 0.25)
    return max(0.0, min(1.0, score))


def score_text(text: str) -> Tuple[float, float, float]:
    words = re.findall(r"[a-zA-Z][a-zA-Z\-]+", text.lower())
    sentiment = 0.0
    for word in words:
        sentiment += POSITIVE_WORDS.get(word, 0.0)
        sentiment += NEGATIVE_WORDS.get(word, 0.0)

    urgency = 0.0
    lower = text.lower()
    for phrase, value in POSITIVE_PHRASES.items():
        if phrase in lower:
            sentiment += value
            urgency += 0.06
    for phrase, value in NEGATIVE_PHRASES.items():
        if phrase in lower:
            sentiment += value
            urgency += 0.06
    for phrase in ("breaking", "unexpected", "surprise", "beats estimates", "misses estimates"):
        if phrase in lower:
            urgency += 0.10
    if "bullish or bearish" in lower:
        sentiment = sentiment * 0.4
    impact = min(1.0, 0.30 + abs(sentiment) + urgency)
    confidence = min(1.0, 0.45 + min(len(text), 500) / 1000.0)
    sentiment = max(-1.0, min(1.0, sentiment))
    score = max(-1.0, min(1.0, sentiment * impact))
    return score, impact, confidence


def _extract_json_object(text: str) -> Optional[dict]:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.I).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        payload = json.loads(cleaned)
        return payload if isinstance(payload, dict) else None
    except Exception:
        pass
    match = re.search(r"\{.*\}", cleaned, flags=re.S)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _numeric_value(value: object, default: float, low: float, high: float) -> float:
    try:
        numeric = float(value)  # type: ignore[arg-type]
        if abs(numeric) > high and high <= 1.0:
            numeric = numeric / 10.0 if abs(numeric) <= 10 else numeric / 100.0
    except Exception:
        return default
    return max(low, min(high, numeric))


def llm_refine_event_score(
    title: str,
    summary: str,
    event_type: str,
    base_score: float,
    base_impact: float,
    base_confidence: float,
    timeout: int = 20,
) -> Tuple[float, float, float, str]:
    api_key = (
        os.getenv("OPENAI_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
        or os.getenv("OPENROUTER_API_KEY")
    )
    base_url = (
        os.getenv("OPENAI_BASE_URL")
        or os.getenv("OPENAI_API_BASE")
        or os.getenv("DEEPSEEK_BASE_URL")
        or os.getenv("OPENROUTER_BASE_URL")
        or ""
    ).rstrip("/")
    model = os.getenv("LLM_EVENT_SCORING_MODEL") or os.getenv("LANGCHAIN_MODEL_NAME") or os.getenv("DEEPSEEK_MODEL") or "deepseek-chat"
    if "deepseek" in base_url and model.startswith("deepseek-v"):
        model = "deepseek-chat"
    if not api_key or not base_url:
        return base_score, base_impact, base_confidence, "llm_not_configured"

    endpoint = f"{base_url}/chat/completions"
    prompt = (
        "You are a US equity event-driven research analyst. "
        "Judge whether this news is likely to affect the stock over the next 1-10 trading days. "
        "Return strict JSON only with keys: score, impact, confidence, reason. "
        "score must be a decimal number from -1 to 1. "
        "impact and confidence must be decimal numbers from 0 to 1. "
        "reason must be a short English string. "
        "Positive score means bullish abnormal return; negative means bearish abnormal return.\n\n"
        f"Event type: {event_type}\n"
        f"Rule score: {base_score}, rule impact: {base_impact}, rule confidence: {base_confidence}\n"
        f"Title: {title}\n"
        f"Summary: {summary[:1200]}"
    )
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": "Return strict JSON only. No markdown, no prose."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 180,
            "response_format": {"type": "json_object"},
        }
    ).encode("utf-8")
    try:
        req = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        response = json.loads(raw)
        content = response["choices"][0]["message"]["content"]
        data = _extract_json_object(content)
        if not data:
            return base_score, base_impact, base_confidence, "llm_bad_json"
        llm_score = _numeric_value(data.get("score", base_score), base_score, -1.0, 1.0)
        llm_impact = _numeric_value(data.get("impact", base_impact), base_impact, 0.0, 1.0)
        llm_confidence = _numeric_value(data.get("confidence", base_confidence), base_confidence, 0.0, 1.0)
        score = 0.45 * base_score + 0.55 * llm_score
        impact = 0.40 * base_impact + 0.60 * llm_impact
        confidence = 0.50 * base_confidence + 0.50 * llm_confidence
        return round(score, 4), round(impact, 4), round(confidence, 4), "llm_refined"
    except Exception as exc:
        return base_score, base_impact, base_confidence, f"llm_error:{type(exc).__name__}"


def make_event(symbol_row: pd.Series, item: Dict[str, str], collected_at: str, use_llm: bool = False) -> EventRow:
    text = f"{item.get('title', '')}. {item.get('summary', '')}"
    published_dt = parse_pubdate(item.get("published_at", ""))
    effective_dt = effective_time(published_dt)
    event_type = classify_event(text)
    score, impact, confidence = score_text(text)
    llm_status = "rule_only"
    if use_llm:
        score, impact, confidence, llm_status = llm_refine_event_score(
            item.get("title", ""),
            item.get("summary", ""),
            event_type,
            score,
            impact,
            confidence,
        )
    rel_score = relevance_score(
        symbol_row.symbol,
        symbol_row.company,
        item.get("title", ""),
        item.get("summary", ""),
    )
    digest = hashlib.sha1(f"{symbol_row.symbol}|{item.get('url')}|{item.get('title')}".encode()).hexdigest()[:16]
    return EventRow(
        event_id=digest,
        collected_at=collected_at,
        published_at=published_dt.isoformat(),
        effective_at=effective_dt.isoformat(),
        symbol=str(symbol_row.symbol).upper(),
        company=str(symbol_row.company),
        sector=str(symbol_row.sector),
        event_type=event_type,
        score=round(score, 4),
        sentiment_score=round(score, 4),
        impact_score=round(impact, 4),
        confidence=round(confidence, 4),
        novelty=1.0,
        relevance_score=round(rel_score, 4),
        source=item.get("source", "unknown"),
        url=item.get("url", ""),
        title=item.get("title", ""),
        summary=(f"[{llm_status}] " + item.get("summary", ""))[:500],
    )


def dedupe_events(events: List[EventRow]) -> List[EventRow]:
    seen = set()
    clean = []
    for event in events:
        title_key = re.sub(r"[^a-z0-9]+", " ", event.title.lower()).strip()
        key = (event.symbol, event.event_type, title_key[:90])
        if key in seen:
            continue
        seen.add(key)
        clean.append(event)
    return clean


def fetch_prices(symbols: Iterable[str], period: str = "3mo") -> Dict[str, pd.DataFrame]:
    unique_symbols = sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
    out = {}
    if not unique_symbols:
        return out

    try:
        raw, _sources = download_daily_history(unique_symbols, period=period)
    except Exception:
        raw = pd.DataFrame()

    if isinstance(raw, pd.DataFrame) and not raw.empty:
        for symbol in unique_symbols:
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    if symbol not in raw.columns.get_level_values(0):
                        continue
                    hist = raw[symbol].copy()
                else:
                    hist = raw.copy() if len(unique_symbols) == 1 else pd.DataFrame()
                if hist.empty:
                    continue
                hist = hist.rename(columns=str.lower).dropna(how="all")
                if "close" in hist.columns and not hist["close"].dropna().empty:
                    out[symbol] = hist
            except Exception:
                continue

    return out


def price_features(df: pd.DataFrame) -> Dict[str, float]:
    if df.empty or "close" not in df.columns:
        return {"last_price": 0.0, "ret_5d": 0.0, "ret_20d": 0.0, "volume_ratio": 0.0}
    close = df["close"].astype(float)
    volume = df.get("volume", pd.Series(0.0, index=df.index)).astype(float)
    last_price = float(close.iloc[-1])
    ret_5d = float(close.pct_change(5).iloc[-1]) if len(close) > 5 else 0.0
    ret_20d = float(close.pct_change(20).iloc[-1]) if len(close) > 20 else 0.0
    vol_base = volume.rolling(20).mean().iloc[-1] if len(volume) > 20 else volume.mean()
    volume_ratio = float(volume.iloc[-1] / vol_base) if vol_base and not math.isnan(vol_base) else 0.0
    return {
        "last_price": round(last_price, 4),
        "ret_5d": round(ret_5d, 4),
        "ret_20d": round(ret_20d, 4),
        "volume_ratio": round(volume_ratio, 4),
    }


def load_calibration(path: str) -> Dict[str, object]:
    if not path:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    model = payload.get("model") or {}
    beta = model.get("primary_beta")
    ic = model.get("primary_ic")
    horizon = model.get("primary_horizon") or "5d"
    usable = bool(model.get("usable"))
    type_betas = model.get("event_type_betas") or {}
    try:
        beta_value = float(beta)
    except Exception:
        return {}
    try:
        ic_value = float(ic)
    except Exception:
        ic_value = 0.0
    return {
        "beta": beta_value,
        "ic": ic_value,
        "horizon": str(horizon),
        "usable": 1.0 if usable else 0.0,
        "event_type_betas": type_betas if isinstance(type_betas, dict) else {},
    }


def aggregate_signals(
    events: List[EventRow],
    price_map: Dict[str, pd.DataFrame],
    top: int,
    calibration: Optional[Dict[str, object]] = None,
) -> pd.DataFrame:
    now = datetime.now(timezone.utc)
    calibration = calibration or {}
    calibration_beta = calibration.get("beta")
    calibration_ic = calibration.get("ic")
    calibration_horizon = calibration.get("horizon", "")
    event_type_betas = calibration.get("event_type_betas") or {}
    if not isinstance(event_type_betas, dict):
        event_type_betas = {}
    shrink_k = 20.0
    rows = []
    by_symbol: Dict[str, List[EventRow]] = {}
    for event in events:
        by_symbol.setdefault(event.symbol, []).append(event)

    for symbol, symbol_events in by_symbol.items():
        event_score = 0.0
        calibrated_event_abret = 0.0
        calibrated_event_count = 0
        calibration_weight_sum = 0.0
        positive = 0
        negative = 0
        for event in symbol_events:
            published = parse_pubdate(event.published_at)
            age_hours = max(0.0, (now - published).total_seconds() / 3600.0)
            decay = math.exp(-math.log(2.0) * age_hours / 36.0)
            contribution = event.score * event.impact_score * event.confidence * decay
            event_score += contribution
            type_stats = event_type_betas.get(str(event.event_type))
            type_beta = None
            if isinstance(type_stats, dict):
                try:
                    type_beta = float(type_stats.get("beta"))
                except Exception:
                    type_beta = None
                try:
                    type_n = float(type_stats.get("n", 0))
                except Exception:
                    type_n = 0.0
            else:
                type_n = 0.0
            if type_beta is None and calibration_beta is not None:
                type_beta = float(calibration_beta)
                shrink_weight = 0.0
            elif type_beta is not None and calibration_beta is not None:
                shrink_weight = max(0.0, min(1.0, type_n / (type_n + shrink_k)))
                type_beta = shrink_weight * type_beta + (1.0 - shrink_weight) * float(calibration_beta)
            else:
                shrink_weight = 0.0
            if type_beta is not None:
                calibrated_event_abret += type_beta * contribution
                calibrated_event_count += 1
                calibration_weight_sum += shrink_weight
            if event.score > 0.05:
                positive += 1
            elif event.score < -0.05:
                negative += 1

        feats = price_features(price_map.get(symbol, pd.DataFrame()))
        tech_score = max(-1.0, min(1.0, feats["ret_5d"] * 4.0 + feats["ret_20d"] * 1.5))
        volume_score = max(-0.3, min(0.7, (feats["volume_ratio"] - 1.0) / 2.0))
        launch_score = max(-1.0, min(1.0, 0.62 * event_score + 0.23 * tech_score + 0.15 * volume_score))
        expected_abret = None
        calibrated_signal = ""
        if calibration_beta is not None:
            expected_abret = calibrated_event_abret if calibrated_event_count > 0 else float(calibration_beta) * event_score
            expected_abret = max(-0.10, min(0.10, expected_abret))
            if expected_abret >= 0.01:
                calibrated_signal = "calibrated_positive"
            elif expected_abret <= -0.01:
                calibrated_signal = "calibrated_negative"
            else:
                calibrated_signal = "calibrated_neutral"
        if launch_score >= 0.45:
            signal_label = "strong_watch"
            signal_cn = "强启动观察"
        elif launch_score >= 0.18:
            signal_label = "watch"
            signal_cn = "观察"
        elif launch_score <= -0.25:
            signal_label = "avoid"
            signal_cn = "回避"
        else:
            signal_label = "neutral"
            signal_cn = "中性"
        reason_parts = []
        if event_score > 0.12:
            reason_parts.append("positive event momentum")
        elif event_score < -0.12:
            reason_parts.append("negative event pressure")
        if tech_score > 0.25:
            reason_parts.append("price trend confirmation")
        elif tech_score < -0.25:
            reason_parts.append("weak price confirmation")
        if feats["volume_ratio"] >= 1.5:
            reason_parts.append("volume expansion")
        elif feats["volume_ratio"] and feats["volume_ratio"] < 0.6:
            reason_parts.append("low relative volume")
        reason = "; ".join(reason_parts) or "mixed signal"
        rows.append(
            {
                "symbol": symbol,
                "signal": signal_label,
                "signal_cn": signal_cn,
                "launch_score": round(launch_score, 4),
                "event_score": round(event_score, 4),
                "tech_score": round(tech_score, 4),
                "expected_abret": "" if expected_abret is None else round(expected_abret, 4),
                "calibration_horizon": calibration_horizon,
                "calibration_ic": "" if calibration_ic is None else round(float(calibration_ic), 4),
                "calibration_type_coverage": calibrated_event_count,
                "calibration_shrinkage": "" if calibrated_event_count == 0 else round(calibration_weight_sum / calibrated_event_count, 4),
                "calibrated_signal": calibrated_signal,
                "last_price": feats["last_price"],
                "ret_5d": feats["ret_5d"],
                "ret_20d": feats["ret_20d"],
                "volume_ratio": feats["volume_ratio"],
                "event_count": len(symbol_events),
                "positive_events": positive,
                "negative_events": negative,
                "reason": reason,
                "top_event": max(symbol_events, key=lambda e: abs(e.score * e.impact_score)).title,
            }
        )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).sort_values("launch_score", ascending=False)
    return df.head(top).reset_index(drop=True)


def write_outputs(events: List[EventRow], signals: pd.DataFrame, output_dir: Path) -> None:
    data_dir = output_dir / "data"
    artifact_dir = output_dir / "artifacts"
    data_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    event_rows = [asdict(e) for e in events]
    events_path = data_dir / "events.csv"
    with events_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(EventRow.__dataclass_fields__.keys()))
        writer.writeheader()
        writer.writerows(event_rows)

    signals_path = artifact_dir / "signals.csv"
    signals.to_csv(signals_path, index=False, encoding="utf-8")

    report = {
        "generated_at": utc_now_iso(),
        "event_count": len(events),
        "signal_count": int(len(signals)),
        "signals": signals.to_dict(orient="records") if not signals.empty else [],
    }
    (artifact_dir / "event_driven_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        "# S&P 500 事件驱动短期启动信号报告",
        "",
        f"生成时间: {report['generated_at']}",
        f"有效事件数: {len(events)}",
        "",
        "说明: 本报告用于研究参考。信号来自新闻事件动量、价格趋势确认与成交量确认，不等同于交易建议或实盘胜率。",
        "",
        "## 启动观察名单",
        "",
    ]
    if signals.empty:
        lines.append("没有生成候选标的。请检查网络、放宽相关新闻阈值，或扩大回看窗口。")
    else:
        has_calibration = "expected_abret" in signals.columns and signals["expected_abret"].astype(str).ne("").any()
        if has_calibration:
            lines.append("| 排名 | 代码 | 信号 | 启动分 | 估计超额 | 校准IC | 现价 | 5日 | 20日 | 量能/20日 | 事件数 | 核心理由 | 关键事件 |")
            lines.append("|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|")
        else:
            lines.append("| 排名 | 代码 | 信号 | 启动分 | 现价 | 5日 | 20日 | 量能/20日 | 事件数 | 核心理由 | 关键事件 |")
            lines.append("|---:|---|---|---:|---:|---:|---:|---:|---:|---|---|")
        for i, row in signals.iterrows():
            if has_calibration:
                expected = row.expected_abret
                expected_text = "" if expected == "" else f"{float(expected):.2%}"
                ic = row.calibration_ic
                ic_text = "" if ic == "" else f"{float(ic):.3f}"
                lines.append(
                    f"| {i + 1} | {row.symbol} | {row.signal_cn} | {row.launch_score:.2f} | "
                    f"{expected_text} | {ic_text} | {row.last_price:.2f} | "
                    f"{row.ret_5d:.1%} | {row.ret_20d:.1%} | {row.volume_ratio:.2f} | "
                    f"{int(row.event_count)} | {row.reason} | {row.top_event[:90]} |"
                )
            else:
                lines.append(
                    f"| {i + 1} | {row.symbol} | {row.signal_cn} | {row.launch_score:.2f} | "
                    f"{row.last_price:.2f} | {row.ret_5d:.1%} | {row.ret_20d:.1%} | "
                    f"{row.volume_ratio:.2f} | {int(row.event_count)} | {row.reason} | {row.top_event[:90]} |"
                )
        lines.extend(
            [
                "",
                "## 评分解释",
                "",
                "- 启动分: 事件动量 62% + 价格趋势确认 23% + 成交量确认 15%。",
                "- 强启动观察: 分数 >= 0.45；观察: 0.18 到 0.45；回避: <= -0.25。",
                "- 估计超额: 若提供历史校准文件，则按 `Beta * 当前事件动量` 估算主窗口超额收益。",
                "- 新闻事件按发布时间进行半衰期衰减，越新的高相关事件权重越高。",
            ]
        )
    (artifact_dir / "event_driven_report.md").write_text("\n".join(lines), encoding="utf-8")


def collect_symbol_events(
    row_obj: object,
    args: argparse.Namespace,
    cache_dir: Optional[Path],
    cutoff: datetime,
    collected_at: str,
) -> Tuple[List[EventRow], Optional[str]]:
    local_events: List[EventRow] = []
    symbol = getattr(row_obj, "symbol")
    try:
        items = news_items_with_fallback(
            symbol,
            str(getattr(row_obj, "company", symbol) or symbol),
            args.news_per_ticker,
            cache_dir=cache_dir,
            cache_ttl_minutes=args.cache_ttl_minutes,
        )
    except Exception as exc:
        return [], f"news fetch failed for {symbol}: {exc}"

    for item in items:
        published = parse_pubdate(item.get("published_at", ""))
        if published < cutoff:
            continue
        event = make_event(row_obj, item, collected_at, use_llm=False)
        if event.relevance_score < args.min_relevance:
            continue
        if bool(getattr(args, "llm_score", False)):
            event = make_event(row_obj, item, collected_at, use_llm=True)
        local_events.append(event)
    if args.sleep > 0:
        time.sleep(args.sleep)
    return local_events, None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=".")
    parser.add_argument("--tickers", default="")
    parser.add_argument("--universe-csv", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--news-per-ticker", type=int, default=5)
    parser.add_argument("--lookback-hours", type=int, default=96)
    parser.add_argument("--min-relevance", type=float, default=0.45)
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--no-price", action="store_true")
    parser.add_argument("--cache-dir", default="")
    parser.add_argument("--cache-ttl-minutes", type=int, default=30)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--calibration-json", default="")
    parser.add_argument("--llm-score", action="store_true")
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir)
    cache_dir = Path(args.cache_dir) if args.cache_dir else output_dir / "cache" / "rss"
    universe = load_universe(args, cache_dir=cache_dir.parent)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.lookback_hours)
    collected_at = utc_now_iso()

    events: List[EventRow] = []
    rows = [SimpleNamespace(**row._asdict()) for row in universe.itertuples(index=False)]
    workers = max(1, min(args.workers, 32))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(collect_symbol_events, row, args, cache_dir, cutoff, collected_at): row.symbol
            for row in rows
        }
        for future in concurrent.futures.as_completed(future_map):
            local_events, error = future.result()
            if error:
                print(error, file=sys.stderr)
            events.extend(local_events)

    events = dedupe_events(events)
    symbols = sorted({event.symbol for event in events})
    price_map = {} if args.no_price else fetch_prices(symbols)
    calibration = load_calibration(args.calibration_json)
    signals = aggregate_signals(events, price_map, args.top, calibration=calibration)
    write_outputs(events, signals, output_dir)
    print(json.dumps({"events": len(events), "signals": len(signals), "output_dir": str(output_dir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
