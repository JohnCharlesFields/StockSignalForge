"""Shared research-universe catalog and cached constituent resolution."""

from __future__ import annotations

import threading
import time
from typing import Any


RESEARCH_UNIVERSE_GROUPS = [
    {"group": "broad", "label": "01 美股宽基指数池", "priority": 10},
    {"group": "growth", "label": "02 科技成长指数池", "priority": 20},
    {"group": "size", "label": "03 市值风格指数池", "priority": 30},
    {"group": "sector", "label": "04 行业指数池", "priority": 40},
    {"group": "custom", "label": "05 自选池", "priority": 50},
]
_GROUP_PRIORITY = {item["group"]: item["priority"] for item in RESEARCH_UNIVERSE_GROUPS}


def _universe(
    universe: str,
    label: str,
    tier: str,
    group: str,
    priority: int,
    purpose: str,
    *,
    run_cost: str = "中",
    recommended: bool = False,
) -> dict[str, Any]:
    return {
        "universe": universe,
        "label": label,
        "tier": tier,
        "group": group,
        "group_label": next(item["label"] for item in RESEARCH_UNIVERSE_GROUPS if item["group"] == group),
        "priority": priority,
        "purpose": purpose,
        "run_cost": run_cost,
        "recommended": recommended,
    }


# User-facing catalog. Keep it aligned with the current research workflow:
# all old thematic/legacy pools are removed from selectors; only these ten
# index-based pools are exposed to the platform.
RESEARCH_UNIVERSE_CATALOG = sorted([
    _universe("spx", "标普 500 指数 / S&P 500 Index", "SPX", "broad", 10, "美国 500 家大型上市公司；最重要的美股大盘基准，用于判断市场整体强弱。", run_cost="高", recommended=True),
    _universe("ixic", "纳斯达克综合指数 / Nasdaq Composite Index", "COMP / IXIC", "growth", 10, "纳斯达克交易所上市的大量股票，科技股占比较高；用于观察成长股整体走势。", run_cost="高", recommended=True),
    _universe("djia", "道琼斯工业平均指数 / Dow Jones Industrial Average", "DJIA / DJI", "broad", 20, "30 家美国大型蓝筹公司；用于观察传统蓝筹股和市场情绪。", run_cost="低"),
    _universe("ndx", "纳斯达克 100 指数 / Nasdaq-100 Index", "NDX", "growth", 20, "纳斯达克上市的 100 家大型非金融企业；用于观察大型科技成长股，常与 QQQ 对应。", run_cost="中", recommended=True),
    _universe("rut", "罗素 2000 指数 / Russell 2000 Index", "RUT", "size", 10, "约 2,000 家美国小盘股；用于观察小盘股行情和市场风险偏好。", run_cost="高"),
    _universe("rua", "罗素 3000 指数 / Russell 3000 Index", "RUA", "broad", 30, "覆盖美国大盘、中盘和小盘股；用于判断上涨是否扩散至整个美股市场。", run_cost="高"),
    _universe("mid", "标普中盘 400 指数 / S&P MidCap 400 Index", "MID", "size", 20, "美国中盘股；用于寻找规模适中、仍有成长空间的公司。", run_cost="高"),
    _universe("sml", "标普小盘 600 指数 / S&P SmallCap 600 Index", "SML", "size", 30, "美国小盘股，包含流动性和财务可行性筛选；用于构建质量相对较高的小盘股股票池。", run_cost="高"),
    _universe("w5000", "威尔希尔 5000 全市场指数 / FT Wilshire 5000 Index", "W5000", "broad", 40, "尽可能覆盖具有可获得价格的美国股票；用于观察美国股市全市场表现。", run_cost="高"),
    _universe("sox", "费城半导体指数 / PHLX Semiconductor Sector Index", "SOX", "sector", 10, "半导体设计、制造、设备、分销等产业链公司；用于观察芯片、AI 硬件和英伟达产业链行情。", run_cost="中", recommended=True),
    _universe("watchlist", "自选池 / Watchlist", "WATCH", "custom", 10, "用户手动维护的研究股票池；Alpha 基准默认按纳斯达克/QQQ 计算，来源池显示为自选池。", run_cost="低", recommended=True),
], key=lambda item: (_GROUP_PRIORITY[item["group"]], int(item["priority"])))

_SUPPORTED = {item["universe"] for item in RESEARCH_UNIVERSE_CATALOG}
_CACHE_TTL_SECONDS = 15 * 60
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def list_research_universes() -> list[dict[str, Any]]:
    out = [dict(item) for item in RESEARCH_UNIVERSE_CATALOG]
    try:
        from app_database import user_watchlist_symbols

        count = len(user_watchlist_symbols())
        for item in out:
            if item.get("universe") == "watchlist":
                item["ticker_count"] = count
                item["watchlist_count"] = count
                item["recent_win"] = {
                    "status": "ready" if count else "empty",
                    "candidate_count": count,
                }
                break
    except Exception:
        pass
    return out


def list_research_universe_groups() -> list[dict[str, Any]]:
    return [dict(item) for item in RESEARCH_UNIVERSE_GROUPS]


def _source_audit(source: str, ticker_count: int) -> dict[str, Any]:
    parts = [part for part in str(source or "").split(";") if part]
    primary = parts[0] if parts else ""
    snapshot = next((part.split(":", 1)[1] for part in parts if part.startswith("snapshot:")), "")
    source_kind = "unknown"
    if primary.startswith("live:"):
        source_kind = "live"
    elif primary.startswith("archive:"):
        source_kind = "archive"
        snapshot = primary.split(":", 1)[1]
    elif primary.startswith("fallback:"):
        source_kind = "fallback"
    elif primary == "custom":
        source_kind = "custom"
    elif primary.startswith("curated:"):
        source_kind = "curated"
    return {
        "source": source,
        "source_kind": source_kind,
        "is_live": source_kind == "live",
        "is_archive": source_kind == "archive" or bool(snapshot),
        "is_fallback": source_kind == "fallback",
        "snapshot_path": snapshot,
        "ticker_count": ticker_count,
        "bias_warning": (
            "fallback 成分股只适合当前研究或离线兜底；历史回测应使用 archive snapshot，避免用今天成分回看过去。"
            if source_kind == "fallback"
            else ""
        ),
        "audit_note": "股票池来源已显式标注；历史研究优先使用 snapshot_date / archive_only。",
    }


def resolve_research_universe(universe: str, refresh: bool = False) -> dict[str, Any]:
    key = str(universe or "spx").strip().lower()
    if key not in _SUPPORTED:
        raise ValueError(f"unsupported universe: {key}")

    if key == "watchlist":
        from app_database import user_watchlist_symbols

        tickers = user_watchlist_symbols()
        source = "custom:watchlist"
        return {
            "universe": key,
            "tickers": tickers,
            "ticker_count": len(tickers),
            "source": source,
            "source_audit": _source_audit(source, len(tickers)),
            "memberships": {ticker: ["自选池"] for ticker in tickers},
            "benchmark": "QQQ",
            "benchmark_source_universe": "watchlist",
        }

    now = time.time()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and not refresh and now - cached[0] < _CACHE_TTL_SECONDS:
            return dict(cached[1])

    from scripts.screening_framework_v2_optimized import resolve_universe

    tickers, source, memberships = resolve_universe(key)
    payload = {
        "universe": key,
        "tickers": list(tickers),
        "ticker_count": len(tickers),
        "source": source,
        "source_audit": _source_audit(source, len(tickers)),
        "memberships": memberships,
    }
    with _cache_lock:
        _cache[key] = (now, payload)
    return dict(payload)
