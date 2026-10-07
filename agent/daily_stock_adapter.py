"""Read-only adapter for the migrated daily-stock-analysis research database."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "daily_stock_analysis" / "data" / "stock_analysis.db"
DB_PATH = Path(os.environ.get("DAILY_STOCK_DB_PATH", str(DEFAULT_DB_PATH)))
_SYMBOL_RE = re.compile(r"^[A-Z0-9.-]{1,16}$")
_SUPPORTED_REMOTE_EXCHANGES = {"NMS", "NYQ", "NYS", "ASE", "NGM", "NCM", "PCX", "BTS", "PNK", "HKG", "SHH", "SHZ"}
_SEARCH_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"daily stock analysis database not found: {DB_PATH}")
    connection = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def _normalize_symbol(value: str) -> str:
    symbol = str(value or "").strip().upper().replace(" ", "")
    if not _SYMBOL_RE.fullmatch(symbol):
        raise ValueError("股票代码格式不正确")
    return symbol


def _json_object(value: Any) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
        return payload if isinstance(payload, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _number(value: Any) -> float | None:
    try:
        return round(float(value), 4) if value is not None else None
    except (TypeError, ValueError):
        return None


def _local_search(query: str, limit: int) -> list[dict[str, Any]]:
    like = f"%{query}%"
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT code, MAX(COALESCE(name, '')) AS name, MAX(created_at) AS latest_at, COUNT(*) AS report_count
            FROM analysis_history
            WHERE UPPER(code) LIKE UPPER(?) OR name LIKE ?
            GROUP BY code
            ORDER BY
              CASE WHEN UPPER(code) = UPPER(?) THEN 0
                   WHEN UPPER(code) LIKE UPPER(?) THEN 1
                   WHEN name = ? THEN 2 ELSE 3 END,
              MAX(created_at) DESC
            LIMIT ?
            """,
            (like, like, query, f"{query}%", query, limit),
        ).fetchall()
    return [
        {
            "symbol": row["code"],
            "name": row["name"] or row["code"],
            "exchange": "历史分析库",
            "source": "local",
            "has_history": True,
            "report_count": row["report_count"],
            "latest_at": row["latest_at"],
        }
        for row in rows
    ]


def _remote_search(query: str, limit: int) -> list[dict[str, Any]]:
    cache_key = query.lower()
    cached = _SEARCH_CACHE.get(cache_key)
    if cached and time.time() - cached[0] < 300:
        return cached[1][:limit]
    url = "https://query2.finance.yahoo.com/v1/finance/search?" + urlencode(
        {"q": query, "quotesCount": limit, "newsCount": 0, "listsCount": 0}
    )
    try:
        request = Request(url, headers={"User-Agent": "Mozilla/5.0 Vibe-Trading/0.1"})
        with urlopen(request, timeout=4) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        rows = []
        for quote in payload.get("quotes", []):
            symbol = str(quote.get("symbol") or "").upper()
            exchange = str(quote.get("exchange") or quote.get("exchDisp") or "")
            if (
                quote.get("quoteType") not in {"EQUITY", "ETF"}
                or exchange not in _SUPPORTED_REMOTE_EXCHANGES
                or not _SYMBOL_RE.fullmatch(symbol)
            ):
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "name": quote.get("shortname") or quote.get("longname") or symbol,
                    "exchange": exchange,
                    "source": "yahoo",
                    "has_history": False,
                }
            )
        _SEARCH_CACHE[cache_key] = (time.time(), rows)
        return rows[:limit]
    except Exception:
        return []


def search_symbols(query: str, limit: int = 8) -> list[dict[str, Any]]:
    query = str(query or "").strip()
    if not query:
        return recent_symbols(limit)
    local = _local_search(query, limit)
    # Keep autocomplete responsive for known symbols. Yahoo is a fallback for
    # discovery, not a blocking enrichment step for records already in the DB.
    return local if local else _remote_search(query, limit)


def recent_symbols(limit: int = 10) -> list[dict[str, Any]]:
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT code, MAX(COALESCE(name, '')) AS name, MAX(created_at) AS latest_at, COUNT(*) AS report_count
            FROM analysis_history GROUP BY code ORDER BY MAX(created_at) DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "symbol": row["code"],
            "name": row["name"] or row["code"],
            "latest_at": row["latest_at"],
            "report_count": row["report_count"],
        }
        for row in rows
    ]


