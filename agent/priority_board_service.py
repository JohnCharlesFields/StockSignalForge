"""Composite priority board: the single 'best available' ranking for the UI.

R1 calibration (spx, 56k events) proved the raw launch_score / daily_tunnel are
*inverted* against net-of-cost excess returns -- high scores underperform their
own baseline.  So this board deliberately does NOT use the raw launch_score as a
positive driver.  It ranks the latest home-dashboard candidates by a composite:

    priority = 0.45 * relative_strength(vs SPY, 20d, normalized)
             + 0.35 * R1 calibrated probability (which corrects the inversion)
             + 0.20 * R3 portfolio gross-exposure multiplier (market regime)

plus an honest ``confidence_badge`` derived from the active calibration's data
quality and whether its buckets differentiate at all.  With today's data the
badge is ``low_edge`` -- the UI must show that, not fake confidence.

Reuses existing pieces: ``latest_home_dashboard_snapshot`` (candidate source),
``signal_calibration.calibrate`` (honest probability), ``get_daily_history``
(SPY benchmark), and the per-row ``portfolio_gross_exposure_multiplier`` that the
home dashboard already computed from R3.  Keep ASCII for code; UI strings may be
Chinese like the rest of the services.
"""

from __future__ import annotations

import math
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

from app_database import (
    cache_get,
    cache_set,
    latest_home_dashboard_snapshot,
    priority_candidate_recent,
    priority_candidate_slices_for_date,
    signal_calibration_active,
)
from market_data_service import download_daily_history, get_daily_history, get_next_earnings, external_data_allowed, external_data_scope
from signal_calibration import calibrate
from iv_signal_service import iv_features
from pullback_parameter_service import ranking_settings

CACHE_KEY = "priority_board:v23"
MAX_SCORED_CANDIDATES = 200
# Relative-strength conviction gate (2026-06-30 conditioning backtest). Default
# on; set PRIORITY_RS_CONVICTION_GATE=0 to revert to calibrated-prob-only order.
_RS_GATE = os.getenv("PRIORITY_RS_CONVICTION_GATE", "1").strip().lower() not in {"0", "false", "no", "off"}
MAX_CACHED_PICKS = 1000
# The board is pre-warmed right after the daily scan saves a new snapshot, so a
# long TTL keeps every page load instant from cache until the next daily run.
# (If the cache ever misses, a single lazy recompute refills it.)
CACHE_TTL_SECONDS = 26 * 3600
# Hold horizon = exit at open[T+H]. Re-backtest under the user's real mechanics
# (buy close[T], sell open[T+k] within 10d) showed the net-of-cost edge peaks at
# H=8 for pullback_hv (+1.10%, spread 19%) and both contrarian signals, and 8
# sits inside the user's T+7~T+9 hold window. See exit_model.py + MAINTENANCE_LOG.
CALIBRATION_HORIZON = 8
PULLBACK_HV_HORIZON = 8

SECTOR_ETFS = {
    "technology": "XLK",
    "software": "XLK",
    "computer": "XLK",
    "semiconductor": "SOXX",
    "electronic": "XLK",
    "communication": "XLC",
    "telecom": "XLC",
    "media": "XLC",
    "entertainment": "XLC",
    "financial": "XLF",
    "bank": "XLF",
    "insurance": "XLF",
    "broker": "XLF",
    "health": "XLV",
    "medical": "XLV",
    "pharma": "XLV",
    "biotech": "XBI",
    "consumer staples": "XLP",
    "food": "XLP",
    "beverage": "XLP",
    "retail": "XLY",
    "restaurant": "XLY",
    "auto": "XLY",
    "apparel": "XLY",
    "industrial": "XLI",
    "aerospace": "XLI",
    "machinery": "XLI",
    "transport": "XLI",
    "energy": "XLE",
    "oil": "XLE",
    "gas": "XLE",
    "materials": "XLB",
    "chemical": "XLB",
    "metal": "XLB",
    "mining": "XLB",
    "utility": "XLU",
    "utilities": "XLU",
    "real estate": "XLRE",
    "reit": "XLRE",
}


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _rs_conviction(rel_strength: Optional[float]):
    """Relative-strength conviction multiplier + state for the pullback ranking.

    Conditioning backtest (2026-06-30, spx 180 / 3y / H=10 / net of 10bps, excess
    vs own baseline, cluster-bootstrap): inside the pullback_hv edge cohort the
    rebound edge lives almost entirely in names trading >= the market (20d RS vs
    SPY >= 0): KEEP RS>=0 net +0.68% CI[+0.31,+1.05] (edge) vs DROP RS<0 net
    -0.02% CI[-0.26,+0.18] (no edge). RS<0 is the no-edge / falling-knife zone, so
    it is demoted (not excluded). Depth (long pinned below EMA) is NOT gated -- it
    is where the edge concentrates. Returns (multiplier, state)."""
    rs = _finite(rel_strength, 0.0)
    config = ranking_settings()
    strong, weak, floor = config["rs_full_threshold"], config["rs_weak_threshold"], config["rs_weak_multiplier"]
    if not _RS_GATE:
        return 1.0, "off"
    if rs >= strong:
        return 1.0, "leader"
    if rs <= weak:
        return floor, "laggard"
    return floor + (1 - floor) * ((rs - weak) / (strong - weak)), "neutral"


def _board_from_latest_slices(limit: int = 8) -> Optional[Dict[str, Any]]:
    """Fast read-only fallback used when the rich board cache is cold.

    The daily ledger writes the complete scored candidate slice into SQLite.
    Page loads should prefer that persisted slice over recomputing market data
    synchronously. Daily scans and explicit force_refresh still rebuild the rich
    board.
    """
    # When the RS gate is on we re-rank, so pull a generous superset (not just the
    # top-N by the persisted old order) and trim after re-ranking below.
    fetch_n = max(int(limit), 300) if _RS_GATE else max(1, int(limit))
    rows = priority_candidate_slices_for_date(limit=fetch_n)
    if not rows:
        return None
    picks: List[Dict[str, Any]] = []
    for row in rows:
        payload = row.get("payload") or {}
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        picks.append({
            "symbol": symbol,
            "current_price": _finite(row.get("current_price"), _finite(payload.get("current_price"))),
            "priority_score": _finite(row.get("priority_score")),
            "relative_strength_20d": _finite(row.get("relative_strength_20d")),
            "stock_return_3m": payload.get("stock_return_3m"),
            "stock_return_6m": payload.get("stock_return_6m"),
            "pullback_hv_score": payload.get("pullback_hv_score"),
            "calibrated_probability": _finite(row.get("calibrated_prob"), 0.5),
            "calibration_source": payload.get("calibration_source"),
            "regime_multiplier": _finite(payload.get("regime_multiplier"), 1.0),
            "portfolio_regime": payload.get("portfolio_regime"),
            "portfolio_regime_cn": payload.get("portfolio_regime_cn"),
            "raw_launch_score": _finite(payload.get("raw_launch_score")),
            "daily_tunnel_score": _finite(payload.get("daily_tunnel_score")),
            "trend_30d": _finite(payload.get("trend_30d")),
            "options_sentiment": payload.get("options_sentiment") or {"tone": "unknown", "label": "--"},
            "iv": payload.get("iv") or {},
            "hv": payload.get("hv") or {},
            "liquidity": payload.get("liquidity") or {},
            "confidence_badge": payload.get("confidence_badge") or "validated",
            "reason": payload.get("reason") or "",
            "detail_url": payload.get("detail_url") or f"/single-stock-overnight?symbol={symbol}",
            "track": payload.get("track"),
            "track_cn": payload.get("track_cn"),
            "track_detail": payload.get("track_detail") or {},
            "source_universe_ids": payload.get("source_universe_ids") or [],
            "source_universe_labels": payload.get("source_universe_labels") or [],
            "playbook_enhancements": payload.get("playbook_enhancements") or {"labels": []},
            "earnings": payload.get("earnings") or _earnings_brief(symbol),
        })
    # Apply the relative-strength conviction gate to the persisted slice too, so
    # the live page reflects it immediately (the daily ledger may have been
    # written before the gate existed). Cheap + read-only: re-rank by conviction.
    for p in picks:
        cp = _finite(p.get("calibrated_probability"), 0.5)
        rs_conv, rs_state = _rs_conviction(p.get("relative_strength_20d"))
        p["rs_conviction"] = round(rs_conv, 3)
        p["rs_state"] = rs_state
        p["conviction_score"] = round(cp * rs_conv, 4)
        if _RS_GATE and rs_state == "laggard":
            flag = "（无热钱·低把握）"
            if flag not in (p.get("reason") or ""):
                p["reason"] = ((p.get("reason") or "").rstrip() + " · " + flag) if p.get("reason") else flag
    if _RS_GATE:
        picks.sort(key=lambda x: _finite(x.get("conviction_score"), 0.0), reverse=True)
        picks = picks[:max(1, int(limit))]
    _attach_sector_diffusion_labels(picks)
    top_win = max((_finite(p.get("calibrated_probability"), 0.5) for p in picks), default=0.0)
    data_as_of = str(rows[0].get("as_of_date") or "")
    return {
        "generated_at": rows[0].get("created_at") or data_as_of,
        **_board_freshness(data_as_of),
        "snapshot_id": None,
        "benchmark_return_20d": 0.0,
        "confidence": {"badge": "validated", "note": "loaded from persisted daily candidate slice"},
        "signal_basis": "pullback_hv_slice",
        "horizon_days": int(rows[0].get("horizon_days") or PULLBACK_HV_HORIZON),
        "n_candidates": len(picks),
        "n_freshly_scored": 0,
        "no_edge_today": bool(top_win < 0.5),
        "top_calibrated_win_rate": round(top_win, 4),
        "win_rate_explainer": WIN_RATE_EXPLAINER,
        "calibration_provenance": _calibration_provenance(PULLBACK_HV_HORIZON),
        "picks": picks,
        "cache_hit": False,
        "source": "priority_candidate_slices",
        "method_note": "Fast read-only fallback: latest persisted daily slice; no market data or external API recomputation.",
    }


