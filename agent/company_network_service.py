"""Company identities and dated industry references, never trading-score inputs."""
from __future__ import annotations

import json
import hashlib
import re
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import gildata_shadow_service as gil
import requests
from app_database import cache_get, cache_set, connection, reference_evidence_append
from market_data_service import external_data_allowed

RELATIONS = ("upstream_suppliers", "downstream_customers", "competitors")
_EVIDENCE_FILE = Path(__file__).parent / "config" / "company_relationship_evidence.json"
_LOCK = threading.Lock()


def _name(value):
    value = unicodedata.normalize("NFKC", str(value or "")).casefold().split(" - ")[0]
    value = re.sub(r"\b(?:incorporated|corporation|corp|inc|holdings|limited|ltd|llc|plc|co)\b\.?", "", value)
    value = value.replace(".com", "").replace("股份有限公司", "").replace("有限公司", "").replace("公司", "")
    return re.sub(r"[^\w\u4e00-\u9fff]", "", value)


def _company_name(value):
    value = str(value or "").strip()[:160]
    if not value or value in {"暂无", "未知", "None", "-"} or "推断" in value:
        return None
    if re.search(r"(?:运营商|供应商|设备商|制造商|行业客户)$", value):
        return None
    return value


def _url(value):
    try:
        parts = urlsplit(str(value or ""))
        host = parts.hostname or ""
        if (parts.scheme not in {"https", "http"} or parts.username or parts.password
                or not host or "." not in host or host.endswith((".local", ".internal"))
                or re.fullmatch(r"[\d.]+", host) or ":" in host or parts.port not in {None, 80, 443}):
            return None
        return str(value)
    except ValueError:
        return None


def _stored_companies():
    # One indexed cache query, not one lookup per possible industry member.
    with connection() as conn:
        rows = conn.execute("SELECT payload_json FROM kv_cache WHERE cache_key LIKE 'gildata:research:company:%' "
                            "AND (expires_at IS NULL OR expires_at > ?) LIMIT 5000", (time.time(),)).fetchall()
    out = []
    for row in rows:
        try:
            value = json.loads(row[0])
            if isinstance(value, dict) and value.get("symbol") and value.get("name"):
                out.append(value)
        except (ValueError, TypeError):
            continue
    return out


def parse_identities(results, listings=None):
    identities = {}
    for api_name in ("美股公司简介", "公司简介", "港股公司简介"):
        for row in gil._table(results, api_name):
            code = str(row.get("股票代码") or row.get("聚源代码") or "").upper()
            if api_name == "美股公司简介":
                if not gil._SYMBOL.fullmatch(code) or not row.get("公司英文名称"):
                    continue
                market = "US"
                names = [row.get(k) for k in ("公司英文名称", "公司英文简称", "股票简称", "公司中文名称")]
                website = row.get("链接地址")
            elif api_name == "公司简介":
                if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code) or not row.get("上市日期"):
                    continue
                market = "CN"
                names = [row.get(k) for k in ("股票名称", "中文名称", "英文名称")]
                website = row.get("公司网站")
            else:
                # A company introduction alone does not verify its listing status.
                listing = next((r for r in gil._table(results, "港股IPO发行情况")
                                if r.get("股票代码") == code and r.get("上市状态") == "上市"), None)
                if not re.fullmatch(r"\d{5}", code) or not listing:
                    continue
                market = "HK"
                names = [row.get(k) for k in ("股票名称", "公司名称")]
                website = row.get("公司网址")
            aliases = list(dict.fromkeys(str(n).strip() for n in names if gil._text(n)))
            if not aliases:
                continue
            listed = (listings or {}).get(code, {})
            verified = market != "US" or (listed.get("etf") == "N" and
                _name(listed.get("name")).startswith(_name(aliases[0])) and
                not re.search(r"warrant|preferred|\bnotes\b|\bunits\b|\brights\b", listed.get("name", ""), re.I))
            identities[market, code] = {"name": aliases[0], "aliases": aliases + [code], "symbol": code,
                                       "market": market, "website": _url(website),
                                       "identity_source": f"gildata:{api_name}", "listing_verified": bool(verified)}
    return list(identities.values())


