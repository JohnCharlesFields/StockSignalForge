"""Read-only 恒生聚源（Glidata）MCP shadow samples for US equity research data.

Only an explicit batch probe calls MCP. Normal page reads consume the small,
validated cache, and no shadow field feeds a historical calibration model.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_MAX_RESPONSE_BYTES = 2_000_000
_MAX_TABLE_ROWS = 5_000


def _root() -> Path:
    return Path(os.getenv("VIBE_MARKET_DATA_CACHE_DIR", "/app/agent/data_cache/market_data")) / "gildata_shadow"


def _safe_symbol(symbol: str) -> str:
    value = str(symbol or "").strip().upper()
    if not _SYMBOL.fullmatch(value):
        raise ValueError("invalid US equity symbol")
    return value


def _date(value: str) -> str:
    return datetime.strptime(value, "%Y-%m-%d").date().isoformat()


def _number(value: Any) -> float | None:
    try:
        parsed = float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _integer(value: Any) -> int | None:
    parsed = _number(value)
    return int(parsed) if parsed is not None and parsed >= 0 and parsed.is_integer() else None


def _parse_markdown_table(markdown: str) -> list[dict[str, str]]:
    """Parse the MCP's table_markdown field as a delimited table, not free text."""
    reader = csv.reader(io.StringIO(markdown or ""), delimiter="|")
    headers: list[str] | None = None
    rows: list[dict[str, str]] = []
    for fields in reader:
        cells = [field.strip() for field in fields]
        if cells and not cells[0]:
            cells.pop(0)
        if cells and not cells[-1]:
            cells.pop()
        if not cells:
            continue
        if headers is None:
            headers = cells
            continue
        if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        if len(cells) != len(headers):
            continue
        rows.append(dict(zip(headers, cells)))
        if len(rows) >= _MAX_TABLE_ROWS:
            break
    return rows


def _mcp_payload(body: str) -> dict[str, Any]:
    if body.lstrip().startswith("data:") or body.lstrip().startswith("event:"):
        messages = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
        body = next((message for message in reversed(messages) if message.startswith("{")), "")
    envelope = json.loads(body)
    if envelope.get("error"):
        raise ValueError("mcp_rpc_error")
    content = (envelope.get("result") or {}).get("content") or []
    text = next((item.get("text") for item in content if item.get("type") == "text"), None)
    payload = json.loads(text) if text else {}
    if payload.get("code") != 0 or not isinstance(payload.get("results"), list):
        raise ValueError("mcp_query_failed")
    return payload


def _finquery(query: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    token = os.getenv("GILDATA_MCP_TOKEN", "").strip()
    if not token:
        raise ValueError("gildata_token_missing")
    mcp_url = os.getenv("GILDATA_MCP_URL", "").strip()
    if not mcp_url.startswith("https://"):
        raise ValueError("gildata_mcp_url_missing_or_invalid")
    started = time.monotonic()
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "FinQuery", "arguments": {"query": query}},
    }
    for attempt in range(2):
        try:
            response = requests.post(
                mcp_url,
                params={"token": token},
                json=body,
                headers={"Accept": "application/json, text/event-stream"},
                timeout=60,
                stream=True,
            )
            try:
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_content(chunk_size=65536):
                    size += len(chunk)
                    if size > _MAX_RESPONSE_BYTES:
                        raise ValueError("mcp_response_too_large")
                    chunks.append(chunk)
                payload = _mcp_payload(b"".join(chunks).decode("utf-8"))
            finally:
                response.close()
            return payload["results"], {
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "response_bytes": size,
                "result_tables": len(payload["results"]),
                "attempts": attempt + 1,
            }
        except (requests.ConnectionError, requests.Timeout):
            if attempt:
                raise
            time.sleep(0.5)
    raise RuntimeError("unreachable")


def _table(results: list[dict[str, Any]], api_name: str) -> list[dict[str, str]]:
    for result in results:
        if result.get("api_name") == api_name:
            return _parse_markdown_table(str(result.get("table_markdown") or ""))
    return []


def _matching(rows: list[dict[str, str]], symbol_key: str, symbol: str, date_key: str, as_of: str) -> dict[str, str]:
    return next(
        (row for row in rows if row.get(symbol_key, "").upper() == symbol and row.get(date_key) == as_of),
        {},
    )