def _window_return(closes: Optional[List[Any]], window: int = 20) -> Optional[float]:
    values = [_finite(c) for c in (closes or []) if _finite(c) > 0]
    if len(values) < 2:
        return None
    start = values[-(window + 1)] if len(values) >= window + 1 else values[0]
    return values[-1] / start - 1.0 if start > 0 else None


def _series_window_return(close: Any, window: int = 20) -> Optional[float]:
    try:
        series = pd.to_numeric(close, errors="coerce").dropna()
        if len(series) < 2:
            return None
        start = float(series.iloc[-(window + 1)]) if len(series) >= window + 1 else float(series.iloc[0])
        end = float(series.iloc[-1])
        return end / start - 1.0 if start > 0 and end > 0 else None
    except Exception:
        return None


def _pct_scale(values: List[float], value: Optional[float]) -> float:
    vals = sorted(v for v in values if math.isfinite(v))
    if value is None or not math.isfinite(value) or len(vals) < 2:
        return 0.5
    lo = vals[int(0.05 * (len(vals) - 1))]
    hi = vals[int(0.95 * (len(vals) - 1))]
    if hi <= lo:
        return 0.5
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))


def _sector_etf_from_track(track: Optional[Dict[str, Any]]) -> Optional[str]:
    text = f"{(track or {}).get('sector') or ''} {(track or {}).get('industry') or ''}".lower()
    if not text.strip():
        return None
    for key, etf in SECTOR_ETFS.items():
        if key in text:
            return etf
    return None


def _earnings_brief(symbol: str) -> Dict[str, Any]:
    try:
        data = get_next_earnings(symbol) or {}
    except Exception:
        data = {}
    if not data.get("available"):
        return {"available": False}
    try:
        days = data.get("days_until")
        near = isinstance(days, int) and 0 <= days <= 10
    except Exception:
        near = False
    return {
        "available": True,
        "next_date": data.get("next_date"),
        "days_until": data.get("days_until"),
        "eps_estimated": data.get("eps_estimated"),
        "revenue_estimated": data.get("revenue_estimated"),
        "last_report_date": data.get("last_report_date"),
        "last_surprise_pct": data.get("last_surprise_pct"),
        "estimate_tone": data.get("estimate_tone") or "neutral",
        "estimate_basis": data.get("estimate_basis") or "",
        "near_earnings": near,
    }


def _etf_returns(etf: str, memo: Dict[str, Dict[str, Optional[float]]]) -> Dict[str, Optional[float]]:
    key = str(etf or "").upper()
    if not key:
        return {"ret_3m": None, "ret_6m": None}
    if key in memo:
        return memo[key]
    out = {"ret_3m": None, "ret_6m": None}
    try:
        frame, _source = get_daily_history(key, period="1y")
        close = frame.get("Close") if frame is not None and not frame.empty else None
        out = {
            "ret_3m": _series_window_return(close, 63),
            "ret_6m": _series_window_return(close, 126),
        }
    except Exception:
        pass
    memo[key] = out
    return out


def _sector_etf_from_pick(pick: Dict[str, Any]) -> Optional[str]:
    enhancement = pick.get("playbook_enhancements") or {}
    etf = enhancement.get("industry_etf")
    if etf:
        return str(etf).upper()
    return _sector_etf_from_track(pick.get("track_detail") or {})


def _sector_etf_from_slice(row: Dict[str, Any]) -> Optional[str]:
    payload = row.get("payload") or {}
    enhancement = payload.get("playbook_enhancements") or {}
    etf = enhancement.get("industry_etf")
    if etf:
        return str(etf).upper()
    return _sector_etf_from_track(payload.get("track_detail") or {})


def _attach_sector_diffusion_labels(picks: List[Dict[str, Any]]) -> None:
    """Attach sector heat / diffusion as review-only playbook labels."""
    if not picks:
        return

    current_by_etf: Dict[str, List[Dict[str, Any]]] = {}
    for p in picks:
        etf = _sector_etf_from_pick(p)
        if etf:
            current_by_etf.setdefault(etf, []).append(p)
    if not current_by_etf:
        return

    try:
        recent = priority_candidate_recent(days=12)
    except Exception:
        recent = []

    by_date_etf: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for row in recent:
        day = str(row.get("as_of_date") or "")
        etf = _sector_etf_from_slice(row)
        if not day or not etf:
            continue
        by_date_etf.setdefault(day, {}).setdefault(etf, []).append(row)

    dates = sorted(by_date_etf)
    latest_date = dates[-1] if dates else None
    previous_dates = [d for d in dates if latest_date is None or d < latest_date][-5:]

    for etf, group in current_by_etf.items():
        current_symbols = {str(p.get("symbol") or "").upper() for p in group if p.get("symbol")}
        current_count = len(current_symbols)
        if current_count <= 0:
            continue

        prev_counts: List[int] = []
        prev_symbols: set[str] = set()
        for day in previous_dates:
            rows = by_date_etf.get(day, {}).get(etf, [])
            syms = {str(r.get("symbol") or "").upper() for r in rows if r.get("symbol")}
            if syms:
                prev_counts.append(len(syms))
                prev_symbols.update(syms)

        prev_avg = sum(prev_counts) / len(prev_counts) if prev_counts else float(current_count)
        breadth_change = (current_count - prev_avg) / max(prev_avg, 1.0)
        fresh_ratio = len(current_symbols - prev_symbols) / max(current_count, 1)
        avg_playbook = sum(_finite((p.get("playbook_enhancements") or {}).get("playbook_score")) for p in group) / max(len(group), 1)
        stock_vs_sector = sum(1 for p in group if (p.get("playbook_enhancements") or {}).get("stock_stronger_than_industry")) / max(len(group), 1)

        raw_score = (
            0.38 * max(0.0, min(1.0, 0.50 + breadth_change))
            + 0.27 * max(0.0, min(1.0, fresh_ratio))
            + 0.20 * max(0.0, min(1.0, stock_vs_sector))
            + 0.15 * max(0.0, min(1.0, avg_playbook))
        )
        score = round(max(0.0, min(1.0, raw_score)), 4)
        if score < 0.55 and current_count < 3:
            continue
        if score >= 0.72:
            label, tone = "赛道扩散强", "strong"
        elif score >= 0.60:
            label, tone = "赛道扩散", "good"
        else:
            label, tone = "赛道热度", "neutral"

        reason = (
            f"{etf} 当前候选 {current_count} 只；近5个切片平均 {prev_avg:.1f} 只；"
            f"新增比例 {fresh_ratio * 100:.0f}%；强于行业占比 {stock_vs_sector * 100:.0f}%。"
            "该标签只用于赛道热度/人工复核，不改写校准胜率。"
        )
        for p in group:
            e = p.setdefault("playbook_enhancements", {})
            labels = e.setdefault("labels", [])
            if not any((x or {}).get("id") == "sector_diffusion" for x in labels):
                labels.append({
                    "id": "sector_diffusion",
                    "label": label,
                    "tone": tone,
                    "reason": reason,
                })
            e["sector_diffusion"] = {
                "sector_etf": etf,
                "score": score,
                "current_count": current_count,
                "prev_avg_count": round(prev_avg, 2),
                "breadth_change": round(breadth_change, 4),
                "fresh_component_ratio": round(fresh_ratio, 4),
                "stock_vs_sector_ratio": round(stock_vs_sector, 4),
                "evidence_level": "soft_review_only",
            }


