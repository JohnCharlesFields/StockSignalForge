"""On-demand supply-chain view over existing company and market caches."""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from datetime import datetime, timezone

import pandas as pd

import company_network_service as network
import gildata_shadow_service as gil
from app_database import cache_get, cache_set, connection
from market_calendar import most_recent_session
from market_data_service import external_data_scope, get_daily_history

_LOCK = threading.Lock()
_ACTIVE: set[str] = set()
_ROLES = {"upstream_suppliers": "upstream", "downstream_customers": "downstream", "competitors": "competitor"}


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def price_metrics(frame, as_of):
    """Three calendar months of cached raw prices, never a total-return claim."""
    out = {"close": None, "price_date": None, "return_3m_pct": None,
           "return_start_date": None, "return_end_date": None,
           "price_basis": "raw_unadjusted", "return_status": "history_missing"}
    if frame is None or frame.empty or "Close" not in frame:
        return out
    close = pd.to_numeric(frame["Close"], errors="coerce").replace([math.inf, -math.inf], float("nan"))
    close.index = pd.DatetimeIndex(pd.to_datetime(close.index)).tz_localize(None).normalize()
    close = close[~close.index.duplicated(keep="last")].sort_index()
    close = close[(close.index <= pd.Timestamp(as_of)) & close.notna() & (close > 0)]
    if close.empty:
        return out
    last = close.index[-1]
    out.update(close=float(close.iloc[-1]), price_date=last.date().isoformat())
    start = last - pd.DateOffset(months=3)
    prior = close[close.index <= start]
    if prior.empty or (start - prior.index[-1]).days > 7:
        out["return_status"] = "history_insufficient"
        return out
    period = close[close.index >= prior.index[-1]]
    out.update(return_start_date=period.index[0].date().isoformat(), return_end_date=last.date().isoformat())
    if len(period) < 40 or period.index.to_series().diff().dt.days.max() > 10:
        out["return_status"] = "history_insufficient"
    elif period.pct_change().abs().max() > 0.35:
        # Raw caches do not certify split adjustments; suppress suspicious jumps.
        out["return_status"] = "price_jump_needs_review"
    else:
        out.update(return_3m_pct=round((period.iloc[-1] / period.iloc[0] - 1) * 100, 3),
                   return_status="raw_price_change")
    return out


def _profiles(symbols):
    keys = [f"single_company_profile:v3:{s}" for s in symbols]
    if not keys:
        return {}
    with connection() as conn:
        rows = conn.execute("SELECT cache_key,payload_json FROM kv_cache WHERE cache_key IN (" +
                            ",".join("?" for _ in keys) + ") AND (expires_at IS NULL OR expires_at>?)",
                            [*keys, time.time()]).fetchall()
    out = {}
    for row in rows:
        try:
            value = json.loads(row["payload_json"])
            if isinstance(value, dict):
                out[row["cache_key"].rsplit(":", 1)[-1]] = value
        except (ValueError, TypeError):
            pass
    return out


def _node(row, role):
    allowed = ("name", "display_name", "symbol", "market", "listing_verified", "relationship_status",
               "detail", "source_title", "evidence_date", "evidence_note", "source_quote", "market_cap_usd", "market_cap_date", "peer_type", "match_basis")
    value = {key: row[key] for key in allowed if key in row}
    name = str(value.get("display_name") or value.get("name") or value.get("symbol") or "公司名称未知")[:160]
    value.update(name=name, role=role,
        id=role + ":" + hashlib.sha256(f"{value.get('market')}:{value.get('symbol') or name}".encode()).hexdigest()[:16],
        website=network._url(row.get("website")), source_url=network._url(row.get("source_url")))
    return value