def normalize_equity(symbol: str, as_of: str, facts: list[dict[str, Any]], ratings: list[dict[str, Any]]) -> dict[str, Any]:
    """Accept only exact-symbol, exact-date, USD observations."""
    symbol, as_of = _safe_symbol(symbol), _date(as_of)
    daily = _matching(_table(facts, "美股日行情"), "证券代码", symbol, "交易日期", as_of)
    valuation = _matching(_table(facts, "美股价值分析"), "证券代码", symbol, "交易日", as_of)
    target = next(
        (row for row in _table(facts, "美股盈利预测")
         if row.get("股票代码", "").upper() == symbol and row.get("截止日期") == as_of
         and row.get("预测指标") == "目标价" and row.get("预测数据币种") in {"美元", "USD"}),
        {},
    )
    rating = _matching(_table(ratings, "美股机构评级"), "股票代码", symbol, "截止日期", as_of)

    out: dict[str, Any] = {"symbol": symbol, "as_of": as_of, "source": "gildata:FinQuery", "shadow_only": True}
    market_cap_daily = _number(daily.get("总市值(万元)"))
    market_cap_valuation = _number(valuation.get("总市值(亿元)"))
    if daily.get("币种") == "USD" and market_cap_daily is not None:
        cap_usd = market_cap_daily * 10_000
        if market_cap_valuation is not None and cap_usd > 0:
            mismatch = abs(market_cap_valuation * 100_000_000 / cap_usd - 1)
            if mismatch <= 0.02:
                out["market_cap_usd"] = round(cap_usd)
            else:
                out["market_cap_warning"] = "daily_valuation_unit_mismatch"
        else:
            out["market_cap_warning"] = "valuation_crosscheck_missing"
    elif daily:
        out["market_cap_warning"] = "daily_currency_not_usd"
    if valuation:
        out["pe"] = _number(valuation.get("市盈率PE"))
        out["pb"] = _number(valuation.get("市净率PB(MRQ)"))
        out["ps"] = _number(valuation.get("市销率PS"))
    if target:
        out["target_avg_usd"] = _number(target.get("预测值平均数"))
        out["target_count"] = _integer(target.get("预测次数(次)"))
        out["target_window_days"] = _integer(target.get("统计周期(天)"))
    if rating:
        fields = {
            "buy": "买入评级机构数(个)", "overweight": "增持评级机构数(个)",
            "neutral": "中性评级机构数(个)", "underweight": "减持评级机构数(个)",
            "sell": "卖出评级机构数(个)",
        }
        counts = {key: _integer(rating.get(label)) for key, label in fields.items()}
        total = _integer(rating.get("评级机构总数(个)"))
        if total is not None and all(value is not None for value in counts.values()) and sum(counts.values()) == total:
            out["ratings"] = {**counts, "total": total, "window_days": _integer(rating.get("统计周期(天)"))}
        else:
            out["rating_warning"] = "category_total_mismatch"
    out["available_fields"] = [name for name in ("market_cap_usd", "pe", "pb", "ps", "target_avg_usd", "ratings") if out.get(name) is not None]
    return out