def _hv_rise(close: Any, window: int = 20, lookback: int = 10) -> Optional[float]:
    """Realized-vol expansion: HV(now) / HV(lookback days ago) - 1, from price."""
    try:
        import numpy as np

        series = pd.to_numeric(close, errors="coerce").dropna()
        if len(series) < window + lookback + 2:
            return None
        log_ret = np.log(series / series.shift(1))
        hv = log_ret.rolling(window).std() * (252.0 ** 0.5)
        now = float(hv.iloc[-1])
        prior = float(hv.iloc[-(lookback + 1)])
        if not (now > 0 and prior > 0):
            return None
        return round(now / prior - 1.0, 4)
    except Exception:
        return None


def _hv_state(hv_rise: Optional[float]) -> Dict[str, Any]:
    if hv_rise is None:
        return {"hv_rise": None, "hv_state": "数据不足"}
    if hv_rise > 0.05:
        state = "波动扩张(临近启动)"
    elif hv_rise < -0.05:
        state = "波动收敛"
    else:
        state = "波动平稳"
    return {"hv_rise": hv_rise, "hv_state": state}


def _symbol_track(symbol: str) -> Dict[str, Any]:
    """Sector/industry label for a symbol (the '赛道'). Two sources: yfinance
    .info (GICS sector+industry, matches the CN maps) then Massive reference
    (SIC description, proxy-capable so it survives flaky direct TLS). Only a
    NON-empty result is cached (forever) -- caching an empty result was why the
    column stayed blank."""
    key = f"sector_industry:v1:{symbol}"
    cached = cache_get(key)
    if isinstance(cached, dict) and (cached.get("sector") or cached.get("industry")):
        return cached
    out: Dict[str, Any] = {"sector": None, "industry": None}
    # Massive ticker-reference FIRST: proxy-capable + reliable in-container. The
    # old yfinance-first path could hang / be blocked behind the GFW under the
    # parallel prewarm, which left the 赛道 column blank for many names. SIC gives
    # a usable industry label; that alone guarantees the column is non-empty.
    try:
        from market_data_service import get_ticker_reference

        sic = (get_ticker_reference(symbol) or {}).get("sic_description")
        if sic:
            out["industry"] = str(sic).title()
    except Exception:
        pass
    # yfinance UPGRADE: nicer GICS sector+industry that matches the CN maps.
    if not external_data_allowed():
        return out
    # Best-effort only -- never let its flakiness override the reliable result.
    try:
        import yfinance as yf

        info = yf.Ticker(symbol).info or {}
        if info.get("sector") or info.get("industry"):
            out = {"sector": info.get("sector") or out.get("sector"),
                   "industry": info.get("industry") or out.get("industry")}
    except Exception:
        pass
    if out.get("sector") or out.get("industry"):
        cache_set(key, out)  # persist only a real hit
    return out


def prewarm_tracks(symbols: List[str], limit: int = 60) -> Dict[str, int]:
    """Warm track labels while preserving the caller's external-data boundary."""
    seen, todo = set(), []
    for raw in symbols:
        sym = str(raw or "").upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        c = cache_get(f"sector_industry:v1:{sym}")
        if not (isinstance(c, dict) and (c.get("sector") or c.get("industry"))):
            todo.append(sym)
        if len(seen) >= limit:
            break
    if not todo:
        return {"warmed": 0, "attempted": 0}
    warmed = 0
    allowed = external_data_allowed()

    def scoped_track(symbol):
        with external_data_scope(allowed):
            return _symbol_track(symbol)

    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for tr in ex.map(scoped_track, todo):
                if isinstance(tr, dict) and (tr.get("sector") or tr.get("industry")):
                    warmed += 1
    except Exception:
        pass
    return {"warmed": warmed, "attempted": len(todo)}


_SECTOR_CN = {
    "technology": "科技", "communication services": "通信服务", "consumer cyclical": "可选消费",
    "consumer defensive": "必需消费", "healthcare": "医疗保健", "financial services": "金融",
    "industrials": "工业", "energy": "能源", "basic materials": "原材料",
    "real estate": "房地产", "utilities": "公用事业",
}