def build_graph(symbol):
    symbol = gil._safe_symbol(symbol)
    as_of = most_recent_session().isoformat()
    profile = cache_get(f"single_company_profile:v3:{symbol}") or {}
    if not isinstance(profile, dict):
        profile = {}
    company = network.cached_network(symbol, profile)
    root = _node({"name": (profile.get("facts") or {}).get("name") or symbol, "symbol": symbol, "market": "US"}, "target")
    nodes, edges = [root], []
    for key, role in _ROLES.items():
        seen = set()
        for row in (company.get("relationships") or {}).get(key, [])[:16]:
            node = _node(row, role)
            if node["id"] in seen or node.get("symbol") == symbol and node.get("market") == "US":
                continue
            seen.add(node["id"])
            nodes.append(node)
            edges.append({"source": node["id"] if role == "upstream" else root["id"],
                          "target": root["id"] if role == "upstream" else node["id"],
                          "role": role, "directed": role != "competitor",
                          "verified": node.get("relationship_status") == "disclosed"})
    for row in (company.get("industry_peers") or [])[:5]:
        # Industry references are isolated, not inferred competitors or supply links.
        nodes.append(_node(row, "industry"))
    symbols = list(dict.fromkeys(n["symbol"] for n in nodes if n.get("market") == "US"
                                and gil._SYMBOL.fullmatch(str(n.get("symbol") or ""))
                                and (n["role"] == "target" or n.get("listing_verified"))))
    profiles = _profiles(symbols)
    metrics, needs = {}, []
    with external_data_scope(False):
        for code in symbols:
            sample = gil.cached_equity(code) or {}
            if sample.get("as_of", "") > as_of:
                sample = {}
            cached = profiles.get(code) or {}
            try:
                frame, source = get_daily_history(code, period="6mo", end=as_of, allow_yfinance_fallback=False, skip_massive=True)
                values = price_metrics(frame, as_of)
            except Exception:
                # A corrupt individual cache must not discard the relationship map.
                values, source = price_metrics(None, as_of), "cache:unavailable"
            cap = _number(sample.get("market_cap_usd"))
            pe = _number(sample.get("pe"))
            meta = cached.get("market_cap_meta") or {}
            if cap is None:
                cap = _number((cached.get("facts") or {}).get("market_cap"))
            date = sample.get("as_of") if sample.get("market_cap_usd") is not None else meta.get("data_as_of_date")
            if date and date > as_of:
                cap, date = None, None
            cap = cap if cap is not None and cap > 0 else None
            values.update(market_cap_usd=cap,
                market_cap_date=date, market_cap_source=sample.get("source") if sample.get("market_cap_usd") is not None else meta.get("source"),
                pe=pe, pe_date=sample.get("as_of") if pe is not None else None,
                pe_basis="聚源PE（源未注明TTM/预测口径）", pe_source=sample.get("source") if pe is not None else None,
                price_source=source, stale=values["price_date"] != as_of or sample.get("as_of") != as_of or cap is not None and date != as_of)
            quote = sample.get("daily_quote") or {}
            if values["close"] is None and _number(quote.get("close")):
                values.update(close=_number(quote["close"]), price_date=quote.get("as_of"), price_source=sample.get("source"))
            metrics[code] = values
            if sample.get("as_of") != as_of or cap is None or pe is None:
                needs.append(code)
    for node in nodes:
        node["metrics"] = dict(metrics.get(node.get("symbol")) or {}) if node.get("market") == "US" else {}
        if node.get("market_cap_usd") is not None and node["metrics"].get("market_cap_usd") is None:
            node["metrics"].update(market_cap_usd=node["market_cap_usd"], market_cap_date=node.get("market_cap_date"))
    status = cache_get(f"supply_chain:refresh:v1:{symbol}") or {"status": "idle"}
    if status.get("status") in {"queued", "running"} and symbol not in _ACTIVE:
        status = {**status, "status": "interrupted"}
    return {"symbol": symbol, "as_of": as_of, "relationship_as_of": company.get("as_of"),
            "nodes": nodes, "edges": edges, "refresh": status, "needs_metrics": needs,
            "relationship_status": company.get("status"),
            "scope_note": "已归档公司关系；不是完整供应链。证券身份核验不等于供应关系已披露。",
            "return_note": "近3个月为缓存未复权收盘价变动，不含股息；异常跳变需人工核验，非总收益。"}


def start_refresh(symbol, slots):
    symbol = gil._safe_symbol(symbol)
    key = f"supply_chain:refresh:v1:{symbol}"
    with _LOCK:
        previous = cache_get(key) or {}
        if symbol in _ACTIVE:
            return {"started": False, "status": "running"}
        if time.time() - float(previous.get("finished_timestamp") or 0) < 300:
            return {"started": False, **previous}
        if not slots.acquire(blocking=False):
            return {"started": False, "status": "busy"}
        _ACTIVE.add(symbol)
        try:
            cache_set(key, {"status": "queued"})
        except Exception:
            _ACTIVE.discard(symbol)
            slots.release()
            raise
    def worker():
        try:
            graph = build_graph(symbol)
            requested = graph["needs_metrics"][:20]
            cache_set(key, {"status": "running", "requested": len(requested)})
            result = gil.refresh_references(requested, graph["as_of"]) if requested else {"status": "cached"}
            remaining = len(build_graph(symbol)["needs_metrics"])
            status = "completed" if not remaining and result.get("status") in {"completed", "cached"} else "partial"
            cache_set(key, {"status": status, "source_status": result.get("status"), "requested": len(requested),
                            "remaining": remaining, "finished_timestamp": time.time(),
                            "finished_at": datetime.now(timezone.utc).isoformat()})
        except Exception as exc:
            cache_set(key, {"status": "failed", "error": type(exc).__name__, "finished_timestamp": time.time()})
        finally:
            with _LOCK:
                _ACTIVE.discard(symbol)
            slots.release()
    try:
        threading.Thread(target=worker, daemon=True, name="supply-chain-metrics").start()
    except Exception:
        with _LOCK:
            _ACTIVE.discard(symbol)
        slots.release()
        cache_set(key, {"status": "failed", "finished_timestamp": time.time()})
        raise
    return {"started": True, "status": "queued"}