def normalize_vix(as_of: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    as_of = _date(as_of)
    row = _matching(_table(results, "指数日行情"), "指数代码", "VIX", "交易日", as_of)
    value = _number(row.get("收盘价(点)"))
    return {
        "as_of": as_of, "value": value if value is not None and 0 < value < 150 else None,
        "source": "gildata:VIX_daily", "shadow_only": True,
    }


def _write_sample(name: str, payload: dict[str, Any]) -> None:
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def cached_vix(required_as_of: str) -> dict[str, Any] | None:
    """Cache-only fallback; never invokes MCP from a page-read path."""
    if os.getenv("GILDATA_VIX_FALLBACK", "0") != "1":
        return None
    try:
        payload = json.loads((_root() / "VIX.json").read_text(encoding="utf-8"))
        value = _number(payload.get("value"))
        if payload.get("as_of") == _date(required_as_of) and value is not None and 0 < value < 150:
            return payload
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return None


def cached_equity(symbol: str) -> dict[str, Any] | None:
    """Return a normalized shadow sample without making a paid MCP call."""
    try:
        safe = _safe_symbol(symbol)
        payload = json.loads((_root() / f"{safe}.json").read_text(encoding="utf-8"))
        if payload.get("symbol") == safe and payload.get("source") == "gildata:FinQuery":
            _date(str(payload.get("as_of")))
            return payload
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    return None


def probe(symbols: list[str], as_of: str) -> dict[str, Any]:
    """Make bounded, explicit shadow calls and save only normalized samples."""
    as_of = _date(as_of)
    symbols = list(dict.fromkeys(_safe_symbol(symbol) for symbol in symbols))
    if len(symbols) > 5:
        raise ValueError("pilot is limited to five symbols")
    if not os.getenv("GILDATA_MCP_TOKEN", "").strip():
        raise ValueError("gildata_token_missing")
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "as_of": as_of,
        "source": "gildata:FinQuery", "shadow_only": True,
        "symbols": {}, "vix": None, "requests": [], "estimated_provider_cost": None,
    }
    for symbol in symbols:
        observations: dict[str, list[dict[str, Any]]] = {}
        queries = {
            "facts": f"查询美股{symbol}在{as_of}的美元计价日行情总市值、市盈率PE、市净率PB、市销率PS、分析师平均目标价；只需该日和该股票",
            "ratings": f"查询美股机构评级：股票代码{symbol}，截止日期只要{as_of}，返回这一天买入、增持、中性、减持、卖出评级机构数；不要其他股票和日期",
        }
        for kind, query in queries.items():
            try:
                observations[kind], stats = _finquery(query)
                report["requests"].append({"symbol": symbol, "kind": kind, "status": "ok", **stats})
            except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
                observations[kind] = []
                report["requests"].append({"symbol": symbol, "kind": kind, "status": type(exc).__name__})
        sample = normalize_equity(symbol, as_of, observations["facts"], observations["ratings"])
        report["symbols"][symbol] = sample
        if sample["available_fields"]:
            _write_sample(symbol, sample)
    try:
        results, stats = _finquery(f"仅查询美国CBOE VIX波动率指数在{as_of}的日收盘点位；不要其他日期")
        report["vix"] = normalize_vix(as_of, results)
        report["requests"].append({"symbol": "VIX", "kind": "daily_close", "status": "ok", **stats})
        if report["vix"]["value"] is not None:
            _write_sample("VIX", report["vix"])
    except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
        report["requests"].append({"symbol": "VIX", "kind": "daily_close", "status": type(exc).__name__})
    _write_sample("latest_report", report)
    return report


def compare_with_existing(report: dict[str, Any]) -> dict[str, Any]:
    """Collect source-side comparisons without changing production readings."""
    from fincept_adapters import get_cboe_vix_latest
    from market_data_service import get_analyst_view, get_ticker_reference

    comparisons: dict[str, Any] = {}
    for symbol, sample in report.get("symbols", {}).items():
        comparison: dict[str, Any] = {}
        try:
            fmp = get_analyst_view(symbol)
            comparison["analyst"] = {
                "fmp_as_of": fmp.get("as_of"),
                "fmp_rating_counts": {key: fmp.get(key) for key in ("strong_buy", "buy", "hold", "sell", "strong_sell")},
                "fmp_target_avg_quarter": fmp.get("target_avg_quarter"),
                "gildata_target_avg_100d": sample.get("target_avg_usd"),
                "comparable": False,
                "reason": "rating_taxonomy_and_target_windows_differ",
            }
        except Exception as exc:
            comparison["analyst_error"] = type(exc).__name__
        try:
            reference = get_ticker_reference(symbol)
            massive_cap = _number(reference.get("market_cap"))
            gildata_cap = _number(sample.get("market_cap_usd"))
            comparison["market_cap"] = {
                "massive_usd": massive_cap,
                "gildata_usd": gildata_cap,
                "relative_difference": round(gildata_cap / massive_cap - 1, 4) if massive_cap and gildata_cap else None,
                "comparable": False,
                "reason": "massive_reference_as_of_not_available",
            }
        except Exception as exc:
            comparison["market_cap_error"] = type(exc).__name__
        comparisons[symbol] = comparison
    try:
        cboe = get_cboe_vix_latest("VIX")
        cboe_date = str(cboe.get("as_of_date") or "")
        try:
            cboe_date = datetime.strptime(cboe_date, "%m/%d/%Y").date().isoformat()
        except ValueError:
            pass
        gildata_vix = report.get("vix") or {}
        comparisons["VIX"] = {
            "cboe_as_of": cboe_date,
            "cboe_close": _number(cboe.get("value")),
            "gildata_as_of": gildata_vix.get("as_of"),
            "gildata_close": gildata_vix.get("value"),
            "same_day": bool(cboe_date and cboe_date == gildata_vix.get("as_of")),
        }
        if comparisons["VIX"]["same_day"] and comparisons["VIX"]["cboe_close"] is not None and gildata_vix.get("value") is not None:
            comparisons["VIX"]["absolute_difference"] = round(abs(comparisons["VIX"]["cboe_close"] - gildata_vix["value"]), 4)
    except Exception as exc:
        comparisons["VIX"] = {"error": type(exc).__name__}
    report["comparisons"] = comparisons
    _write_sample("latest_report", report)
    return report