_INDUSTRY_CN = {
    # Technology
    "software-application": "应用软件", "software-infrastructure": "基础软件", "semiconductors": "半导体",
    "semiconductor equipment & materials": "半导体设备与材料", "consumer electronics": "消费电子",
    "information technology services": "IT服务", "communication equipment": "通信设备",
    "computer hardware": "计算机硬件", "electronic components": "电子元件",
    "scientific & technical instruments": "科学技术仪器", "solar": "太阳能",
    # Communication Services
    "internet content & information": "互联网内容与信息", "entertainment": "娱乐",
    "telecom services": "电信服务", "advertising agencies": "广告", "broadcasting": "广播",
    "electronic gaming & multimedia": "电子游戏与多媒体", "publishing": "出版",
    # Consumer Cyclical
    "internet retail": "互联网零售", "specialty retail": "专业零售", "restaurants": "餐饮",
    "auto manufacturers": "汽车制造", "auto parts": "汽车零部件", "apparel retail": "服装零售",
    "apparel manufacturing": "服装制造", "footwear & accessories": "鞋类与配饰",
    "travel services": "旅游服务", "lodging": "酒店", "resorts & casinos": "度假村与赌场",
    "home improvement retail": "家居装修零售", "residential construction": "住宅建筑",
    "packaging & containers": "包装与容器", "leisure": "休闲", "gambling": "博彩",
    "personal services": "个人服务", "furnishings, fixtures & appliances": "家居用品与电器",
    "department stores": "百货", "luxury goods": "奢侈品", "recreational vehicles": "休闲车辆",
    # Consumer Defensive
    "grocery stores": "杂货店", "discount stores": "折扣店", "beverages-non-alcoholic": "非酒精饮料",
    "beverages-wineries & distilleries": "酒类", "beverages-brewers": "啤酒", "packaged foods": "包装食品",
    "confectioners": "糖果", "household & personal products": "家居与个人护理", "tobacco": "烟草",
    "farm products": "农产品", "food distribution": "食品分销", "education & training services": "教育培训",
    # Healthcare
    "drug manufacturers-general": "大型制药", "drug manufacturers-specialty & generic": "专科与仿制药",
    "biotechnology": "生物科技", "medical devices": "医疗器械", "medical instruments & supplies": "医疗仪器与耗材",
    "healthcare plans": "医疗保险", "medical care facilities": "医疗机构", "diagnostics & research": "诊断与研究",
    "medical distribution": "医药分销", "pharmaceutical retailers": "药品零售", "health information services": "健康信息服务",
    # Financial Services
    "banks-diversified": "综合银行", "banks-regional": "区域银行", "capital markets": "资本市场",
    "asset management": "资产管理", "insurance-diversified": "综合保险", "insurance-life": "寿险",
    "insurance-property & casualty": "财产险", "insurance brokers": "保险经纪", "credit services": "信贷服务",
    "financial data & stock exchanges": "金融数据与交易所", "insurance-reinsurance": "再保险",
    "mortgage finance": "抵押贷款", "insurance-specialty": "专业保险",
    # Industrials
    "aerospace & defense": "航空航天与国防", "specialty industrial machinery": "专业工业机械",
    "railroads": "铁路", "trucking": "货运", "integrated freight & logistics": "综合货运物流",
    "building products & equipment": "建筑产品与设备", "farm & heavy construction machinery": "农业与重型机械",
    "engineering & construction": "工程与建筑", "industrial distribution": "工业分销", "conglomerates": "综合企业",
    "airlines": "航空", "electrical equipment & parts": "电气设备与零件", "specialty business services": "专业商业服务",
    "staffing & employment services": "人力资源服务", "security & protection services": "安保服务",
    "waste management": "废物管理", "rental & leasing services": "租赁服务", "tools & accessories": "工具与配件",
    "metal fabrication": "金属加工", "consulting services": "咨询服务", "marine shipping": "海运",
    # Energy
    "oil & gas integrated": "综合油气", "oil & gas e&p": "油气勘探开采", "oil & gas midstream": "油气中游",
    "oil & gas equipment & services": "油气设备服务", "oil & gas refining & marketing": "油气炼化与销售",
    "uranium": "铀", "thermal coal": "动力煤",
    # Basic Materials
    "specialty chemicals": "特种化工", "chemicals": "化工", "gold": "黄金", "copper": "铜", "aluminum": "铝",
    "steel": "钢铁", "building materials": "建材", "agricultural inputs": "农资",
    "other industrial metals & mining": "工业金属与采矿", "lumber & wood production": "木材",
    "paper & paper products": "造纸",
    # Real Estate
    "reit-specialty": "专业REIT", "reit-industrial": "工业REIT", "reit-retail": "零售REIT",
    "reit-residential": "住宅REIT", "reit-office": "办公REIT", "reit-healthcare facilities": "医疗REIT",
    "reit-hotel & motel": "酒店REIT", "reit-diversified": "综合REIT", "reit-mortgage": "抵押REIT",
    "real estate services": "房地产服务",
    # Utilities
    "utilities-regulated electric": "受监管电力", "utilities-regulated gas": "受监管燃气",
    "utilities-regulated water": "受监管水务", "utilities-diversified": "综合公用",
    "utilities-renewable": "可再生能源公用", "utilities-independent power producers": "独立发电",
}


def _cn_lookup(value: Optional[str], table: Dict[str, str]) -> Optional[str]:
    if not value:
        return None
    norm = str(value).strip().lower().replace("—", "-").replace("–", "-")
    return table.get(norm)


def _track_label(track: Dict[str, Any]) -> Optional[str]:
    industry = (track or {}).get("industry")
    sector = (track or {}).get("sector")
    return industry or sector or None


def _track_label_cn(track: Dict[str, Any]) -> Optional[str]:
    """Chinese label for the track, English-source mapped; None if unmapped."""
    return _cn_lookup((track or {}).get("industry"), _INDUSTRY_CN) or _cn_lookup((track or {}).get("sector"), _SECTOR_CN)


def _adv(frame: Any, window: int = 20) -> Optional[float]:
    """Average daily dollar volume (close x volume) over `window` days."""
    try:
        close = pd.to_numeric(frame.get("Close"), errors="coerce")
        vol = pd.to_numeric(frame.get("Volume"), errors="coerce")
        dollar = (close * vol).dropna()
        if len(dollar) < 5:
            return None
        return float(dollar.tail(window).mean())
    except Exception:
        return None


def _liquidity_tier(adv: Optional[float]) -> Dict[str, Any]:
    """Liquidity = your margin-of-error: how easily you can exit if wrong.
    NOT a guarantee of recovery (liquid names can still stay impaired)."""
    if adv is None or adv <= 0:
        return {"adv": None, "tier": "未知", "tone": "unknown"}
    if adv >= 1e9:
        return {"adv": adv, "tier": "超强流动性", "tone": "strong"}
    if adv >= 2e8:
        return {"adv": adv, "tier": "强流动性", "tone": "good"}
    if adv >= 5e7:
        return {"adv": adv, "tier": "中等流动性", "tone": "ok"}
    return {"adv": adv, "tier": "弱流动性·容错低", "tone": "warn"}


_FLOOR_CACHE_KEY = "liquidity_floor:v1"
FLOOR_TOP_N = max(20, int(os.getenv("LIQUIDITY_FLOOR_TOP_N", "150")))


def _floor_base_symbols() -> tuple[list[str], bool]:
    """Base universe for the liquidity floor: SPX (archived snapshot, offline)
    plus NDX if an offline snapshot exists. NDX membership needs Wikipedia/QQQ
    holdings which are GFW-blocked in-container, so we use archive_only and
    gracefully degrade to SPX when no NDX snapshot is present (agreed fallback).
    """
    syms: set[str] = set()
    ndx_ok = False
    try:
        from scripts.screening_framework_v2_optimized import resolve_universe

        spx, _src, _m = resolve_universe("spx", archive_only=True)
        syms.update(str(s).upper() for s in spx)
    except Exception:
        pass
    try:
        from scripts.screening_framework_v2_optimized import resolve_universe

        ndx, _src, _m = resolve_universe("ndx", archive_only=True)
        if ndx:
            syms.update(str(s).upper() for s in ndx)
            ndx_ok = True
    except Exception:
        ndx_ok = False
    return sorted(syms), ndx_ok


def liquidity_floor(force: bool = False) -> Dict[str, Any]:
    """Compute (and cache) the high-liquidity floor: the dollar-ADV of the
    ``FLOOR_TOP_N``-th most liquid name in SPX(∪NDX). Cache-only price reads, so
    it never blocks on the network; refreshed once per daily batch.
    """
    if not force:
        cached = cache_get(_FLOOR_CACHE_KEY)
        if isinstance(cached, dict) and cached.get("threshold_adv") is not None:
            return cached
    base, ndx_ok = _floor_base_symbols()
    ranked: List[tuple] = []
    for sym in base:
        try:
            frame, _src = get_daily_history(sym, period="2mo", allow_yfinance_fallback=False)
            adv = _adv(frame)
        except Exception:
            adv = None
        if adv and adv > 0:
            ranked.append((adv, sym))
    ranked.sort(reverse=True)
    top = ranked[:FLOOR_TOP_N]
    threshold = float(top[-1][0]) if top else 0.0
    out = {
        "threshold_adv": threshold,
        "set": [s for _a, s in top],
        "set_size": len(top),
        "base": "SPX∪NDX" if ndx_ok else "SPX（NDX 无离线快照，已退化）",
        "base_count": len(base),
        "ndx_included": ndx_ok,
        "top_n": FLOOR_TOP_N,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }
    cache_set(_FLOOR_CACHE_KEY, out, ttl_seconds=3 * 24 * 3600)
    return out


def _pullback_state(launch_norm: float) -> str:
    """Plain-language pullback read: low launch score = more oversold/pulled back."""
    v = min(1.0, max(0.0, _finite(launch_norm)))
    if v <= 0.40:
        return "深度回调(超卖)"
    if v <= 0.55:
        return "回调中"
    if v <= 0.70:
        return "偏强(回调有限)"
    return "强势追高(非回调)"