def resolve_company(name, identities):
    # Only an unambiguous name match receives a security link. No ticker guesses.
    terms = [_name(t) for t in re.split(r"[()（）]", name) if _name(t)]
    # Explicit parent/name before parentheses wins over unrelated brand matches.
    for term in terms:
        exact, prefix = [], []
        for identity in identities:
            aliases = [_name(n) for n in identity.get("aliases", [identity.get("name")])]
            if term in aliases:
                exact.append(identity)
            elif len(term) >= 4 and any(alias.startswith(term) for alias in aliases):
                prefix.append(identity)
        matches = exact or prefix
        # Prefer one US listing for a dual-listed company, otherwise one CN listing.
        for market in ("US", "CN", "HK"):
            group = {r["symbol"]: r for r in matches if r["market"] == market}
            if len(group) == 1:
                return {**next(iter(group.values())), "display_name": name}
            if len(group) > 1:
                return None
    return None


def directory_identities(listings):
    return [{"name": row["name"].split(" - ")[0], "aliases": [row["name"].split(" - ")[0], code],
             "symbol": code, "market": "US", "listing_verified": True, "identity_source": "nasdaq:symbol_directory"}
            for code, row in listings.items() if row.get("etf") == "N" and gil._SYMBOL.fullmatch(code)
            and row.get("name") and not re.search(r"warrant|preferred|\bnotes\b|\bunits\b|\brights\b", row["name"], re.I)]


def relation_evidence(symbol):
    try:
        data = json.loads(_EVIDENCE_FILE.read_text(encoding="utf-8"))
        return data.get("symbols", {}).get(symbol, {})
    except (OSError, ValueError):
        return {}


def relation_rows(symbol, profile, identities):
    evidence = relation_evidence(symbol)
    groups, omitted = {}, {}
    for kind in RELATIONS:
        declared = [dict(r, relationship_status="disclosed", source_url=_url(r.get("source_url") or evidence.get("source_url")),
                         source_title=r.get("source_title") or evidence.get("source_title"),
                         evidence_date=r.get("evidence_date") or evidence.get("disclosed_at"))
                    for r in evidence.get(kind, []) if isinstance(r, dict) and _company_name(r.get("name"))]
        researched = ((cache_get(f"company_relationship_research:v1:{symbol}") or {}).get("relationships") or {}).get(kind) or []
        legacy = [{"name": name, "relationship_status": "ai_unverified"}
                  for value in profile.get(kind, []) if (name := _company_name(value))]
        rows, seen = [], set()
        for item in declared + researched + legacy:
            resolved = resolve_company(item["name"], identities)
            ident = (resolved["market"], resolved["symbol"]) if resolved else _name(item["name"])
            if ident in seen:
                continue
            seen.add(ident)
            rows.append({**(resolved or {"display_name": item["name"], "listing_verified": False}), **item})
        groups[kind] = rows[:16]
        omitted[kind] = sum(1 for value in profile.get(kind, []) if not _company_name(value))
    return groups, omitted, evidence.get("upstream_note")


def industry_rows(symbol, company, companies, caps, direct_peers, as_of, listings=None):
    rows = []
    for peer in companies:
        code = peer.get("symbol")
        if code == symbol or not code or not gil._SYMBOL.fullmatch(code):
            continue
        factset = bool(company.get("factset_industry") and peer.get("factset_industry") == company["factset_industry"])
        sic = bool(company.get("sic_industry") and peer.get("sic_industry") == company["sic_industry"])
        if not factset and not sic:
            continue
        cap = caps.get(code) or {}
        value = gil._number(cap.get("market_cap_usd"))
        if not value or not 0 < value < 1e15 or cap.get("as_of") != as_of:
            continue
        rows.append({"symbol": code, "name": peer["name"], "display_name": peer["name"], "market": "US",
                     "listing_verified": ((listings or {}).get(code, {}).get("etf") == "N" and
                                          _name((listings or {}).get(code, {}).get("name")).startswith(_name(peer["name"]))),
                     "identity_source": "gildata:美股公司简介",
                     "market_cap_usd": value, "market_cap_date": as_of,
                     "match_basis": [label for match, label in ((factset, "FactSet同行业"), (sic, "SIC同行业")) if match],
                     "peer_type": "curated_track_peer" if code in direct_peers else "industry_reference"})
    rows.sort(key=lambda r: (-r["market_cap_usd"], r["symbol"]))
    return list({r["symbol"]: r for r in rows}.values())[:5]