def _technical_snapshot(rows: list[sqlite3.Row]) -> dict[str, Any]:
    if not rows:
        return {"available": False, "note": "数据库中暂无行情序列。"}
    closes = [float(row["close"]) for row in rows if row["close"] is not None]
    volumes = [float(row["volume"]) for row in rows if row["volume"] is not None]
    if not closes:
        return {"available": False, "note": "行情序列缺少收盘价。"}
    last = rows[-1]
    previous = closes[-2] if len(closes) > 1 else closes[-1]
    ma = lambda period: round(sum(closes[-period:]) / min(period, len(closes)), 4)
    avg_volume = sum(volumes[-6:-1]) / len(volumes[-6:-1]) if len(volumes) > 1 else 0
    volume_ratio = volumes[-1] / avg_volume if avg_volume and volumes else None
    ma5, ma10, ma20 = ma(5), ma(10), ma(20)
    if closes[-1] > ma5 > ma10 > ma20:
        trend = "多头排列"
    elif closes[-1] < ma5 < ma10 < ma20:
        trend = "空头排列"
    else:
        trend = "区间整理"
    return {
        "available": True,
        "as_of": str(last["date"]),
        "source": last["data_source"] or "unknown",
        "close": round(closes[-1], 4),
        "change_pct": round((closes[-1] / previous - 1) * 100, 2) if previous else None,
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "volume_ratio": round(volume_ratio, 2) if volume_ratio is not None else None,
        "trend": trend,
        "history": [{"date": str(row["date"]), "close": _number(row["close"])} for row in rows if row["close"] is not None],
    }


def _days_old(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return max(0, (datetime.now() - datetime.fromisoformat(value)).days)
    except ValueError:
        return None


def get_analysis(symbol: str) -> dict[str, Any]:
    symbol = _normalize_symbol(symbol)
    with _connect() as connection:
        analyses = connection.execute(
            "SELECT * FROM analysis_history WHERE UPPER(code) = ? ORDER BY created_at DESC LIMIT 10",
            (symbol,),
        ).fetchall()
        prices = connection.execute(
            "SELECT date, close, volume, data_source FROM stock_daily WHERE UPPER(code) = ? ORDER BY date DESC LIMIT 60",
            (symbol,),
        ).fetchall()
    prices = list(reversed(prices))
    technical = _technical_snapshot(prices)
    if not analyses:
        return {
            "symbol": symbol,
            "has_analysis": False,
            "technical": technical,
            "warning": "该标的尚未生成历史 AI 报告。搜索建议仅用于定位标的，不代表已完成研究。",
        }
    latest = dict(analyses[0])
    raw = _json_object(latest.pop("raw_result", ""))
    age = _days_old(latest.get("created_at"))
    stale = age is None or age > 4
    warning = "历史报告可能已过期，请结合最新行情和公告重新复核。" if stale else "报告来自迁移数据库，仅作为研究参考，不构成实时下单指令。"
    levels = {
        "ideal_buy": _number(latest.get("ideal_buy")),
        "secondary_buy": _number(latest.get("secondary_buy")),
        "stop_loss": _number(latest.get("stop_loss")),
        "take_profit": _number(latest.get("take_profit")),
    }
    dashboard = raw.get("dashboard") if isinstance(raw.get("dashboard"), dict) else {}
    summary = str(latest.get("analysis_summary") or "")
    advice = str(latest.get("operation_advice") or "")
    confidence = str(raw.get("confidence_level") or "")
    review_flags = []
    if "持有" in advice and any(term in summary for term in ("买入信号", "建议买入", "分批入场")):
        review_flags.append("摘要中的买入措辞与结构化操作建议不一致，请以人工复核后的结论为准。")
    if confidence in {"低", "Low", "low"} and int(latest.get("sentiment_score") or 0) >= 70:
        review_flags.append("评分较高但模型置信度较低，不宜仅依据评分采取行动。")
    if technical.get("trend") == "区间整理" and str(latest.get("trend_prediction") or "") in {"看多", "Bullish"}:
        review_flags.append("历史趋势判断与当前已保存行情的均线结构存在差异。")
    observed_levels = [levels[key] for key in ("stop_loss", "ideal_buy", "secondary_buy", "take_profit")]
    if all(value is not None for value in observed_levels) and observed_levels != sorted(observed_levels):
        review_flags.append("价格计划层级顺序异常，请重新核对观察价、止损位和止盈位。")
    return {
        "symbol": symbol,
        "name": latest.get("name") or symbol,
        "has_analysis": True,
        "report": {
            "id": latest.get("id"),
            "created_at": latest.get("created_at"),
            "age_days": age,
            "stale": stale,
            "report_type": latest.get("report_type"),
            "sentiment_score": latest.get("sentiment_score"),
            "trend_prediction": latest.get("trend_prediction"),
            "operation_advice": latest.get("operation_advice"),
            "analysis_summary": latest.get("analysis_summary"),
            "levels": levels,
            "confidence_level": raw.get("confidence_level"),
            "risk_warning": raw.get("risk_warning"),
            "key_points": raw.get("key_points"),
            "buy_reason": raw.get("buy_reason"),
            "data_sources": raw.get("data_sources"),
            "model_used": raw.get("model_used"),
            "dashboard": dashboard,
        },
        "technical": technical,
        "history": [
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "score": row["sentiment_score"],
                "advice": row["operation_advice"],
                "trend": row["trend_prediction"],
            }
            for row in analyses
        ],
        "review_flags": review_flags,
        "warning": warning,
    }