def single_stock_signal_read(
    symbol: str,
    *,
    frame: Optional[pd.DataFrame] = None,
    spy_ret: Optional[float] = None,
    selected: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """The validated buy-signal read for one stock (same logic as the board).

    Returns calibrated win-rate + pullback state + vol-expansion + relative
    strength + confidence badge, so the detail page leads with the validated
    pullback+vol-expansion signal instead of the legacy overnight-alpha lens.

    ``frame`` / ``spy_ret`` / ``selected`` can be passed in to avoid redundant
    per-call price downloads + benchmark fetches + calibration lookups when
    scoring many symbols at once (e.g. the portfolio page).
    """
    try:
        from launch_signal_service import _score_frame

        if frame is None:
            frame, _src = get_daily_history(symbol, period="6mo")
        scored = _score_frame(symbol, frame, 0.5) if (frame is not None and not frame.empty) else None
        if not scored:
            return {"available": False, "reason": "价格历史不足，无法评估"}
        launch = _finite(scored.get("launch_score"))
        tunnel = _finite((scored.get("daily_tunnel") or {}).get("score"))
        close = pd.to_numeric(frame.get("Close"), errors="coerce").dropna()
        hv_rise = _hv_rise(close)
        stock_ret_20 = float(close.iloc[-1] / close.iloc[-21] - 1.0) if len(close) >= 21 else None
        if spy_ret is None:
            spy_ret = _spy_return(20)
        rel_strength = (stock_ret_20 - spy_ret) if stock_ret_20 is not None else None

        sel = selected if selected is not None else _select_signals()
        horizon = int(sel.get("horizon", PULLBACK_HV_HORIZON))
        if sel.get("mode") == "pullback_hv":
            score = _pullback_hv_score(launch, hv_rise)
            cal = calibrate("pullback_hv", score, horizon)
            basis = "pullback_hv"
        else:
            cal = calibrate(sel.get("launch", "launch_contrarian"), launch, horizon)
            basis = sel.get("launch", "launch_contrarian")
        return {
            "available": True,
            "calibrated_win_rate": cal.get("p_calibrated"),
            "calibration_source": cal.get("source"),
            "confidence_badge": sel.get("badge"),
            "signal_basis": basis,
            "horizon_days": horizon,
            "pullback_state": _pullback_state(launch),
            "hv": _hv_state(hv_rise),
            "relative_strength_20d": round(rel_strength, 4) if rel_strength is not None else None,
            "raw_launch_score": round(launch, 4),
            "daily_tunnel_score": round(tunnel, 1),
        }
    except Exception as exc:
        return {"available": False, "reason": str(exc)[-200:]}


def _spy_return(window: int = 20) -> float:
    try:
        frame, _source = get_daily_history("SPY", period="3mo")
        if frame.empty or "Close" not in frame:
            return 0.0
        close = frame["Close"].dropna().astype(float)
        if len(close) < window + 1:
            return 0.0
        return float(close.iloc[-1] / close.iloc[-(window + 1)] - 1.0)
    except Exception:
        return 0.0


def _active_edge(signal_type: str, horizon: int = CALIBRATION_HORIZON) -> Optional[Dict[str, Any]]:
    row = signal_calibration_active(signal_type, horizon)
    if row and isinstance(row.get("curve"), dict):
        return (row["curve"].get("global") or {}).get("edge") or {}
    return None


# Plain-language explainer shown on the board/detail pages so a non-quant reader
# understands what "胜率/win-rate" means here and where the number comes from.
WIN_RATE_EXPLAINER = (
    "这里的「胜率」不是涨跌概率，而是：按历史校准，买入后未来 N 日"
    "【扣掉交易成本、并跑赢这只股票自己长期平均表现】的概率。50% = 跟它自己长期表现一样；"
    ">50% 才是真有超额。只有最被错杀(回调+波动放大)的那批票才显著 >50%。"
)


# Override the older mojibake copy above with the current product wording.
WIN_RATE_EXPLAINER = (
    "这里的「校准胜率」不是单纯涨跌概率，也不是保证赚钱概率；它表示："
    "按历史校准曲线，信号出现后未来 N 个交易日，扣除交易成本后，跑赢这只股票自身长期基线表现的概率。"
    "命中概率与平均期望收益是不同指标；概率超过50%不保证期望净收益为正。"
    "当前主胜率只来自 pullback_hv 主信号，也就是「回调错杀 + 波动放大」的历史校准；"
    "市场+流动性+RS前40% 与强于行业ETF目前只是软增强标签，只参与同档位排序微调和前向对账，不会改写这个胜率。"
)


def _calibration_provenance(horizon: int = PULLBACK_HV_HORIZON) -> Dict[str, Any]:
    """Where the active pullback_hv calibration came from (for UI transparency)."""
    row = signal_calibration_active("pullback_hv", horizon)
    if not row or not isinstance(row.get("curve"), dict):
        return {"available": False}
    prov = (row["curve"].get("global") or {}).get("provenance") or {}
    return {
        "available": True,
        "source": row.get("source"),
        "source_label": (
            "Databento/PIT 校准" if str(row.get("source") or "").startswith("databento")
            else "历史回放校准"
        ),
        "generated_at": row.get("generated_at"),
        "event_count": row.get("event_count"),
        "survivorship_controlled": bool(prov.get("survivorship_controlled")),
        "price_source": prov.get("price_source"),
        "window": prov.get("window"),
        "in_sample": prov.get("in_sample"),
        "note": prov.get("note"),
        "provenance_complete": bool(prov.get("price_source") and prov.get("window")),
    }


def _select_signals() -> Dict[str, Any]:
    """Pick the best-available calibrated signal + horizon and an honest badge.

    Preference order: the combined "pullback + vol-expansion" at 10d (strongest
    validated edge); else the contrarian (pullback) curve at 5d; else raw; else
    fallback.  The badge always reflects the real edge verdict.
    """
    combo = _active_edge("pullback_hv", PULLBACK_HV_HORIZON)
    if combo and combo.get("validated"):
        return {"mode": "pullback_hv", "horizon": PULLBACK_HV_HORIZON, "badge": "validated",
                "note": "回调+波动扩张(10日)：顶桶净超额显著，CI 排除 0", "edge": combo}

    launch_c, launch_b = _active_edge("launch_contrarian"), _active_edge("launch")
    if launch_c and launch_c.get("validated"):
        return {"mode": "dual", "horizon": CALIBRATION_HORIZON, "launch": "launch_contrarian",
                "daily_tunnel": "daily_tunnel_contrarian", "badge": "validated",
                "note": "反向(回调)信号顶桶净超额显著，CI 排除 0", "edge": launch_c}
    if launch_b and launch_b.get("validated"):
        return {"mode": "dual", "horizon": CALIBRATION_HORIZON, "launch": "launch",
                "daily_tunnel": "daily_tunnel", "badge": "validated",
                "note": "原始信号顶桶净超额显著，CI 排除 0", "edge": launch_b}
    if launch_c is not None:
        return {"mode": "dual", "horizon": CALIBRATION_HORIZON, "launch": "launch_contrarian",
                "daily_tunnel": "daily_tunnel_contrarian", "badge": "low_edge",
                "note": "已用反向校准纠正方向，但顶桶超额 CI 未排除 0，暂列低把握", "edge": launch_c}
    if launch_b is not None:
        return {"mode": "dual", "horizon": CALIBRATION_HORIZON, "launch": "launch",
                "daily_tunnel": "daily_tunnel", "badge": "low_edge",
                "note": "校准已建但无显著 edge", "edge": launch_b}
    return {"mode": "dual", "horizon": CALIBRATION_HORIZON, "launch": "launch", "daily_tunnel": "daily_tunnel",
            "badge": "experimental", "note": "无 active 校准曲线，胜率回退经验 sigmoid", "edge": None}


def _pullback_hv_score(launch: float, hv_rise: Optional[float]) -> float:
    """Live combined score: high when oversold (low launch) AND HV expanding.

    Mirrors the replay-side ``pullback_hv_score`` so calibrate() reads the same
    axis.  Missing HV reads neutral (0.5) so the pullback part still counts.
    """
    launch_norm = min(1.0, max(0.0, _finite(launch)))
    hv_norm = 0.5 if hv_rise is None else min(1.0, max(0.0, 0.5 + 0.5 * _finite(hv_rise)))
    config = ranking_settings()
    a, b = config["oversold_weight"], config["volatility_weight"]
    return min(1.0, max(0.0, (a * (1.0 - launch_norm) + b * hv_norm) / (a + b)))


def _options_sentiment(row: Dict[str, Any]) -> Dict[str, str]:
    """Collapse options/gamma data into a directional sentiment for equity reads."""
    regime = str(row.get("gex_regime") or "").lower()
    if regime.startswith("positive"):
        return {"tone": "bullish", "label": "期权情绪：看涨（正 Gamma 压制波动）"}
    if regime.startswith("negative"):
        return {"tone": "bearish", "label": "期权情绪：看跌（负 Gamma 放大波动）"}
    if regime:
        return {"tone": "neutral", "label": "期权情绪：中性"}
    return {"tone": "unknown", "label": "期权情绪：无数据"}


def _reason(rel_strength: float, calibrated_prob: float, regime: float, row: Dict[str, Any]) -> str:
    parts = []
    parts.append(f"校准胜率 {calibrated_prob * 100:.0f}%")
    if _RS_GATE and rel_strength < 0:
        # Backtested: the pullback rebound edge concentrates in RS>=0 names;
        # RS<0 is the no-edge / falling-knife zone, so flag low conviction.
        parts.append(f"弱于大盘 20 日 {rel_strength * 100:+.1f}%（无热钱·低把握）")
    else:
        parts.append(f"相对大盘 20 日 {rel_strength * 100:+.1f}%")
    regime_cn = row.get("portfolio_regime_cn") or row.get("portfolio_regime") or ""
    if regime < 0.999:
        parts.append(f"择时打折×{regime:.2f}（{regime_cn}）")
    return " · ".join(parts)


def _deep_oversold(close: "pd.Series") -> Dict[str, Any]:
    """Depth-of-oversold tag for a candidate. Backtested (2026-06-21): inside the
    pullback_hv top bucket, 'deep' (closed below the lower Bollinger band, z<-2)
    raised net excess from +0.40% to +1.23% / hit 53%->60% (CI excluded 0); RSI2
    / z<-1.5 'oversold' to ~+0.8%. Display/filter tag only -- does NOT touch the
    calibration curve."""
    try:
        if close is None or len(close) < 22:
            return {"level": "none"}
        c = float(close.iloc[-1])
        ma = float(close.tail(20).mean())
        sd = float(close.tail(20).std())
        z = (c - ma) / sd if sd > 0 else 0.0
        delta = close.diff()
        gain = float(delta.clip(lower=0).rolling(2).mean().iloc[-1] or 0.0)
        loss = float((-delta.clip(upper=0)).rolling(2).mean().iloc[-1] or 0.0)
        rsi2 = 100.0 - 100.0 / (1.0 + gain / loss) if loss > 0 else (100.0 if gain > 0 else 50.0)
        below_band = z < -2.0  # below lower Bollinger band (ma - 2sd)
        if below_band:
            level = "deep"
        elif rsi2 < 10 or z < -1.5:
            level = "oversold"
        else:
            level = "none"
        return {"level": level, "z": round(z, 2), "rsi2": round(rsi2, 1), "below_band": bool(below_band)}
    except Exception:
        return {"level": "none"}


def _attach_playbook_enhancements(picks: List[Dict[str, Any]]) -> None:
    """Attach research-only playbook tags proved useful as soft filters.

    These are deliberately labels/tiebreak evidence, not a replacement for the
    calibrated pullback_hv probability. Backtests showed the combination of
    market regime + liquidity + medium-term relative strength mainly helps the
    front bucket; stock > sector helps as a soft quality flag.
    """
    if not picks:
        return

    etf_memo: Dict[str, Dict[str, Optional[float]]] = {}
    spy = _etf_returns("SPY", etf_memo)
    spy_3m = spy.get("ret_3m")
    spy_6m = spy.get("ret_6m")

    eligible_scores: List[float] = []
    market_rs_values: List[float] = []
    for p in picks:
        s3 = p.get("stock_return_3m")
        s6 = p.get("stock_return_6m")
        if s3 is not None and spy_3m is not None and s6 is not None and spy_6m is not None:
            raw = 0.65 * (_finite(s3) - _finite(spy_3m)) + 0.35 * (_finite(s6) - _finite(spy_6m))
            p["_market_rs_raw"] = raw
            market_rs_values.append(raw)

    for p in picks:
        labels: List[Dict[str, Any]] = []
        enhancement: Dict[str, Any] = {
            "market_liquid_rs_top40": False,
            "stock_stronger_than_industry": False,
            "labels": labels,
        }

        track = p.get("track_detail") or {}
        etf = _sector_etf_from_track(track)
        s3 = p.get("stock_return_3m")
        s6 = p.get("stock_return_6m")
        if etf and s3 is not None and s6 is not None:
            sector = _etf_returns(etf, etf_memo)
            sec3 = sector.get("ret_3m")
            sec6 = sector.get("ret_6m")
            if sec3 is not None and sec6 is not None:
                vs3 = _finite(s3) - _finite(sec3)
                vs6 = _finite(s6) - _finite(sec6)
                strong = bool(_finite(s3) > _finite(sec3) and _finite(s6) > _finite(sec6) * 0.85)
                enhancement.update({
                    "industry_etf": etf,
                    "stock_vs_industry_3m": round(vs3, 4),
                    "stock_vs_industry_6m": round(vs6, 4),
                    "stock_stronger_than_industry": strong,
                })
                if strong:
                    labels.append({
                        "id": "stock_stronger_than_industry",
                        "label": "强于行业",
                        "tone": "good",
                        "reason": f"近3个月跑赢行业ETF {etf} {vs3 * 100:+.1f}%，近6个月相对 {vs6 * 100:+.1f}%。",
                    })

        market_ok = _finite(p.get("regime_multiplier"), 1.0) >= 0.75
        liq_tone = str(((p.get("liquidity") or {}).get("tone") or "")).lower()
        liquid_ok = liq_tone in {"strong", "good"}
        market_rs_raw = p.get("_market_rs_raw")
        market_rs_ok = market_rs_raw is not None and _finite(market_rs_raw) > 0
        market_rs_score = _pct_scale(market_rs_values, market_rs_raw if market_rs_raw is not None else None)
        playbook_score = 0.70 * _finite(p.get("pullback_hv_score"), 0.0) + 0.30 * market_rs_score
        enhancement.update({
            "market_ok": market_ok,
            "liquid_ok": liquid_ok,
            "market_rs_score": round(market_rs_score, 4),
            "market_rs_raw": round(_finite(market_rs_raw), 4) if market_rs_raw is not None else None,
            "playbook_score": round(playbook_score, 4),
        })
        if market_ok and liquid_ok and market_rs_ok:
            eligible_scores.append(playbook_score)
        p["playbook_enhancements"] = enhancement

    ranked_scores = sorted(eligible_scores, reverse=True)
    top_n = max(1, math.ceil(len(ranked_scores) * 0.40)) if ranked_scores else 0
    cutoff = ranked_scores[top_n - 1] if top_n else None
    for p in picks:
        e = p.get("playbook_enhancements") or {}
        score = _finite(e.get("playbook_score"))
        qualifies = bool(cutoff is not None and e.get("market_ok") and e.get("liquid_ok") and _finite(e.get("market_rs_raw"), -1.0) > 0 and score >= cutoff)
        e["market_liquid_rs_top40"] = qualifies
        e["market_liquid_rs_percentile_cutoff"] = round(cutoff, 4) if cutoff is not None else None
        if qualifies:
            (e.setdefault("labels", [])).insert(0, {
                "id": "market_liquid_rs_top40",
                "label": "市场+流动性+RS前40%",
                "tone": "strong",
                "reason": "市场闸门未关闭、日均成交额进入强/良好档，并且3/6个月相对SPY强度组合位于本批候选前40%。",
            })
        p["playbook_enhancements"] = e


def _score_candidates(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """Compute fresh launch + daily_tunnel scores for the candidate pool.

    Reuses the launch scanner's ``_score_frame`` (no high-score filter, so the
    oversold names the validated contrarian edge depends on are kept).  This
    fixes the prior limitation where most snapshot rows carried no launch score,
    leaving the calibrated term idle.
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not symbols:
        return out
    try:
        from launch_signal_service import _score_frame, _ticker_frame
        download, _sources = download_daily_history(symbols, period="6mo")
    except Exception:
        return out
    for symbol in symbols:
        try:
            frame = _ticker_frame(download, symbol)
            scored = _score_frame(symbol, frame, 0.5)
            if not scored:
                continue
            close = pd.to_numeric(frame.get("Close"), errors="coerce").dropna()
            stock_ret_20 = float(close.iloc[-1] / close.iloc[-21] - 1.0) if len(close) >= 21 else None
            stock_ret_63 = _series_window_return(close, 63)
            stock_ret_126 = _series_window_return(close, 126)
            hv_rise = _hv_rise(close)
            out[symbol] = {
                "launch": _finite(scored.get("launch_score")),
                "tunnel": _finite((scored.get("daily_tunnel") or {}).get("score")),
                "last_price": _finite(scored.get("last_price")),
                "price_as_of": close.index[-1].date().isoformat() if len(close) else None,
                "stock_ret_20": stock_ret_20,
                "stock_ret_63": stock_ret_63,
                "stock_ret_126": stock_ret_126,
                "hv_rise": hv_rise,
                "pullback_hv_score": _pullback_hv_score(_finite(scored.get("launch_score")), hv_rise),
                "adv": _adv(frame),
                "deep_oversold": _deep_oversold(close),
            }
        except Exception:
            continue
    return out


def _board_freshness(data_as_of: Any) -> Dict[str, Any]:
    from market_calendar import most_recent_session

    day = str(data_as_of or "").strip()
    return {
        "data_as_of": day or None,
        "data_stale": day < most_recent_session().isoformat() if day else None,
    }


def compute_priority_board(limit: int = 8, force_refresh: bool = False) -> Dict[str, Any]:
    """Rank latest home-dashboard candidates by composite priority. Cached.

    The cache stores the full (capped) pick list so the requested ``limit`` is
    applied at return time. Otherwise whichever caller computes first (e.g. the
    5-pick TopPickBanner) would pin the cached list length for everyone else.
    """
    if not force_refresh:
        cached = cache_get(CACHE_KEY)
        if isinstance(cached, dict):
            freshness = _board_freshness(cached.get("data_as_of") or cache_get("market_calendar:last_synced_session"))
            from v_swing_service import attach_board
            view = attach_board({**cached, **freshness, "cache_hit": True})
            return {**view, "picks": (view.get("picks") or [])[:limit]}
        if os.getenv("PRIORITY_BOARD_RECOMPUTE_ON_MISS", "0").lower() in {"0", "false", "no"}:
            slice_board = _board_from_latest_slices(MAX_CACHED_PICKS)
            if slice_board is not None:
                from v_swing_service import attach_board
                view = attach_board(slice_board)
                return {**view, "picks": view["picks"][:limit]}

    snapshot = latest_home_dashboard_snapshot() or {}
    rows = (((snapshot.get("payload") or {}).get("rows")) or [])
    spy_ret = _spy_return(20)
    selected = _select_signals()
    badge = {"badge": selected["badge"], "note": selected["note"], "edge": selected.get("edge")}

    # Compute fresh launch/tunnel scores for the candidate pool so the validated
    # calibrated term actually applies to every name (snapshot scores are sparse).
    candidate_symbols = []
    seen = set()
    for row in rows:
        sym = str(row.get("symbol") or "").upper()
        if sym and sym not in seen:
            seen.add(sym)
            candidate_symbols.append(sym)
    scored_map = _score_candidates(candidate_symbols[:MAX_SCORED_CANDIDATES])
    fresh_count = 0

    picks: List[Dict[str, Any]] = []
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        sc = scored_map.get(symbol)
        if sc:
            fresh_count += 1
            launch = sc["launch"]
            tunnel = sc["tunnel"]
            current_price = sc["last_price"] or _finite(row.get("current_price"))
            stock_ret = sc["stock_ret_20"]
            stock_ret_63 = sc.get("stock_ret_63")
            stock_ret_126 = sc.get("stock_ret_126")
            pullback_hv_score = _finite(sc.get("pullback_hv_score"))
        else:
            launch = _finite(row.get("launch_signal_score"))
            tunnel = _finite(row.get("daily_tunnel_score"))
            current_price = _finite(row.get("current_price"))
            stock_ret = _window_return(row.get("recent_closes"), 20)
            stock_ret_63 = _window_return(row.get("recent_closes"), 63)
            stock_ret_126 = _window_return(row.get("recent_closes"), 126)
            pullback_hv_score = _pullback_hv_score(launch, None)

        horizon = int(selected.get("horizon", CALIBRATION_HORIZON))
        if selected.get("mode") == "pullback_hv":
            # Single combined score: oversold (low launch) + HV expanding.
            combo_score = pullback_hv_score
            cal = calibrate("pullback_hv", combo_score, horizon)
            calibrated_prob = cal.get("p_calibrated") if cal.get("p_calibrated") is not None else 0.5
            calibration_source = cal.get("source")
            expected_return = cal.get("expected_return")
            expected_excess = cal.get("expected_excess")
        else:
            cal_launch = calibrate(selected["launch"], launch, horizon)
            cal_tunnel = calibrate(selected["daily_tunnel"], tunnel, horizon)
            # Only count a calibrated term when the underlying raw score is
            # actually present; a missing/zero score reads neutral, not "max oversold".
            cps = []
            if launch > 0 and cal_launch.get("p_calibrated") is not None:
                cps.append(cal_launch["p_calibrated"])
            if tunnel > 0 and cal_tunnel.get("p_calibrated") is not None:
                cps.append(cal_tunnel["p_calibrated"])
            calibrated_prob = sum(cps) / len(cps) if cps else 0.5
            calibration_source = cal_launch.get("source")
            expected_return = cal_launch.get("expected_return") if cal_launch.get("expected_return") is not None else cal_tunnel.get("expected_return")
            expected_excess = cal_launch.get("expected_excess") if cal_launch.get("expected_excess") is not None else cal_tunnel.get("expected_excess")

        rel_strength = (stock_ret - spy_ret) if stock_ret is not None else 0.0
        rs_norm = max(0.0, min(1.0, 0.5 + rel_strength * 2.0))
        regime = _finite(row.get("portfolio_gross_exposure_multiplier"), 1.0)
        # Relative-strength conviction gate. Conditioning backtest (2026-06-30,
        # spx 180 / 3y / H=10 / net of 10bps, excess vs each stock's own baseline,
        # cluster-bootstrap CI): INSIDE the pullback_hv edge cohort the rebound
        # edge lives almost entirely in names trading >= the market (20d RS vs
        # SPY >= 0):
        #     KEEP RS>=0  net +0.68%  CI[+0.31,+1.05]  (edge)
        #     DROP RS<0   net -0.02%  CI[-0.26,+0.18]  (no edge)
        # The top RS quintile was the BEST bucket (+0.71%), so the earlier
        # "high RS = chasing" assumption does NOT hold once conditioned on the
        # oversold pullback setup. Depth (long-pinned-below-EMA) is NOT a
        # disqualifier -- it is exactly where the mean-reversion edge concentrates
        # (frac20>=0.75 bucket: +0.88%) -- so we gate on RS, never on trend depth.
        # A "sunk + weak RS" falling knife (e.g. MCD: 20d RS -2.3%) sits in the
        # no-edge zone and is demoted (not excluded) here.
        rs_conviction, rs_state = _rs_conviction(rel_strength)
        # conviction_score is the ranking key: validated win-rate gated by RS.
        conviction_score = calibrated_prob * rs_conviction
        rs_nudge = ranking_settings()["relative_strength_nudge"] * (rs_norm - 0.5)
        priority = (conviction_score + rs_nudge) * (0.6 + 0.4 * regime)

        picks.append({
            "symbol": symbol,
            "current_price": _finite(current_price),
            "price_as_of": sc.get("price_as_of") if sc else None,
            "priority_score": round(priority, 4),
            "conviction_score": round(conviction_score, 4),
            "rs_conviction": round(rs_conviction, 3),
            "rs_state": rs_state,
            "relative_strength_20d": round(rel_strength, 4),
            "stock_return_3m": round(_finite(stock_ret_63), 4) if stock_ret_63 is not None else None,
            "stock_return_6m": round(_finite(stock_ret_126), 4) if stock_ret_126 is not None else None,
            "pullback_hv_score": round(pullback_hv_score, 4),
            "calibrated_probability": round(calibrated_prob, 4),
            "expected_return_8d": round(expected_return, 6) if expected_return is not None else None,
            "expected_excess_8d": round(expected_excess, 6) if expected_excess is not None else None,
            "calibration_source": calibration_source,
            "regime_multiplier": round(regime, 4),
            "portfolio_regime": row.get("portfolio_regime"),
            "portfolio_regime_cn": row.get("portfolio_regime_cn"),
            "raw_launch_score": round(launch, 4),
            "daily_tunnel_score": round(tunnel, 1),
            "trend_30d": _finite(row.get("trend_30d")),
            "options_sentiment": _options_sentiment(row),
            "iv": iv_features(symbol),
            "hv": _hv_state(sc.get("hv_rise") if sc else None),
            "liquidity": _liquidity_tier(sc.get("adv") if sc else None),
            "deep_oversold": (sc.get("deep_oversold") if sc else None) or {"level": "none"},
            "source_universe_ids": list(row.get("source_universe_ids") or []),
            "source_universe_labels": list(row.get("source_universe_labels") or []),
            "confidence_badge": badge["badge"],
            "reason": _reason(rel_strength, calibrated_prob, regime, row),
            "detail_url": f"/single-stock-overnight?symbol={symbol}",
            "earnings": row.get("earnings") or {},
        })

    # Break ties (common while calibration is flat) by raw relative strength so
    # the genuinely strongest leader surfaces as #1, not snapshot order.
    # Rank by the validated calibrated win-rate first; relative strength only
    # breaks near-ties (it is not a validated forward predictor).
    picks.sort(key=lambda item: (item.get("conviction_score", item["calibrated_probability"]), item["priority_score"]), reverse=True)
    # Attach the 赛道 label. Read cache-only inline (fast, deterministic); the
    # actual lookup is warmed in the background for the top picks by
    # prewarm_tracks() during the daily batch, then served from cache here.
    # Warm the 赛道 cache for the displayed slice so the column is never blank
    # below the daily-batch prewarm cutoff (best-effort, threaded; worker threads
    # are external-fetch-allowed even inside a cache-only board recompute).
    try:
        prewarm_tracks([p["symbol"] for p in picks[:150]], limit=150)
    except Exception:
        pass
    for p in picks[:MAX_CACHED_PICKS]:
        tr = cache_get(f"sector_industry:v1:{p['symbol']}") or {}
        p["track"] = _track_label(tr)
        p["track_cn"] = _track_label_cn(tr)
        p["track_detail"] = tr
    _attach_playbook_enhancements(picks[:MAX_CACHED_PICKS])
    _attach_sector_diffusion_labels(picks[:MAX_CACHED_PICKS])
    for p in picks[:MAX_CACHED_PICKS]:
        e = p.get("playbook_enhancements") or {}
        nudge = 0.0
        if e.get("market_liquid_rs_top40"):
            nudge += ranking_settings()["market_tag_nudge"]
        if e.get("stock_stronger_than_industry"):
            nudge += ranking_settings()["sector_tag_nudge"]
        p["soft_enhancement_nudge"] = round(nudge, 4)
        p["ranking_score"] = round(_finite(p.get("priority_score")) + nudge, 4)
    picks[:MAX_CACHED_PICKS] = sorted(
        picks[:MAX_CACHED_PICKS],
        key=lambda item: (
            _finite(item.get("conviction_score"), _finite(item.get("calibrated_probability"), 0.5)),
            _finite(item.get("ranking_score"), _finite(item.get("priority_score"))),
        ),
        reverse=True,
    )
    from pullback_parameter_service import ACTIVE_KEY, predict_pick
    active_parameters = cache_get(ACTIVE_KEY)
    if isinstance(active_parameters, dict) and active_parameters.get("eligible_for_activation") and not active_parameters.get("rejection_reasons"):
        for p in picks:
            p["parameter_research"] = predict_pick(p, active_parameters)
        picks.sort(key=lambda p: (bool((p.get("parameter_research") or {}).get("entry_candidate")),
                                 (p.get("parameter_research") or {}).get("expected_net_return", -1), p["ranking_score"]), reverse=True)
    # Compact 舆情 tag for the displayed slice (label + net + Trump flag); cached
    # per-symbol 2h so this only costs news calls on the daily batch / cache miss.
    try:
        from news_sentiment_service import news_digest
        for p in picks[:40]:
            nd = news_digest(p["symbol"])
            p["news"] = {
                "label": nd.get("label"), "net_score": nd.get("net_score"),
                "neg": nd.get("neg"), "pos": nd.get("pos"),
                "trump": bool((nd.get("trump") or {}).get("mentioned")),
            }
    except Exception:
        pass
    for p in picks[:120]:
        if not (p.get("earnings") or {}).get("available"):
            p["earnings"] = _earnings_brief(p["symbol"])
    top_win = picks[0]["calibrated_probability"] if picks else 0.0
    result = {
        "parameter_version": (active_parameters or {}).get("parameter_version", ranking_settings()["version"]),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **_board_freshness((snapshot.get("payload") or {}).get("data_as_of") or cache_get("market_calendar:last_synced_session")),
        "snapshot_id": snapshot.get("snapshot_id"),
        "benchmark_return_20d": round(spy_ret, 4),
        "confidence": badge,
        "signal_basis": selected.get("mode") if selected.get("mode") == "pullback_hv" else selected.get("launch"),
        "horizon_days": int(selected.get("horizon", CALIBRATION_HORIZON)),
        "n_candidates": len(picks),
        "n_freshly_scored": fresh_count,
        # Honest flag: if even the best name is below 50%, no candidate is
        # expected to beat its own baseline today -> stand aside.
        "no_edge_today": bool(top_win < 0.5),
        "top_calibrated_win_rate": round(top_win, 4),
        "win_rate_explainer": WIN_RATE_EXPLAINER,
        "calibration_provenance": _calibration_provenance(int(selected.get("horizon", PULLBACK_HV_HORIZON))),
        # Cache a generous slice so the ledger can persist every scanned
        # candidate while the UI still asks for a small, paginated view.
        "picks": picks[:MAX_CACHED_PICKS],
        "cache_hit": False,
        "method_note": (
            "排序按【已验证的校准胜率】(P(净成本后跑赢自身基线))优先；相对大盘强度仅作平票微调，"
            "不作正向驱动（高过往强度不预示未来超额，校准显示极端处反而偏负）。择时乘子按市况整体打折。"
        ),
    }
    cache_set(CACHE_KEY, result, ttl_seconds=CACHE_TTL_SECONDS)
    from v_swing_service import attach_board
    view = attach_board(result)
    return {**view, "picks": view["picks"][:limit]}