def _profile_signature(profile):
    values = {kind: profile.get(kind) or [] for kind in RELATIONS}
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def cached_network(symbol, profile):
    value = cache_get(f"company_network:v1:{symbol}")
    attempt = cache_get(f"company_network:attempt:v1:{symbol}:{profile.get('market_session')}") or {}
    running = isinstance(attempt, dict) and attempt.get("status") == "running"
    if isinstance(value, dict):
        if profile.get("ai_available"):
            identities = cache_get("company_network:identities:v1") or []
            groups, omitted, note = relation_rows(symbol, profile, identities)
            value = {**value, "relationships": groups, "omitted_categories": omitted, "upstream_note": note}
        return {**value, "relationship_research": value.get("relationship_research") or {},
                "stale": value.get("as_of") != profile.get("market_session"),
                "refresh_status": "running" if running else "idle"}
    identities = cache_get("company_network:identities:v1") or []
    groups, omitted, note = relation_rows(symbol, profile, identities)
    return {"status": "not_updated", "refresh_status": "running" if running else "idle", "relationships": groups, "omitted_categories": omitted,
            "upstream_note": note, "industry_peers": [], "research_only": True}


def refresh_network(symbol, profile, as_of, *, retry=False):
    """Called only by the existing background warmer; two bounded MCP requests."""
    if not gil.research_enabled() or not external_data_allowed():
        return {"status": "disabled_or_cache_only"}
    symbol, as_of = gil._safe_symbol(symbol), gil._date(as_of)
    key = f"company_network:v1:{symbol}"
    attempt_key = f"company_network:attempt:v1:{symbol}:{as_of}"
    with _LOCK:
        attempt = cache_get(attempt_key)
        previous = cache_get(key) or {}
        unchanged = previous.get("profile_signature") == _profile_signature(profile)
        if attempt and (attempt.get("status") == "running" or (not retry and unchanged)):
            return {"status": "cached_or_running"}
        cache_set(attempt_key, {"status": "running"}, ttl_seconds=120)
    errors = []
    companies = _stored_companies()
    target = next((r for r in companies if r["symbol"] == symbol), {})
    from peer_earnings_signal_service import PEER_GROUPS, GROUP_SUBLANES
    direct = [s for g in PEER_GROUPS if symbol in g["symbols"]
              for lanes in [GROUP_SUBLANES.get(g["id"], {})] if lanes.get(symbol)
              for s in g["symbols"] if s != symbol and lanes.get(s) == lanes[symbol]]
    names = list(dict.fromkeys(name for kind in RELATIONS for value in profile.get(kind, [])
                              if (name := _company_name(value))))
    for kind in RELATIONS:
        names += [r["name"] for r in relation_evidence(symbol).get(kind, [])]
    seeds = list(dict.fromkeys([symbol] + names))[:20]
    identities = cache_get("company_network:identities:v1") or []
    listings = cache_get("company_network:us_listings:v1") or {}
    if not listings:
        try:
            from option_volume_leaders_service import _listing_directory
            with requests.Session() as session:
                listings = _listing_directory(session)
            cache_set("company_network:us_listings:v1", listings, ttl_seconds=86400)
        except Exception as exc:
            errors.append("listing_directory_" + type(exc).__name__)
    official = directory_identities(listings)
    base = [r for seed in seeds if (r := resolve_company(seed, official))]
    identities = list({(r["market"], r["symbol"]): r for r in identities + base}.values())
    try:
        results, _ = gil._query("FinQuery", "查询这些具体公司的基础资料：" + ",".join(seeds) +
            "。返回公司名称、证券代码、上市日期、上市市场/状态、官网，美股FactSet和SIC行业。"
            "中美市场分表，不要替换公司、不猜测供应商客户或母子关系，非上市或未知保留为空。", read_timeout=30, max_attempts=2)
        fresh_identities = parse_identities(results, listings)
        requested = {(r["market"], r["symbol"]) for seed in seeds if (r := resolve_company(seed, fresh_identities))}
        fresh_identities = [r for r in fresh_identities if (r["market"], r["symbol"]) in requested]
        if not fresh_identities:
            errors.append("identity_rows_missing")
        with _LOCK:
            current = cache_get("company_network:identities:v1") or []
            merged = {(r["market"], r["symbol"]): r for r in current + base + fresh_identities}
            identities = list(merged.values())[-5000:]
            cache_set("company_network:identities:v1", identities)
        fetched = datetime.now(timezone.utc).isoformat()
        for identity in fresh_identities:
            if identity["market"] != "US":
                continue
            company = gil.normalize_company(identity["symbol"], results)
            if company:
                company.update(fetched_at=fetched, research_only=True, point_in_time_verified=False)
                cache_set(f"gildata:research:company:{company['symbol']}", company, ttl_seconds=7 * 86400)
                reference_evidence_append(company["symbol"], "company", as_of, company)
        companies = _stored_companies()
        target = next((r for r in companies if r["symbol"] == symbol), target)
    except Exception as exc:
        errors.append(type(exc).__name__)
    matches = [r for r in companies if r["symbol"] != symbol and any(
        target.get(k) and r.get(k) == target[k] for k in ("factset_industry", "sic_industry"))]
    matches.sort(key=lambda r: (r["symbol"] not in direct, r["symbol"]))
    cap_symbols = [r["symbol"] for r in matches][:20]
    caps = {}
    if cap_symbols:
        try:
            results, _ = gil._query("FinQuery", f"仅查询美股{','.join(cap_symbols)}在{as_of}的美元日行情总市值、币种，"
                                   "以及美股价值分析总市值，列明交易日期及万元/亿元单位；缺失留空。", read_timeout=25, max_attempts=2)
            caps = {s: gil.normalize_equity(s, as_of, results, []) for s in cap_symbols}
        except Exception as exc:
            errors.append(type(exc).__name__)
    if retry:
        from company_relationship_research import research_relationships
        research_relationships(symbol, profile, identities)
    groups, omitted, note = relation_rows(symbol, profile, identities)
    payload = {"status": "partial" if errors else "completed", "as_of": as_of,
               "relationship_research": cache_get(f"company_relationship_research:v1:{symbol}") or {},
               "fetched_at": datetime.now(timezone.utc).isoformat(), "relationships": groups,
               "omitted_categories": omitted, "upstream_note": note,
               "industry_peers": industry_rows(symbol, target, companies, caps, direct, as_of, listings),
               "industry_candidate_count": len(matches), "market_cap_checked_count": len(cap_symbols),
               "coverage_note": "已归档行业资料和既有赛道候选范围内的同日市值前5；不是全行业、全美市场排名。",
               "errors": errors, "research_only": True, "does_not_change_scores": True}
    payload["profile_signature"] = _profile_signature(profile)
    previous = cache_get(key) or {}
    if errors:
        retained = []
        for kind in RELATIONS:
            old_rows = (previous.get("relationships") or {}).get(kind) or []
            if not groups[kind] and old_rows:
                groups[kind] = old_rows
                retained.append(kind)
        if retained:
            payload["relationships_retained_as_of"] = previous.get("as_of")
    if not payload["industry_peers"] and matches:
        errors.append("market_cap_not_verified")
        payload["status"] = "partial"
    if errors and not payload["industry_peers"] and previous.get("industry_peers"):
        payload["industry_peers"] = previous["industry_peers"]
        payload["coverage_note"] = "本期市值核验未完成，保留上次可比资料；请以各行行情日期为准，不作为本期行业排名。"
    cache_set(key, payload)
    reference_evidence_append(symbol, "company_network", as_of, payload)
    cache_set(attempt_key, {"status": payload["status"]}, ttl_seconds=600 if errors else 86400)
    return payload
