"""Peer earnings-lag research signals for US equities.

The scanner looks for a reported company whose earnings and post-report tape
validate a narrow business theme, then ranks close peers that have not yet
reported.  yfinance does not expose exchange-grade net buying, so dollar-volume
expansion is explicitly used as a flow proxy.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd
import yfinance as yf

from market_data_service import download_daily_history, get_company_fundamentals
from technical_signal_metrics import (
    calculate_atr,
    calculate_avg_dollar_volume,
    calculate_relative_momentum,
    calculate_volume_ratio,
    compute_daily_tunnel_score,
)


PEER_GROUPS: list[dict[str, Any]] = [
    {"id": "enterprise_servers", "label": "AI 服务器与企业计算", "similarity": 0.96, "symbols": ["DELL", "HPE", "SMCI", "VRT"]},
    {"id": "ai_accelerators", "label": "AI 加速器与高性能芯片", "similarity": 0.90, "symbols": ["NVDA", "AMD", "AVGO", "MRVL", "INTC"]},
    {"id": "memory_storage", "label": "存储与内存芯片", "similarity": 0.94, "symbols": ["MU", "WDC", "STX", "SNDK"]},
    {"id": "semi_equipment", "label": "半导体设备", "similarity": 0.91, "symbols": ["AMAT", "LRCX", "KLAC", "ASML", "TER"]},
    {"id": "networking_optics", "label": "数据中心网络与光互联", "similarity": 0.88, "symbols": ["ANET", "CSCO", "CIEN", "COHR", "LITE", "GLW"]},
    {"id": "data_center_power", "label": "数据中心供配电与散热", "similarity": 0.90, "symbols": ["VRT", "ETN", "PWR", "GEV", "NVT"]},
    {"id": "hyperscale_cloud", "label": "云平台与 AI 基础设施", "similarity": 0.86, "symbols": ["MSFT", "AMZN", "GOOGL", "ORCL", "META"]},
    {"id": "cybersecurity", "label": "网络安全平台", "similarity": 0.91, "symbols": ["CRWD", "PANW", "FTNT", "ZS", "OKTA", "S"]},
    {"id": "enterprise_software", "label": "企业软件与数据平台", "similarity": 0.84, "symbols": ["CRM", "NOW", "SNOW", "DDOG", "MDB", "PLTR"]},
    {"id": "digital_ads", "label": "数字广告平台", "similarity": 0.83, "symbols": ["META", "GOOGL", "PINS", "SNAP", "TTD"]},
    {"id": "fintech_brokers", "label": "金融科技与互联网券商", "similarity": 0.84, "symbols": ["HOOD", "SOFI", "COIN", "AFRM", "PYPL", "XYZ"]},
    {"id": "crypto_miners", "label": "比特币矿企与算力基础设施", "similarity": 0.92, "symbols": ["IREN", "RIOT", "MARA", "CLSK", "WULF", "CORZ"]},
    {"id": "ev_automakers", "label": "电动车整车", "similarity": 0.88, "symbols": ["TSLA", "RIVN", "LCID", "NIO", "XPEV", "LI"]},
    {"id": "airlines", "label": "美国航空公司", "similarity": 0.92, "symbols": ["DAL", "UAL", "AAL", "LUV", "ALK", "JBLU"]},
    {"id": "cruise_lines", "label": "邮轮运营", "similarity": 0.96, "symbols": ["CCL", "RCL", "NCLH", "VIK"]},
    {"id": "retail_discounters", "label": "折扣零售", "similarity": 0.91, "symbols": ["WMT", "COST", "TGT", "DG", "DLTR", "BJ"]},
    {"id": "home_improvement", "label": "家装零售", "similarity": 0.96, "symbols": ["HD", "LOW", "FND"]},
    {"id": "apparel_sportswear", "label": "运动服饰与鞋类", "similarity": 0.88, "symbols": ["NKE", "LULU", "DECK", "ONON", "UAA"]},
    {"id": "banks_money_center", "label": "大型银行", "similarity": 0.92, "symbols": ["JPM", "BAC", "C", "WFC", "GS", "MS"]},
    {"id": "payments", "label": "支付网络与收单", "similarity": 0.89, "symbols": ["V", "MA", "AXP", "PYPL", "XYZ", "FISV"]},
    {"id": "oil_majors", "label": "综合能源", "similarity": 0.91, "symbols": ["XOM", "CVX", "COP", "OXY", "EOG"]},
    {"id": "uranium_nuclear", "label": "核能与铀产业链", "similarity": 0.82, "symbols": ["CCJ", "LEU", "BWXT", "SMR", "OKLO", "NNE"]},
    {"id": "defense_aerospace", "label": "国防与航空航天", "similarity": 0.86, "symbols": ["LMT", "RTX", "NOC", "GD", "LHX", "KTOS", "AVAV"]},
    {"id": "biotech_tools", "label": "生命科学工具", "similarity": 0.87, "symbols": ["TMO", "DHR", "A", "ILMN", "WAT"]},
]

_cache_lock = threading.Lock()
_memory_cache: dict[str, tuple[float, dict[str, Any]]] = {}
PEER_MATCH_VERSION = 9
_CACHE_VERSION = PEER_MATCH_VERSION
_DEFAULT_OVERRIDES_PATH = Path(__file__).resolve().parent / "data" / "peer_earnings_calendar_overrides.json"
_EXTENDED_CATALOG_PATH = Path(__file__).resolve().parent / "data" / "peer_similarity_catalog_extensions.json"

# Every broad discovery group is refined into investable sublanes. This keeps
# the rule general: a new signal must match the economic engine of its leader,
# not merely share a loose sector label.
GROUP_SUBLANES: dict[str, dict[str, str]] = {
    "enterprise_servers": {"DELL": "enterprise_compute", "HPE": "enterprise_compute", "SMCI": "ai_server", "VRT": "data_center_infrastructure"},
    "ai_accelerators": {"NVDA": "ai_accelerator", "AMD": "ai_accelerator", "AVGO": "custom_ai_connectivity", "MRVL": "custom_ai_connectivity", "INTC": "general_compute"},
    "memory_storage": {"MU": "memory", "WDC": "storage", "STX": "storage", "SNDK": "storage"},
    "semi_equipment": {"AMAT": "wafer_fab", "LRCX": "wafer_fab", "KLAC": "process_control", "ASML": "lithography", "TER": "test_equipment"},
    "networking_optics": {"ANET": "networking", "CSCO": "networking", "CIEN": "optical_networking", "COHR": "optics_components", "LITE": "optics_components", "GLW": "fiber_materials"},
    "data_center_power": {"VRT": "data_center_power", "ETN": "electrical_equipment", "PWR": "grid_engineering", "GEV": "grid_equipment", "NVT": "electrical_components"},
    "hyperscale_cloud": {"MSFT": "hyperscale_cloud", "AMZN": "hyperscale_cloud", "GOOGL": "hyperscale_cloud", "ORCL": "enterprise_cloud", "META": "consumer_platform"},
    "cybersecurity": {"CRWD": "endpoint_cloud_security", "PANW": "security_platform", "FTNT": "network_security", "ZS": "zero_trust", "OKTA": "identity", "S": "endpoint_cloud_security"},
    "enterprise_software": {"CRM": "enterprise_apps", "NOW": "workflow_apps", "SNOW": "data_platform", "DDOG": "observability", "MDB": "database", "PLTR": "ai_data_platform"},
    "digital_ads": {"META": "scaled_ads_platform", "GOOGL": "scaled_ads_platform", "PINS": "social_ads", "SNAP": "social_ads", "TTD": "adtech_dsp"},
    "fintech_brokers": {"HOOD": "retail_broker", "SOFI": "digital_finance", "COIN": "crypto_exchange", "AFRM": "bnpl", "PYPL": "wallet_payments", "XYZ": "merchant_fintech"},
    "crypto_miners": {"IREN": "bitcoin_miner", "RIOT": "bitcoin_miner", "MARA": "bitcoin_miner", "CLSK": "bitcoin_miner", "WULF": "bitcoin_miner", "CORZ": "bitcoin_miner"},
    "ev_automakers": {"TSLA": "global_ev", "RIVN": "us_ev", "LCID": "us_ev", "NIO": "china_ev", "XPEV": "china_ev", "LI": "china_ev"},
    "airlines": {"DAL": "network_airline", "UAL": "network_airline", "AAL": "network_airline", "LUV": "low_cost_airline", "ALK": "regional_airline", "JBLU": "low_cost_airline"},
    "cruise_lines": {"CCL": "cruise_operator", "RCL": "cruise_operator", "NCLH": "cruise_operator", "VIK": "premium_cruise"},
    "retail_discounters": {"WMT": "mass_discount", "COST": "warehouse_club", "TGT": "mass_discount", "DG": "dollar_store", "DLTR": "dollar_store", "BJ": "warehouse_club"},
    "home_improvement": {"HD": "home_improvement", "LOW": "home_improvement", "FND": "specialty_flooring"},
    "apparel_sportswear": {"NKE": "mass_athletic", "UAA": "mass_athletic", "LULU": "premium_athleisure", "ONON": "performance_running", "DECK": "performance_footwear"},
    "banks_money_center": {"JPM": "money_center_bank", "BAC": "money_center_bank", "C": "money_center_bank", "WFC": "money_center_bank", "GS": "capital_markets_bank", "MS": "capital_markets_bank"},
    "payments": {"V": "card_network", "MA": "card_network", "AXP": "closed_loop_card", "PYPL": "wallet_payments", "XYZ": "merchant_acquiring", "FISV": "merchant_acquiring"},
    "oil_majors": {"XOM": "integrated_energy", "CVX": "integrated_energy", "COP": "upstream", "OXY": "upstream", "EOG": "upstream"},
    "uranium_nuclear": {"CCJ": "uranium_miner", "LEU": "nuclear_fuel", "BWXT": "nuclear_components", "SMR": "advanced_reactor", "OKLO": "advanced_reactor", "NNE": "advanced_reactor"},
    "defense_aerospace": {"LMT": "prime_defense", "RTX": "prime_defense", "NOC": "prime_defense", "GD": "prime_defense", "LHX": "defense_electronics", "KTOS": "defense_drones", "AVAV": "defense_drones"},
    "biotech_tools": {"TMO": "life_science_tools", "DHR": "life_science_tools", "A": "life_science_tools", "ILMN": "sequencing", "WAT": "analytical_instruments"},
}

SUBLANE_DISPLAY_NAMES: dict[str, str] = {
    "enterprise_compute": "企业计算整机",
    "ai_server": "AI 服务器",
    "data_center_infrastructure": "数据中心基础设施",
    "ai_accelerator": "AI 加速芯片",
    "custom_ai_connectivity": "定制芯片与高速互联",
    "general_compute": "通用计算芯片",
    "memory": "内存芯片",
    "storage": "存储设备",
    "wafer_fab": "晶圆制造设备",
    "process_control": "过程控制与检测",
    "lithography": "光刻设备",
    "test_equipment": "半导体测试设备",
    "networking": "数据中心网络设备",
    "optical_networking": "光通信网络",
    "optics_components": "光模块与光器件",
    "fiber_materials": "光纤与材料",
    "data_center_power": "数据中心供配电",
    "electrical_equipment": "电气设备",
    "grid_engineering": "电网工程建设",
    "grid_equipment": "电网设备",
    "electrical_components": "电气元件",
    "hyperscale_cloud": "超大规模云平台",
    "enterprise_cloud": "企业云服务",
    "consumer_platform": "消费者平台",
    "endpoint_cloud_security": "云端终端安全",
    "security_platform": "安全平台",
    "network_security": "网络安全设备",
    "zero_trust": "零信任安全",
    "identity": "身份认证安全",
    "enterprise_apps": "企业应用软件",
    "workflow_apps": "工作流软件",
    "data_platform": "数据平台",
    "observability": "可观测性平台",
    "database": "数据库",
    "ai_data_platform": "AI 数据平台",
    "scaled_ads_platform": "大型广告平台",
    "social_ads": "社交广告平台",
    "adtech_dsp": "广告技术 DSP",
    "retail_broker": "互联网券商",
    "digital_finance": "数字金融",
    "crypto_exchange": "加密资产交易平台",
    "bnpl": "先买后付",
    "wallet_payments": "电子钱包支付",
    "merchant_fintech": "商户金融科技",
    "bitcoin_miner": "比特币矿企",
    "global_ev": "全球电动车龙头",
    "us_ev": "美国电动车新势力",
    "china_ev": "中国电动车新势力",
    "network_airline": "网络型航空公司",
    "low_cost_airline": "低成本航空",
    "regional_airline": "区域航空",
    "cruise_operator": "邮轮运营商",
    "premium_cruise": "高端邮轮",
    "mass_discount": "大众折扣零售",
    "warehouse_club": "会员仓储零售",
    "dollar_store": "一元店/低价零售",
    "home_improvement": "家装建材零售",
    "specialty_flooring": "专业地板零售",
    "mass_athletic": "大众运动品牌",
    "premium_athleisure": "高端运动休闲",
    "performance_running": "专业跑步品牌",
    "performance_footwear": "功能性鞋履",
    "money_center_bank": "大型综合银行",
    "capital_markets_bank": "资本市场型银行",
    "card_network": "银行卡清算网络",
    "closed_loop_card": "闭环信用卡网络",
    "merchant_acquiring": "商户收单",
    "integrated_energy": "综合能源公司",
    "upstream": "上游油气勘探生产",
    "uranium_miner": "铀矿商",
    "nuclear_fuel": "核燃料",
    "nuclear_components": "核电设备部件",
    "advanced_reactor": "先进核反应堆",
    "prime_defense": "主承包国防军工",
    "defense_electronics": "国防电子",
    "defense_drones": "无人系统",
    "life_science_tools": "生命科学工具",
    "sequencing": "基因测序",
    "analytical_instruments": "分析仪器",
    "managed_care": "管理式医疗保险",
}


def _sublane_label(value: Any) -> str:
    key = str(value or "")
    return SUBLANE_DISPLAY_NAMES.get(key, key.replace("_", " ") if key else "未标注")

GROUP_AUDIENCES: dict[str, dict[str, str]] = {
    "apparel_sportswear": {"NKE": "mass_global", "UAA": "mass_global", "LULU": "premium_direct", "ONON": "premium_running", "DECK": "premium_footwear"},
    "ev_automakers": {"TSLA": "global", "RIVN": "us", "LCID": "us", "NIO": "china", "XPEV": "china", "LI": "china"},
    "retail_discounters": {"WMT": "mass", "TGT": "mass", "COST": "membership", "BJ": "membership", "DG": "value", "DLTR": "value"},
    "payments": {"V": "global_network", "MA": "global_network", "AXP": "premium_consumer", "PYPL": "consumer_wallet", "XYZ": "merchant", "FISV": "merchant"},
}

# Adjacent sublanes are explicit and auditable. Scores below 0.55 remain useful
# as context but cannot become a relay signal. An unlisted cross-sublane pair is
# intentionally treated as unrelated instead of receiving a generous default.
GROUP_ADJACENT_SUBLANES: dict[str, dict[frozenset[str], float]] = {
    "enterprise_servers": {frozenset(("enterprise_compute", "ai_server")): 0.74, frozenset(("ai_server", "data_center_infrastructure")): 0.50},
    "ai_accelerators": {frozenset(("ai_accelerator", "custom_ai_connectivity")): 0.70, frozenset(("ai_accelerator", "general_compute")): 0.48},
    "memory_storage": {frozenset(("memory", "storage")): 0.48},
    "semi_equipment": {frozenset(("wafer_fab", "process_control")): 0.72, frozenset(("wafer_fab", "lithography")): 0.68, frozenset(("wafer_fab", "test_equipment")): 0.58, frozenset(("process_control", "lithography")): 0.62},
    "networking_optics": {frozenset(("networking", "optical_networking")): 0.68, frozenset(("optical_networking", "optics_components")): 0.76, frozenset(("optics_components", "fiber_materials")): 0.62},
    "data_center_power": {frozenset(("data_center_power", "electrical_equipment")): 0.72, frozenset(("electrical_equipment", "electrical_components")): 0.70, frozenset(("grid_engineering", "grid_equipment")): 0.72, frozenset(("electrical_equipment", "grid_equipment")): 0.62},
    "hyperscale_cloud": {frozenset(("hyperscale_cloud", "enterprise_cloud")): 0.68, frozenset(("hyperscale_cloud", "consumer_platform")): 0.46},
    "cybersecurity": {frozenset(("endpoint_cloud_security", "security_platform")): 0.72, frozenset(("network_security", "security_platform")): 0.68, frozenset(("zero_trust", "security_platform")): 0.64, frozenset(("identity", "zero_trust")): 0.60},
    "enterprise_software": {frozenset(("data_platform", "ai_data_platform")): 0.68, frozenset(("database", "data_platform")): 0.62, frozenset(("observability", "data_platform")): 0.56, frozenset(("enterprise_apps", "workflow_apps")): 0.62},
    "digital_ads": {frozenset(("scaled_ads_platform", "social_ads")): 0.62, frozenset(("scaled_ads_platform", "adtech_dsp")): 0.58, frozenset(("social_ads", "adtech_dsp")): 0.54},
    "fintech_brokers": {frozenset(("wallet_payments", "merchant_fintech")): 0.58, frozenset(("retail_broker", "digital_finance")): 0.56},
    "ev_automakers": {frozenset(("global_ev", "us_ev")): 0.72, frozenset(("global_ev", "china_ev")): 0.56, frozenset(("us_ev", "china_ev")): 0.46},
    "airlines": {frozenset(("network_airline", "low_cost_airline")): 0.62, frozenset(("network_airline", "regional_airline")): 0.56, frozenset(("low_cost_airline", "regional_airline")): 0.58},
    "cruise_lines": {frozenset(("cruise_operator", "premium_cruise")): 0.70},
    "retail_discounters": {frozenset(("mass_discount", "warehouse_club")): 0.62, frozenset(("mass_discount", "dollar_store")): 0.56, frozenset(("warehouse_club", "dollar_store")): 0.44},
    "home_improvement": {frozenset(("home_improvement", "specialty_flooring")): 0.62},
    "apparel_sportswear": {frozenset(("performance_running", "performance_footwear")): 0.72, frozenset(("premium_athleisure", "performance_running")): 0.50, frozenset(("mass_athletic", "performance_footwear")): 0.46},
    "banks_money_center": {frozenset(("money_center_bank", "capital_markets_bank")): 0.62},
    "payments": {frozenset(("card_network", "closed_loop_card")): 0.66, frozenset(("wallet_payments", "merchant_acquiring")): 0.58, frozenset(("card_network", "merchant_acquiring")): 0.52},
    "oil_majors": {frozenset(("integrated_energy", "upstream")): 0.74},
    "uranium_nuclear": {frozenset(("uranium_miner", "nuclear_fuel")): 0.62, frozenset(("nuclear_fuel", "advanced_reactor")): 0.58, frozenset(("nuclear_components", "advanced_reactor")): 0.64},
    "defense_aerospace": {frozenset(("prime_defense", "defense_electronics")): 0.66, frozenset(("prime_defense", "defense_drones")): 0.56, frozenset(("defense_electronics", "defense_drones")): 0.60},
    "biotech_tools": {frozenset(("life_science_tools", "analytical_instruments")): 0.68, frozenset(("life_science_tools", "sequencing")): 0.56},
}


def _load_extended_catalog(path: Path = _EXTENDED_CATALOG_PATH) -> None:
    """Merge the maintainable long-tail industry map into the core catalog."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, TypeError, json.JSONDecodeError):
        return
    existing_ids = {str(group["id"]) for group in PEER_GROUPS}
    def number(value: Any, default: float) -> float:
        try:
            parsed = float(value)
            return parsed if math.isfinite(parsed) else default
        except (TypeError, ValueError):
            return default

    for raw_group in payload.get("groups") or []:
        group_id = str(raw_group.get("id") or "").strip()
        sublanes = {
            str(symbol).upper(): str(sublane)
            for symbol, sublane in dict(raw_group.get("sublanes") or {}).items()
            if str(symbol).strip() and str(sublane).strip()
        }
        if not group_id or group_id in existing_ids or len(sublanes) < 2:
            continue
        PEER_GROUPS.append({
            "id": group_id,
            "label": str(raw_group.get("label") or group_id),
            "similarity": number(raw_group.get("similarity"), 0.85),
            "symbols": list(sublanes),
        })
        GROUP_SUBLANES[group_id] = sublanes
        adjacent: dict[frozenset[str], float] = {}
        for row in raw_group.get("adjacent_sublanes") or []:
            lanes = [str(value) for value in (row.get("sublanes") or []) if str(value)]
            if len(lanes) == 2:
                adjacent[frozenset(lanes)] = number(row.get("similarity"), 0.24)
        if adjacent:
            GROUP_ADJACENT_SUBLANES[group_id] = adjacent
        existing_ids.add(group_id)


_load_extended_catalog()

_peer_profile_cache: dict[str, dict[str, Any]] = {}


def _slug(value: Any) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return text[:48] or "unknown"


def _peer_profile(symbol: str) -> dict[str, Any]:
    symbol = str(symbol or "").strip().upper()
    if not symbol:
        return {}
    cached = _peer_profile_cache.get(symbol)
    if cached is not None:
        return cached
    profile = get_company_fundamentals(symbol) or {}
    sector = profile.get("sector")
    industry = profile.get("industry")
    market_cap = profile.get("market_cap")
    try:
        info = yf.Ticker(symbol).info or {}
        sector = sector or info.get("sector")
        industry = industry or info.get("industry")
        market_cap = market_cap or info.get("marketCap")
    except Exception:
        pass
    result = {
        "symbol": symbol,
        "sector": sector or "Unknown",
        "industry": industry or sector or "Unknown",
        "market_cap": _finite_or_none(market_cap),
    }
    _peer_profile_cache[symbol] = result
    return result


def _static_peer_symbols() -> set[str]:
    return {symbol for group in PEER_GROUPS for symbol in group["symbols"]}


def _dynamic_peer_groups_for_symbols(symbols: Iterable[str], *, min_group_size: int = 2) -> list[dict[str, Any]]:
    """Build temporary same-industry peer groups for index constituents not covered by the curated map."""
    target_symbols = sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
    uncovered = [symbol for symbol in target_symbols if symbol not in _static_peer_symbols()]
    buckets: dict[str, list[str]] = {}
    labels: dict[str, str] = {}
    for symbol in uncovered:
        profile = _peer_profile(symbol)
        industry = str(profile.get("industry") or profile.get("sector") or "Unknown").strip()
        if not industry or industry == "Unknown":
            continue
        key = _slug(industry)
        buckets.setdefault(key, []).append(symbol)
        labels[key] = industry
    groups: list[dict[str, Any]] = []
    for key, members in sorted(buckets.items()):
        unique = sorted(set(members))
        if len(unique) < min_group_size:
            continue
        group_id = f"dynamic_industry_{key}"
        GROUP_SUBLANES[group_id] = {symbol: key for symbol in unique}
        groups.append({
            "id": group_id,
            "label": f"动态行业映射 · {labels.get(key, key)}",
            "similarity": 0.72,
            "symbols": unique,
            "dynamic": True,
        })
    return groups


def _peer_mapping_for_symbols(symbols: Iterable[str], groups: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    target = {str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()}
    mapping: dict[str, dict[str, Any]] = {}
    for group in groups:
        group_symbols = [str(symbol).upper() for symbol in group.get("symbols", [])]
        for symbol in group_symbols:
            if symbol not in target:
                continue
            peers = [peer for peer in group_symbols if peer != symbol and (not target or peer in target)]
            lanes = GROUP_SUBLANES.get(str(group.get("id") or ""), {})
            target_lane = lanes.get(symbol)
            valuation_peers = [peer for peer in group_symbols if peer != symbol and target_lane and lanes.get(peer) == target_lane]
            current = mapping.get(symbol)
            candidate = {
                "available": True,
                "group_id": group.get("id"),
                "group_label": group.get("label"),
                "mapping_source": "dynamic_industry" if group.get("dynamic") else "curated_peer_catalog",
                "peer_count": len(peers),
                "direct_peers": peers[:12],
                "target_sublane": target_lane,
                "valuation_peers": valuation_peers,
                "peer_sublanes": {peer: lanes.get(peer) for peer in valuation_peers},
                "coverage_note": "同行映射仅用于财报接力证据，不改变指数股票池成分。",
            }
            if current is None or (
                current.get("mapping_source") == "dynamic_industry"
                and candidate["mapping_source"] == "curated_peer_catalog"
            ):
                mapping[symbol] = candidate
    return mapping


def list_peer_groups() -> list[dict[str, Any]]:
    return [{
        "id": row["id"],
        "label": row["label"],
        "symbols": list(row["symbols"]),
        "sublanes": GROUP_SUBLANES.get(row["id"], {}),
        "adjacent_sublanes": [
            {"sublanes": sorted(pair), "similarity": similarity, "relay_eligible": similarity >= 0.55}
            for pair, similarity in GROUP_ADJACENT_SUBLANES.get(row["id"], {}).items()
        ],
    } for row in PEER_GROUPS]


def peer_catalog_summary() -> dict[str, int]:
    symbols = {symbol for group in PEER_GROUPS for symbol in group["symbols"]}
    sublanes = {lane for mapping in GROUP_SUBLANES.values() for lane in mapping.values()}
    adjacent_rules = sum(len(mapping) for mapping in GROUP_ADJACENT_SUBLANES.values())
    return {
        "version": PEER_MATCH_VERSION,
        "group_count": len(PEER_GROUPS),
        "symbol_count": len(symbols),
        "sublane_count": len(sublanes),
        "adjacent_rule_count": adjacent_rules,
    }


def peer_group_symbols(group_id: str = "all") -> list[str]:
    selected = PEER_GROUPS if group_id == "all" else [row for row in PEER_GROUPS if row["id"] == group_id]
    return sorted({symbol for row in selected for symbol in row["symbols"]})


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _finite_or_none(value: Any) -> float | None:
    number = _finite(value, float("nan"))
    return number if math.isfinite(number) else None


def _business_match(group_id: str, leader_symbol: str, target_symbol: str) -> dict[str, Any]:
    lanes = GROUP_SUBLANES.get(group_id, {})
    leader_lane = lanes.get(leader_symbol)
    target_lane = lanes.get(target_symbol)
    leader_label = _sublane_label(leader_lane)
    target_label = _sublane_label(target_lane)
    if not leader_lane or not target_lane:
        return {
            "score": 0.0,
            "relation": "画像缺失",
            "relation_display": "细分赛道画像缺失",
            "leader_sublane": leader_lane,
            "target_sublane": target_lane,
            "leader_sublane_label": leader_label,
            "target_sublane_label": target_label,
        }
    if leader_lane == target_lane:
        return {
            "score": 1.0,
            "relation": "直接同行",
            "relation_display": f"同一细分赛道：{leader_label}",
            "leader_sublane": leader_lane,
            "target_sublane": target_lane,
            "leader_sublane_label": leader_label,
            "target_sublane_label": target_label,
        }
    score = GROUP_ADJACENT_SUBLANES.get(group_id, {}).get(frozenset((leader_lane, target_lane)), 0.24)
    relation = "邻近赛道" if score >= 0.55 else "仅作观察"
    return {
        "score": score,
        "relation": relation,
        "relation_display": f"{relation}：{leader_label} → {target_label}",
        "leader_sublane": leader_lane,
        "target_sublane": target_lane,
        "leader_sublane_label": leader_label,
        "target_sublane_label": target_label,
    }


def _business_similarity(group_id: str, leader_symbol: str, target_symbol: str) -> float:
    return float(_business_match(group_id, leader_symbol, target_symbol)["score"])


def _audience_similarity(group_id: str, leader_symbol: str, target_symbol: str) -> float:
    audiences = GROUP_AUDIENCES.get(group_id, {})
    leader_audience = audiences.get(leader_symbol)
    target_audience = audiences.get(target_symbol)
    if not leader_audience or not target_audience:
        return 0.82
    return 1.0 if leader_audience == target_audience else 0.52


def _directional_scale_influence(leader: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    """Estimate whether earnings information can plausibly transmit leader -> target."""
    leader_cap = _finite(leader.get("market_cap"))
    target_cap = _finite(target.get("market_cap"))
    if leader_cap <= 0 or target_cap <= 0:
        return {"score": 0.68, "ratio": None, "direction": "市值待确认"}
    ratio = leader_cap / target_cap
    if ratio >= 1.0:
        score = min(1.0, 0.88 + 0.08 * math.log10(ratio))
        direction = "龙头向较小同行传导" if ratio >= 1.35 else "同量级同行传导"
    else:
        inverse_ratio = 1.0 / ratio
        score = max(0.12, 0.82 - 0.42 * math.log10(inverse_ratio))
        direction = "较小同行反推较大公司"
    return {"score": round(score, 3), "ratio": round(ratio, 3), "direction": direction}


def _tape_style(frame: pd.DataFrame) -> dict[str, float]:
    if frame.empty or "Close" not in frame or len(frame) < 12:
        return {"volatility": 0.0, "momentum": 0.0}
    close = frame["Close"].astype(float).dropna().tail(61)
    returns = close.pct_change().dropna()
    return {
        "volatility": _finite(returns.std()),
        "momentum": _finite(close.iloc[-1] / close.iloc[0] - 1.0) if len(close) >= 2 else 0.0,
    }


def _tape_style_similarity(leader_frame: pd.DataFrame, target_frame: pd.DataFrame) -> float:
    leader = _tape_style(leader_frame)
    target = _tape_style(target_frame)
    vol_gap = abs(leader["volatility"] - target["volatility"])
    momentum_gap = abs(leader["momentum"] - target["momentum"])
    return max(0.30, min(1.0, math.exp(-12.0 * vol_gap - 1.8 * momentum_gap)))


def _pair_similarity(
    group: dict[str, Any],
    leader_symbol: str,
    target_symbol: str,
    leader: dict[str, Any],
    target: dict[str, Any],
    histories: dict[str, pd.DataFrame],
) -> dict[str, Any]:
    group_id = str(group.get("id") or "")
    business_match = _business_match(group_id, leader_symbol, target_symbol)
    business = _finite(business_match["score"])
    audience = _audience_similarity(group_id, leader_symbol, target_symbol)
    directional = _directional_scale_influence(leader, target)
    tape_style = _tape_style_similarity(
        histories.get(leader_symbol, pd.DataFrame()),
        histories.get(target_symbol, pd.DataFrame()),
    )
    group_similarity = _finite(group.get("similarity"), 0.85)
    combined = group_similarity * (
        0.42 * business
        + 0.13 * audience
        + 0.30 * _finite(directional["score"])
        + 0.15 * tape_style
    )
    return {
        "business": round(business, 3),
        "sublane_relation": business_match["relation"],
        "sublane_relation_display": business_match["relation_display"],
        "leader_sublane": business_match["leader_sublane"],
        "target_sublane": business_match["target_sublane"],
        "leader_sublane_label": business_match["leader_sublane_label"],
        "target_sublane_label": business_match["target_sublane_label"],
        "audience": round(audience, 3),
        "directional_scale": round(_finite(directional["score"]), 3),
        "leader_to_target_market_cap_ratio": directional["ratio"],
        "transmission_direction": directional["direction"],
        "tape_style": round(tape_style, 3),
        "combined": round(max(0.0, min(1.0, combined)), 3),
    }


def _iso_day(value: Any) -> date | None:
    if value is None:
        return None
    try:
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is not None:
            stamp = stamp.tz_convert(None)
        return stamp.date()
    except (TypeError, ValueError):
        return None


def _ticker_frame(download: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if download.empty:
        return pd.DataFrame()
    if isinstance(download.columns, pd.MultiIndex):
        if ticker in download.columns.get_level_values(0):
            return download[ticker].dropna(how="all")
        if ticker in download.columns.get_level_values(-1):
            return download.xs(ticker, axis=1, level=-1).dropna(how="all")
    return download.dropna(how="all")


_EARNINGS_CACHE_PATH = Path(__file__).resolve().parent / "data" / "earnings_dates_cache.json"
_earnings_cache: dict[str, dict[str, Any]] = {}
_earnings_cache_loaded = False


def _get_earnings_from_finnhub(symbol: str, now: date) -> dict[str, Any] | None:
    """Get earnings date from Finnhub API (free tier available)."""
    import requests
    api_key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not api_key:
        return None

    try:
        # Query for next 90 days
        from_date = now.isoformat()
        to_date = (now + timedelta(days=90)).isoformat()
        url = f"https://finnhub.io/api/v1/calendar/earnings?from={from_date}&to={to_date}&symbol={symbol}&token={api_key}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        earnings = data.get("earningsCalendar", [])
        for item in earnings:
            if item.get("symbol", "").upper() == symbol.upper():
                report_date = _iso_day(item.get("date"))
                if report_date and report_date > now:
                    return {"date": report_date, "eps_surprise_pct": None}
    except Exception:
        pass
    return None


def _get_earnings_from_alpha_vantage(symbol: str, now: date) -> dict[str, Any] | None:
    """Get earnings date from Alpha Vantage API (25 calls/day free)."""
    import requests
    import csv
    import io

    api_key = os.environ.get("ALPHA_VANTAGE_API_KEY", "").strip()
    if not api_key:
        return None

    try:
        # Alpha Vantage returns CSV for earnings calendar
        url = f"https://www.alphavantage.co/query?function=EARNINGS_CALENDAR&horizon=3month&apikey={api_key}"
        response = requests.get(url, timeout=15)
        response.raise_for_status()

        # Parse CSV response
        reader = csv.DictReader(io.StringIO(response.text))
        for row in reader:
            if row.get("symbol", "").upper() == symbol.upper():
                report_date = _iso_day(row.get("reportDate"))
                if report_date and report_date > now:
                    return {"date": report_date, "eps_surprise_pct": None}
    except Exception:
        pass
    return None


def _load_earnings_cache() -> None:
    """Load earnings dates cache from file."""
    global _earnings_cache, _earnings_cache_loaded
    if _earnings_cache_loaded:
        return
    try:
        if _EARNINGS_CACHE_PATH.exists():
            data = json.loads(_EARNINGS_CACHE_PATH.read_text(encoding="utf-8"))
            _earnings_cache = data if isinstance(data, dict) else {}
    except Exception:
        _earnings_cache = {}
    _earnings_cache_loaded = True


def _save_earnings_cache() -> None:
    """Save earnings dates cache to file."""
    try:
        _EARNINGS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = _EARNINGS_CACHE_PATH.with_suffix(".tmp")
        temp.write_text(json.dumps(_earnings_cache, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(_EARNINGS_CACHE_PATH)
    except Exception:
        pass


def _earnings_record(symbol: str, now: date) -> dict[str, Any]:
    _load_earnings_cache()

    # Check cache first (valid for 7 days)
    cache_key = symbol.upper()
    if cache_key in _earnings_cache:
        cached = _earnings_cache[cache_key]
        cache_date = _iso_day(cached.get("_cache_date"))
        if cache_date and (now - cache_date).days <= 7:
            # If cached data has next_report_date, use it
            if cached.get("next_report_date"):
                return {k: v for k, v in cached.items() if k != "_cache_date"}

    # Try Finnhub first (more reliable for earnings dates, free tier available)
    upcoming = _get_earnings_from_finnhub(symbol, now)

    # If Finnhub fails, try Alpha Vantage
    if upcoming is None:
        upcoming = _get_earnings_from_alpha_vantage(symbol, now)

    # Try yfinance as last resort
    records: list[dict[str, Any]] = []
    latest = None
    ticker = None

    try:
        ticker = yf.Ticker(symbol)
        rows = ticker.get_earnings_dates(limit=12)
        if isinstance(rows, pd.DataFrame) and not rows.empty:
            for index, row in rows.iterrows():
                day = _iso_day(index)
                if day:
                    records.append({
                        "date": day,
                        "eps_surprise_pct": _finite(row.get("Surprise(%)")) / 100.0,
                    })
        records.sort(key=lambda item: item["date"])
        past = [item for item in records if item["date"] <= now]
        future = [item for item in records if item["date"] > now]
        latest = past[-1] if past else None

        # If we don't have upcoming from Finnhub/AV, use yfinance
        if upcoming is None and future:
            upcoming = future[0]

    except Exception as e:
        # If rate limited, continue with what we have
        pass

    # Try yfinance calendar as last resort for upcoming date
    if upcoming is None and ticker is not None:
        try:
            calendar = ticker.calendar
            raw_dates = calendar.get("Earnings Date") if isinstance(calendar, dict) else None
            if not raw_dates and isinstance(calendar, pd.DataFrame) and "Earnings Date" in calendar.index:
                raw_dates = calendar.loc["Earnings Date"].tolist()
            if not isinstance(raw_dates, (list, tuple)):
                raw_dates = [raw_dates]
            calendar_days = sorted(day for day in (_iso_day(value) for value in raw_dates) if day and day > now)
            if calendar_days:
                upcoming = {"date": calendar_days[0], "eps_surprise_pct": None}
        except Exception:
            upcoming = None

    fundamentals = get_company_fundamentals(symbol)
    revenue_yoy = _finite_or_none(fundamentals.get("revenue_yoy"))
    if revenue_yoy is None:
        try:
            income = ticker.quarterly_income_stmt
            if isinstance(income, pd.DataFrame) and not income.empty:
                revenue_row = next((name for name in ("Total Revenue", "Operating Revenue") if name in income.index), None)
                if revenue_row and income.shape[1] >= 5:
                    values = income.loc[revenue_row]
                    latest_revenue = _finite(values.iloc[0])
                    prior_year = _finite(values.iloc[4])
                    if prior_year:
                        revenue_yoy = latest_revenue / prior_year - 1.0
        except Exception:
            revenue_yoy = None
    market_cap = _finite_or_none(fundamentals.get("market_cap"))
    trailing_pe = forward_pe = None
    try:
        fast_info = ticker.fast_info
        market_cap = _finite_or_none(fast_info.get("market_cap"))
    except Exception:
        market_cap = None
    try:
        info = ticker.info or {}
        market_cap = market_cap or _finite_or_none(info.get("marketCap"))
        trailing_pe = _finite_or_none(info.get("trailingPE"))
        forward_pe = _finite_or_none(info.get("forwardPE"))
    except Exception:
        pass

    result = {
        "symbol": symbol,
        "latest_report_date": latest["date"].isoformat() if latest else None,
        "next_report_date": upcoming["date"].isoformat() if upcoming else None,
        "eps_surprise_pct": latest["eps_surprise_pct"] if latest else None,
        "revenue_yoy": revenue_yoy,
        "market_cap": market_cap,
        "trailing_pe": trailing_pe,
        "forward_pe": forward_pe,
        "fundamentals_source": fundamentals.get("source") if fundamentals else "yfinance",
    }

    # Cache the result
    _earnings_cache[cache_key] = {**result, "_cache_date": now.isoformat()}
    _save_earnings_cache()

    return result


def _load_calendar_overrides(path: Path | None = None) -> dict[str, dict[str, Any]]:
    selected = path or _DEFAULT_OVERRIDES_PATH
    try:
        payload = json.loads(selected.read_text(encoding="utf-8"))
        return {
            str(symbol).upper(): dict(record)
            for symbol, record in dict(payload.get("symbols") or {}).items()
            if isinstance(record, dict)
        }
    except (FileNotFoundError, OSError, TypeError, json.JSONDecodeError):
        return {}


def _apply_calendar_override(record: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    if not override:
        return record
    result = dict(record)
    for field in ("latest_report_date", "next_report_date", "eps_surprise_pct", "revenue_yoy"):
        if override.get(field) is not None:
            result[field] = override[field]
    result["calendar_override"] = True
    result["calendar_override_note"] = str(override.get("note") or "人工复核后的财报日历修正")
    return result


def load_peer_earnings_history(path: Path, limit: int = 30) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return []
    rows = []
    for line in lines[-max(1, limit):]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def summarize_peer_earnings_history(path: Path, limit: int = 200) -> dict[str, Any]:
    """Summarize archived peer-relay snapshots into a small calibration audit.

    This is intentionally a *snapshot audit*, not a full point-in-time earnings
    database.  It only measures signals that reappear in later archived scans
    with a newer target price.  Unresolved rows remain visible so callers do not
    mistake the summary for a complete historical backtest.
    """
    snapshots = load_peer_earnings_history(path, limit=limit)
    observations: dict[str, list[dict[str, Any]]] = {}
    for snapshot in snapshots:
        scan_time = str(snapshot.get("scan_time") or "")
        for signal in snapshot.get("signals") or []:
            if not isinstance(signal, dict):
                continue
            symbol = str(signal.get("symbol") or "").upper()
            leader = str(signal.get("leader_symbol") or "").upper()
            leader_day = str(signal.get("leader_report_date") or "")
            price = _finite(signal.get("target_latest_price"))
            if not symbol or not leader or not leader_day or price <= 0:
                continue
            key = "|".join([symbol, leader, leader_day])
            observations.setdefault(key, []).append({
                "scan_time": scan_time,
                "symbol": symbol,
                "leader_symbol": leader,
                "leader_report_date": leader_day,
                "research_probability": _finite(signal.get("research_probability")),
                "relay_score": _finite(signal.get("relay_score")),
                "start_price": price,
                "latest_price": price,
                "expected_target_price": _finite(signal.get("expected_target_price")),
                "peer_similarity": _finite(signal.get("peer_similarity")),
                "mapping_version": signal.get("peer_match_version") or PEER_MATCH_VERSION,
            })

    resolved: list[dict[str, Any]] = []
    unresolved = 0
    for rows in observations.values():
        rows.sort(key=lambda item: item["scan_time"])
        first = rows[0]
        later = next((row for row in reversed(rows[1:]) if row["latest_price"] > 0), None)
        if later is None:
            unresolved += 1
            continue
        realized_return = later["latest_price"] / first["start_price"] - 1.0
        expected_target = first.get("expected_target_price") or 0.0
        target_hit = bool(expected_target > 0 and later["latest_price"] >= expected_target)
        resolved.append({
            "symbol": first["symbol"],
            "leader_symbol": first["leader_symbol"],
            "leader_report_date": first["leader_report_date"],
            "first_scan_time": first["scan_time"],
            "last_scan_time": later["scan_time"],
            "research_probability": round(first["research_probability"], 4),
            "relay_score": round(first["relay_score"], 4),
            "peer_similarity": round(first["peer_similarity"], 4),
            "start_price": round(first["start_price"], 4),
            "latest_price": round(later["latest_price"], 4),
            "realized_return": round(realized_return, 6),
            "win": realized_return > 0,
            "target_hit": target_hit,
        })

    bucket_defs = [(0.0, 0.55), (0.55, 0.65), (0.65, 0.75), (0.75, 1.01)]
    buckets: list[dict[str, Any]] = []
    for low, high in bucket_defs:
        members = [
            item for item in resolved
            if low <= float(item.get("research_probability") or 0) < high
        ]
        wins = sum(1 for item in members if item["win"])
        hits = sum(1 for item in members if item["target_hit"])
        buckets.append({
            "probability_low": round(low, 2),
            "probability_high": round(min(high, 1.0), 2),
            "n": len(members),
            "win_rate": round(wins / len(members), 4) if members else None,
            "target_hit_rate": round(hits / len(members), 4) if members else None,
            "mean_realized_return": round(sum(float(item["realized_return"]) for item in members) / len(members), 6) if members else None,
        })

    wins = sum(1 for item in resolved if item["win"])
    return {
        "available": bool(snapshots),
        "snapshot_count": len(snapshots),
        "signal_keys": len(observations),
        "resolved_count": len(resolved),
        "unresolved_count": unresolved,
        "win_rate": round(wins / len(resolved), 4) if resolved else None,
        "mean_realized_return": round(sum(float(item["realized_return"]) for item in resolved) / len(resolved), 6) if resolved else None,
        "buckets": buckets,
        "examples": sorted(resolved, key=lambda item: float(item["realized_return"]), reverse=True)[:10],
        "method_note": "基于已归档同行接力快照的后续价格观察；不是完整 point-in-time 财报 surprise 回测。resolved_count 为有后续快照可观察的样本数。",
    }


def _bar_on_or_after(frame: pd.DataFrame, day: date) -> int | None:
    for index, value in enumerate(frame.index):
        if _iso_day(value) and _iso_day(value) >= day:
            return index
    return None


def _event_tape(frame: pd.DataFrame, report_day: date) -> dict[str, Any] | None:
    if frame.empty or "Close" not in frame or "Volume" not in frame or len(frame) < 22:
        return None
    close = frame["Close"].astype(float)
    volume = frame["Volume"].astype(float).fillna(0)
    start_index = _bar_on_or_after(frame, report_day)
    if start_index is None:
        return None
    candidates = range(start_index, min(len(frame), start_index + 3))
    selected = start_index
    best_trigger = -999.0
    for index in candidates:
        prior_volume = max(_finite(volume.iloc[max(0, index - 20):index].mean()), 1.0)
        ratio = _finite(volume.iloc[index] / prior_volume, 1.0)
        daily_return = _finite(close.pct_change().iloc[index])
        trigger = max(0.0, ratio - 1.0) + max(0.0, daily_return * 8.0)
        if trigger > best_trigger:
            selected, best_trigger = index, trigger
    base = _finite(close.iloc[selected])
    latest = _finite(close.iloc[-1])
    pre_report_index = max(0, start_index - 1)
    pre_report_price = _finite(close.iloc[pre_report_index])
    prior_volume = max(_finite(volume.iloc[max(0, selected - 20):selected].mean()), 1.0)
    dollar_volume = close * volume
    prior_dollar = max(_finite(dollar_volume.iloc[max(0, selected - 20):selected].mean()), 1.0)
    return {
        "start_date": _iso_day(frame.index[selected]).isoformat(),
        "start_price": base,
        "latest_price": latest,
        "return_since_start": latest / base - 1.0 if base else 0.0,
        "pre_report_price": pre_report_price,
        "post_report_return": latest / pre_report_price - 1.0 if pre_report_price else 0.0,
        "event_volume_ratio": _finite(volume.iloc[selected] / prior_volume, 1.0),
        "dollar_volume_ratio": _finite(dollar_volume.iloc[selected] / prior_dollar, 1.0),
    }


def _target_tape(frame: pd.DataFrame, start_day: date) -> dict[str, Any] | None:
    if frame.empty or "Close" not in frame or len(frame) < 2:
        return None
    close = frame["Close"].astype(float)
    volume = frame.get("Volume", pd.Series(index=frame.index, dtype=float)).astype(float).fillna(0)
    start_index = _bar_on_or_after(frame, start_day)
    if start_index is None:
        return None
    base = _finite(close.iloc[start_index])
    latest = _finite(close.iloc[-1])
    prior_volume = max(_finite(volume.tail(20).mean()), 1.0)
    return {
        "start_price": base,
        "latest_price": latest,
        "return_since_start": latest / base - 1.0 if base else 0.0,
        "latest_volume_ratio": _finite(volume.iloc[-1] / prior_volume, 1.0),
    }


def _probability(score: float) -> float:
    return 1.0 / (1.0 + math.exp(-5.0 * (score - 0.52)))


def _build_signals(
    groups: Iterable[dict[str, Any]],
    earnings: dict[str, dict[str, Any]],
    histories: dict[str, pd.DataFrame],
    now: date,
    max_lead_days: int,
    target_symbols: set[str] | None = None,
) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    target_symbols = {str(symbol).upper() for symbol in (target_symbols or set())}
    for group in groups:
        leaders: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for symbol in group["symbols"]:
            record = earnings.get(symbol, {})
            report_day = _iso_day(record.get("latest_report_date"))
            if not report_day or not (now - timedelta(days=35) <= report_day <= now):
                continue
            tape = _event_tape(histories.get(symbol, pd.DataFrame()), report_day)
            surprise = _finite(record.get("eps_surprise_pct"))
            revenue_yoy = _finite(record.get("revenue_yoy"))
            if tape and (surprise >= 0.05 or revenue_yoy >= 0.15) and (
                tape["return_since_start"] >= 0.025 or tape["event_volume_ratio"] >= 1.35
            ):
                leaders.append((record, tape))
        for leader, leader_tape in leaders:
            leader_day = _iso_day(leader.get("latest_report_date"))
            start_day = _iso_day(leader_tape.get("start_date"))
            if not leader_day or not start_day:
                continue
            for symbol in group["symbols"]:
                if symbol == leader["symbol"]:
                    continue
                if target_symbols and symbol not in target_symbols:
                    continue
                target = earnings.get(symbol, {})
                next_day = _iso_day(target.get("next_report_date"))
                target_latest_day = _iso_day(target.get("latest_report_date"))
                manual_date_check = next_day is None
                if next_day and not (now < next_day <= now + timedelta(days=max_lead_days)):
                    continue
                if manual_date_check and target_latest_day and target_latest_day >= leader_day:
                    continue
                target_tape = _target_tape(histories.get(symbol, pd.DataFrame()), start_day)
                if not target_tape:
                    continue
                pair = _pair_similarity(group, leader["symbol"], symbol, leader, target, histories)
                cap_ratio = pair.get("leader_to_target_market_cap_ratio")
                small_leader_reverse_read = cap_ratio is not None and cap_ratio < 0.34
                if pair["business"] < 0.55 or pair["combined"] < 0.62 or small_leader_reverse_read:
                    continue
                gap = leader_tape["return_since_start"] - target_tape["return_since_start"]
                days_to_report = (next_day - now).days if next_day else None
                beat_strength = max(_finite(leader.get("eps_surprise_pct")), max(0.0, _finite(leader.get("revenue_yoy"))))
                score = (
                    0.28 * pair["combined"]
                    + 0.18 * min(1.0, max(0.0, beat_strength / 0.30))
                    + 0.22 * min(1.0, max(0.0, leader_tape["return_since_start"] / 0.16))
                    + 0.22 * min(1.0, max(0.0, gap / 0.14))
                    + 0.10 * min(1.0, max(0.0, (target_tape["latest_volume_ratio"] - 0.8) / 1.2))
                )
                if manual_date_check:
                    score *= 0.92
                expected_catch_up_return = max(
                    0.0,
                    gap * pair["combined"] * (0.45 + 0.55 * _probability(score)),
                )
                signals.append({
                    "peer_match_version": PEER_MATCH_VERSION,
                    "symbol": symbol,
                    "group_id": group["id"],
                    "group_label": group["label"],
                    "peer_similarity": pair["combined"],
                    "peer_similarity_breakdown": pair,
                    "transmission_direction": pair["transmission_direction"],
                    "sublane_relation": pair["sublane_relation"],
                    "sublane_relation_display": pair.get("sublane_relation_display"),
                    "leader_sublane": pair["leader_sublane"],
                    "target_sublane": pair["target_sublane"],
                    "leader_sublane_label": pair.get("leader_sublane_label"),
                    "target_sublane_label": pair.get("target_sublane_label"),
                    "leader_to_target_market_cap_ratio": pair["leader_to_target_market_cap_ratio"],
                    "leader_symbol": leader["symbol"],
                    "leader_report_date": leader_day.isoformat(),
                    "target_report_date": next_day.isoformat() if next_day else None,
                    "days_to_target_report": days_to_report,
                    "target_report_date_requires_manual_check": manual_date_check,
                    "target_report_date_note": "披露日待人工确认" if manual_date_check else "已获取下一次披露日",
                    "leader_eps_surprise_pct": round(_finite(leader.get("eps_surprise_pct")), 4),
                    "leader_revenue_yoy": round(_finite(leader.get("revenue_yoy")), 4) if leader.get("revenue_yoy") is not None else None,
                    "leader_start_date": start_day.isoformat(),
                    "leader_market_cap": leader.get("market_cap"),
                    "leader_trailing_pe": leader.get("trailing_pe"),
                    "leader_forward_pe": leader.get("forward_pe"),
                    "leader_pre_report_price": round(leader_tape["pre_report_price"], 2),
                    "leader_latest_price": round(leader_tape["latest_price"], 2),
                    "leader_post_report_return": round(leader_tape["post_report_return"], 4),
                    "leader_return": round(leader_tape["return_since_start"], 4),
                    "leader_event_volume_ratio": round(leader_tape["event_volume_ratio"], 3),
                    "leader_dollar_volume_ratio": round(leader_tape["dollar_volume_ratio"], 3),
                    "target_return": round(target_tape["return_since_start"], 4),
                    "target_start_price": round(target_tape["start_price"], 2),
                    "target_latest_price": round(target_tape["latest_price"], 2),
                    "target_market_cap": target.get("market_cap"),
                    "target_trailing_pe": target.get("trailing_pe"),
                    "target_forward_pe": target.get("forward_pe"),
                    "target_latest_volume_ratio": round(target_tape["latest_volume_ratio"], 3),
                    "relative_lag_gap": round(gap, 4),
                    "expected_catch_up_return": round(expected_catch_up_return, 4),
                    "expected_target_price": round(target_tape["latest_price"] * (1.0 + expected_catch_up_return), 2),
                    "relay_score": round(min(1.0, max(0.0, score)), 4),
                    "research_probability": round(_probability(score), 4),
                    "daily_tunnel": compute_daily_tunnel_score(histories.get(symbol, pd.DataFrame())),
                    "flow_proxy_note": "yfinance 不提供真实净买入；使用成交额与成交量放大作为资金关注代理。",
                })
    best: dict[str, dict[str, Any]] = {}
    for signal in signals:
        if signal["relative_lag_gap"] <= 0:
            continue
        current = best.get(signal["symbol"])
        if current is None or signal["relay_score"] > current["relay_score"]:
            best[signal["symbol"]] = signal
    return sorted(best.values(), key=lambda item: item["relay_score"], reverse=True)


def scan_peer_earnings_signals(
    group_id: str = "all",
    max_lead_days: int = 45,
    progress: Callable[[str, float], None] | None = None,
    cache_path: Path | None = None,
    cache_ttl_seconds: int = 6 * 3600,
    overrides_path: Path | None = None,
    target_symbols: Iterable[str] | None = None,
    include_dynamic_target_groups: bool = False,
    restrict_targets: bool = False,
    cache_only: bool = False,
) -> dict[str, Any]:
    """Fetch calendars and tape data, then rank unreported peers."""
    emit = progress or (lambda _phase, _pct: None)
    started = time.time()
    target_set = {str(symbol).strip().upper() for symbol in (target_symbols or []) if str(symbol).strip()}
    target_digest = ""
    if target_set:
        target_digest = hashlib.sha1(",".join(sorted(target_set)).encode("utf-8")).hexdigest()[:12]
    cache_key = f"{group_id}:{max_lead_days}:{target_digest}:{int(include_dynamic_target_groups)}:{int(restrict_targets)}"
    with _cache_lock:
        cached = _memory_cache.get(cache_key)
        if cached and time.time() - cached[0] < cache_ttl_seconds:
            return {**cached[1], "cache_hit": True}
    if cache_path and cache_path.exists() and time.time() - cache_path.stat().st_mtime < cache_ttl_seconds:
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            if (
                payload.get("cache_version") == _CACHE_VERSION
                and payload.get("group_id") == group_id
                and payload.get("max_lead_days") == max_lead_days
                and payload.get("target_digest", "") == target_digest
            ):
                with _cache_lock:
                    _memory_cache[cache_key] = (time.time(), payload)
                return {**payload, "cache_hit": True}
        except (OSError, json.JSONDecodeError):
            pass

    if cache_only:
        return {"signals": [], "cache_hit": False, "evidence_status": "unknown",
                "reason": "同行财报缓存不可用；等待后台复核，不代表无事件风险"}

    groups = list(PEER_GROUPS if group_id == "all" else [group for group in PEER_GROUPS if group["id"] == group_id])
    if not groups:
        raise ValueError(f"unsupported peer group: {group_id}")
    if target_set and include_dynamic_target_groups:
        dynamic_groups = _dynamic_peer_groups_for_symbols(target_set)
        if dynamic_groups:
            groups.extend(dynamic_groups)
    if target_set:
        groups = [
            group for group in groups
            if set(str(symbol).upper() for symbol in group.get("symbols", [])) & target_set
            or not restrict_targets
        ]
    peer_mapping = _peer_mapping_for_symbols(target_set, groups) if target_set else {}
    if restrict_targets and target_set:
        symbols = sorted({symbol for group in groups for symbol in group["symbols"] if symbol in target_set})
    else:
        symbols = sorted({symbol for group in groups for symbol in group["symbols"]})
    emit(f"读取 {len(symbols)} 家同行公司的财报日历", 0.12)
    today = datetime.now(timezone.utc).date()
    earnings: dict[str, dict[str, Any]] = {}
    overrides = _load_calendar_overrides(overrides_path)
    with ThreadPoolExecutor(max_workers=min(12, max(1, len(symbols)))) as executor:
        futures = {executor.submit(_earnings_record, symbol, today): symbol for symbol in symbols}
        for index, future in enumerate(as_completed(futures), start=1):
            symbol = futures[future]
            try:
                earnings[symbol] = _apply_calendar_override(future.result(), overrides.get(symbol))
            except Exception:
                earnings[symbol] = _apply_calendar_override(
                    {"symbol": symbol, "latest_report_date": None, "next_report_date": None},
                    overrides.get(symbol),
                )
            if index % 10 == 0 or index == len(symbols):
                emit(f"读取财报节点 {index}/{len(symbols)}", 0.12 + 0.38 * index / max(1, len(symbols)))

    emit("批量拉取同行量价数据", 0.55)
    download, market_data_sources = download_daily_history(symbols, period="4mo")
    histories = {symbol: _ticker_frame(download, symbol) for symbol in symbols}
    emit("计算先发财报验证与后发同行滞涨差", 0.82)
    signals = _build_signals(
        groups,
        earnings,
        histories,
        today,
        max_lead_days,
        target_symbols=target_set if restrict_targets else None,
    )
    payload = {
        "cache_version": _CACHE_VERSION,
        "scan_time": datetime.utcnow().isoformat() + "Z",
        "group_id": group_id,
        "max_lead_days": max_lead_days,
        "group_count": len(groups),
        "ticker_count": len(symbols),
        "target_digest": target_digest,
        "target_symbol_count": len(target_set),
        "target_mapping": peer_mapping,
        "target_mapping_coverage": {
            "target_count": len(target_set),
            "mapped_count": len(peer_mapping),
            "coverage": round(len(peer_mapping) / len(target_set), 4) if target_set else None,
            "curated_count": sum(1 for item in peer_mapping.values() if item.get("mapping_source") == "curated_peer_catalog"),
            "dynamic_count": sum(1 for item in peer_mapping.values() if item.get("mapping_source") == "dynamic_industry"),
        },
        "calendar_coverage": sum(
            1 for item in earnings.values()
            if item.get("latest_report_date") or item.get("next_report_date")
        ),
        "calendar_override_count": sum(1 for item in earnings.values() if item.get("calendar_override")),
        "earnings_calendar": [earnings[symbol] for symbol in symbols],
        "signals": signals,
        "market_data_sources": market_data_sources,
        "elapsed_seconds": round(time.time() - started, 3),
        "cache_hit": False,
        "method_note": "同行财报接力为研究信号：EPS surprise、季度营收同比与量价异动来自 yfinance；全部同行配对统一检验业务子赛道、客群、方向性市值传导和盘口风格。直接同行与白名单内邻近赛道可以进入接力，未列明的跨赛道组合仅作观察；龙头向较小同行传导会获得更高可信度，较小公司反推显著更大公司会被降权或剔除；净买入使用成交额放大代理，并非真实订单流；下一次披露日为空时仍保留候选，但必须人工确认公司尚未披露；概率为启发式研究概率，需积累历史快照后再做校准。",
    }
    with _cache_lock:
        _memory_cache[cache_key] = (time.time(), payload)
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp = cache_path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(cache_path)
        history_path = cache_path.parent / "_peer_earnings_signal_history.jsonl"
        with history_path.open("a", encoding="utf-8") as history_file:
            history_file.write(json.dumps(payload, ensure_ascii=False) + "\n")
    emit("整理同行财报接力候选", 0.96)
    return payload


def build_peer_earnings_signals_for_test(
    groups: Iterable[dict[str, Any]],
    earnings: dict[str, dict[str, Any]],
    histories: dict[str, pd.DataFrame],
    as_of: date,
    max_lead_days: int = 45,
) -> list[dict[str, Any]]:
    """Deterministic seam for unit tests and research replay."""
    return _build_signals(groups, earnings, histories, as_of, max_lead_days)


# ---------------------------------------------------------------------------
# Pre-Earnings Expectation Revision Strategy
# ---------------------------------------------------------------------------
# This module implements a pre-earnings signal that detects expectation
# revisions BEFORE the earnings report.  The strategy aims to capture
# price movement driven by analyst estimate revisions, peer inference,
# relative momentum, and revenue expectation changes in the 6-10 trading
# days before a target company's earnings report.
#
# Naming convention:
#   - "pre_earnings_expectation_revision" (not "arbitrage" since this
#     involves directional risk)
#   - All dates use trading days, not calendar days
#   - Execution is next_open after signal generation
#   - Exit is T-2 close (2 trading days before earnings)
# ---------------------------------------------------------------------------

# Default configuration for the pre-earnings strategy
_PRE_EARNINGS_DEFAULT_CONFIG: dict[str, Any] = {
    "signal_version": "v0.2.0",
    "entry_window_min_trading_days": 6,
    "entry_window_max_trading_days": 10,
    "scheduled_exit_days_before_report": 2,
    "max_holding_days": 8,
    "min_price": 5.0,
    "min_avg_dollar_volume_20d": 20_000_000,
    "min_data_quality_score": 0.8,
    "min_report_date_confidence": 0.7,
    "min_final_score": 0.65,
    "score_weights": {
        "analyst_revision": 0.40,
        "peer_inference": 0.25,
        "relative_momentum": 0.20,
        "revenue_revision": 0.15,
    },
}


def _get_eps_revisions(symbol: str) -> dict[str, Any]:
    """Fetch EPS estimate revisions from yfinance.

    Uses get_eps_trend() and get_eps_revisions() to obtain:
    - EPS trend: current, 7daysAgo, 30daysAgo, 60daysAgo, 90daysAgo
    - EPS revisions: upLast7days, upLast30days, downLast7days, downLast30days

    Returns:
        Dict with eps_trend, eps_revisions, and data_quality warnings.
        Fields are None when data is unavailable (never fabricated).
    """
    result: dict[str, Any] = {
        "eps_current": None,
        "eps_7d_ago": None,
        "eps_30d_ago": None,
        "eps_60d_ago": None,
        "eps_90d_ago": None,
        "eps_change_7d": None,
        "eps_change_30d": None,
        "revision_up_7d": None,
        "revision_up_30d": None,
        "revision_down_7d": None,
        "revision_down_30d": None,
        "data_available": False,
        "warnings": [],
    }

    try:
        ticker = yf.Ticker(symbol)

        # Try get_eps_trend() first
        try:
            eps_trend = ticker.get_eps_trend()
            if isinstance(eps_trend, pd.DataFrame) and not eps_trend.empty:
                # yfinance returns rows like "current", "7daysAgo", "30daysAgo", etc.
                for idx_label in ("current", "7daysAgo", "30daysAgo", "60daysAgo", "90daysAgo"):
                    if idx_label in eps_trend.index:
                        row = eps_trend.loc[idx_label]
                        eps_val = _finite(row.get("epsTrend", row.get("0y", None)))
                        if idx_label == "current":
                            result["eps_current"] = eps_val if eps_val != 0.0 else None
                        elif idx_label == "7daysAgo":
                            result["eps_7d_ago"] = eps_val if eps_val != 0.0 else None
                        elif idx_label == "30daysAgo":
                            result["eps_30d_ago"] = eps_val if eps_val != 0.0 else None
                        elif idx_label == "60daysAgo":
                            result["eps_60d_ago"] = eps_val if eps_val != 0.0 else None
                        elif idx_label == "90daysAgo":
                            result["eps_90d_ago"] = eps_val if eps_val != 0.0 else None
        except Exception:
            result["warnings"].append("get_eps_trend() failed or returned unexpected format")

        # Try get_eps_revisions() for revision counts
        try:
            eps_revisions = ticker.get_eps_revisions()
            if isinstance(eps_revisions, pd.DataFrame) and not eps_revisions.empty:
                for idx_label in ("current", "7daysAgo", "30daysAgo", "90daysAgo"):
                    if idx_label in eps_revisions.index:
                        row = eps_revisions.loc[idx_label]
                        up_val = _finite_or_none(row.get("up", row.get("Up", None)))
                        down_val = _finite_or_none(row.get("down", row.get("Down", None)))
                        if idx_label == "7daysAgo":
                            result["revision_up_7d"] = int(up_val) if up_val is not None else None
                            result["revision_down_7d"] = int(down_val) if down_val is not None else None
                        elif idx_label == "30daysAgo":
                            result["revision_up_30d"] = int(up_val) if up_val is not None else None
                            result["revision_down_30d"] = int(down_val) if down_val is not None else None
        except Exception:
            result["warnings"].append("get_eps_revisions() failed or returned unexpected format")

        # Calculate EPS changes if we have both current and historical values
        if result["eps_current"] is not None and result["eps_7d_ago"] is not None and result["eps_7d_ago"] != 0:
            result["eps_change_7d"] = round(result["eps_current"] / result["eps_7d_ago"] - 1.0, 6)
        if result["eps_current"] is not None and result["eps_30d_ago"] is not None and result["eps_30d_ago"] != 0:
            result["eps_change_30d"] = round(result["eps_current"] / result["eps_30d_ago"] - 1.0, 6)

        # Check if we have minimum viable data
        has_trend = result["eps_current"] is not None
        has_revisions = (result["revision_up_30d"] is not None or result["revision_down_30d"] is not None)
        result["data_available"] = has_trend or has_revisions

        if not result["data_available"]:
            result["warnings"].append("EPS revision data unavailable: both eps_trend and eps_revisions are empty")

    except Exception as e:
        result["warnings"].append(f"yfinance API error for {symbol}: {str(e)[:100]}")

    return result


def _get_revenue_estimate(symbol: str) -> dict[str, Any]:
    """Fetch revenue estimate data from yfinance.

    Uses get_revenue_estimate() to obtain current and historical revenue
    expectations.  If historical snapshots are not available, marks fields
    as None rather than fabricating values.

    Returns:
        Dict with revenue_current, revenue_30d_ago, revenue_change_30d,
        and data_quality warnings.
    """
    result: dict[str, Any] = {
        "revenue_current": None,
        "revenue_30d_ago": None,
        "revenue_change_30d": None,
        "data_available": False,
        "warnings": [],
    }

    try:
        ticker = yf.Ticker(symbol)
        revenue_est = ticker.get_revenue_estimate()

        if isinstance(revenue_est, pd.DataFrame) and not revenue_est.empty:
            # Try to get current revenue estimate
            if "avg" in revenue_est.columns:
                latest_row = revenue_est.iloc[0] if len(revenue_est) > 0 else None
                if latest_row is not None:
                    result["revenue_current"] = _finite_or_none(latest_row.get("avg"))

            # Try to get 30-day-ago estimate if available
            # Note: yfinance may not provide historical snapshots
            # If not available, we mark as None (not fabricated)
            if len(revenue_est) > 1:
                # Some yfinance versions return multiple periods, not historical snapshots
                # We only use the first row as current estimate
                pass

            result["data_available"] = result["revenue_current"] is not None

            if not result["data_available"]:
                result["warnings"].append("revenue estimate exists but avg field is null")
        else:
            result["warnings"].append("get_revenue_estimate() returned empty or invalid DataFrame")

    except Exception as e:
        result["warnings"].append(f"revenue estimate fetch failed for {symbol}: {str(e)[:100]}")

    # Revenue change requires historical snapshot which yfinance doesn't reliably provide
    # Mark as unavailable rather than fabricating
    if result["revenue_change_30d"] is None:
        result["warnings"].append("revenue_change_30d unavailable: historical snapshot not implemented")

    return result


def _analyst_revision_score(eps_data: dict[str, Any]) -> dict[str, Any]:
    """Calculate analyst EPS revision score.

    Score logic:
    - Based on net revision ratio: (up - down) / (up + down)
    - Uses 30-day window as primary, 7-day as secondary
    - EPS trend change also contributes to score

    Returns:
        Dict with score (0-1), detail string, and component breakdown.
    """
    result: dict[str, Any] = {
        "score": None,
        "detail": "数据不足",
        "components": {},
        "warnings": [],
    }

    if not eps_data.get("data_available"):
        result["warnings"].append("EPS revision data not available")
        return result

    score_components = []

    # Component 1: Revision breadth (30-day)
    up_30d = eps_data.get("revision_up_30d")
    down_30d = eps_data.get("revision_down_30d")
    if up_30d is not None and down_30d is not None:
        total = up_30d + down_30d
        if total > 0:
            breadth_30d = (up_30d - down_30d) / total
            # Map from [-1, 1] to [0, 1]
            breadth_score = 0.5 + 0.5 * breadth_30d
            score_components.append(breadth_score)
            result["components"]["revision_breadth_30d"] = round(breadth_score, 4)
            result["components"]["up_30d"] = up_30d
            result["components"]["down_30d"] = down_30d

    # Component 2: Revision breadth (7-day) - more recent, slightly weighted
    up_7d = eps_data.get("revision_up_7d")
    down_7d = eps_data.get("revision_down_7d")
    if up_7d is not None and down_7d is not None:
        total_7d = up_7d + down_7d
        if total_7d > 0:
            breadth_7d = (up_7d - down_7d) / total_7d
            breadth_7d_score = 0.5 + 0.5 * breadth_7d
            score_components.append(breadth_7d_score * 1.1)  # Slightly weight recent revisions more
            result["components"]["revision_breadth_7d"] = round(breadth_7d_score, 4)

    # Component 3: EPS trend change (30-day)
    eps_change_30d = eps_data.get("eps_change_30d")
    if eps_change_30d is not None:
        # Positive change = upward revision = good
        # Map to [0, 1] with sigmoid-like scaling
        trend_score = 1.0 / (1.0 + math.exp(-20.0 * eps_change_30d))
        score_components.append(trend_score)
        result["components"]["eps_trend_change"] = round(trend_score, 4)

    if not score_components:
        result["warnings"].append("no usable revision components")
        return result

    # Average of available components
    result["score"] = round(sum(score_components) / len(score_components), 4)

    # Build detail string
    parts = []
    if up_30d is not None and down_30d is not None:
        parts.append(f"30日修正: {up_30d}↑/{down_30d}↓")
    if up_7d is not None and down_7d is not None:
        parts.append(f"7日修正: {up_7d}↑/{down_7d}↓")
    if eps_change_30d is not None:
        parts.append(f"EPS变化: {eps_change_30d:.1%}")
    result["detail"] = "; ".join(parts) if parts else "数据不足"

    return result


def _revenue_revision_score(revenue_data: dict[str, Any]) -> dict[str, Any]:
    """Calculate revenue expectation revision score.

    Since yfinance doesn't reliably provide historical revenue snapshots,
    this score is based on whether current revenue estimate is available
    and any available change data.

    Returns:
        Dict with score (0-1 or None), detail string, and warnings.
    """
    result: dict[str, Any] = {
        "score": None,
        "detail": "数据不足",
        "components": {},
        "warnings": [],
    }

    if not revenue_data.get("data_available"):
        result["warnings"].append("revenue estimate data not available")
        return result

    # If we have revenue change data, use it
    revenue_change = revenue_data.get("revenue_change_30d")
    if revenue_change is not None:
        # Positive change = upward revision = good
        score = 1.0 / (1.0 + math.exp(-15.0 * revenue_change))
        result["score"] = round(score, 4)
        result["detail"] = f"营收预期变化: {revenue_change:.1%}"
        result["components"]["revenue_change_30d"] = round(score, 4)
    else:
        # No historical snapshot available
        # We cannot calculate a meaningful score
        result["warnings"].append("revenue_change_30d unavailable: cannot calculate revision score")
        result["detail"] = "营收预期历史快照不可用，无法计算修正评分"

    return result


def _peer_inference_score(
    symbol: str,
    group: dict[str, Any],
    earnings: dict[str, dict[str, Any]],
    histories: dict[str, pd.DataFrame],
    now: date,
) -> dict[str, Any]:
    """Calculate peer earnings inference score.

    Logic:
    - Find peers in the same group that have already reported earnings
    - Weight by: peer_relevance * earnings_surprise_strength * report_recency_decay
    - Reduce score if target has already rallied significantly

    Returns:
        Dict with score (0-1), peer_events list, and detail string.
    """
    result: dict[str, Any] = {
        "score": None,
        "peer_events": [],
        "detail": "无同行财报数据",
        "components": {},
        "warnings": [],
    }

    group_symbols = group.get("symbols", [])
    if symbol not in group_symbols:
        result["warnings"].append(f"{symbol} not in group {group.get('id')}")
        return result

    # Find peers that have reported in the last 35 days
    peer_events = []
    for peer_symbol in group_symbols:
        if peer_symbol == symbol:
            continue

        peer_earnings = earnings.get(peer_symbol, {})
        report_date_str = peer_earnings.get("latest_report_date")
        if not report_date_str:
            continue

        report_date = _iso_day(report_date_str)
        if not report_date:
            continue

        # Must be in the past and within 35 days
        days_since = (now - report_date).days
        if days_since < 0 or days_since > 35:
            continue

        # Check if peer beat expectations
        eps_surprise = _finite(peer_earnings.get("eps_surprise_pct"))
        revenue_yoy = _finite(peer_earnings.get("revenue_yoy"))

        is_positive = eps_surprise >= 0.05 or revenue_yoy >= 0.15
        if not is_positive:
            continue

        # Calculate recency decay (more recent = higher weight)
        recency_decay = max(0.3, 1.0 - days_since / 35.0)

        # Calculate surprise strength
        surprise_strength = min(1.0, max(eps_surprise, revenue_yoy) / 0.30)

        # Peer relevance (default 1.0 if not configured)
        peer_relevance = 1.0  # TODO: support weighted peer graph

        event_score = peer_relevance * surprise_strength * recency_decay
        peer_events.append({
            "symbol": peer_symbol,
            "report_date": report_date_str,
            "eps_surprise": round(eps_surprise, 4),
            "revenue_yoy": round(revenue_yoy, 4) if revenue_yoy else None,
            "days_since_report": days_since,
            "recency_decay": round(recency_decay, 4),
            "event_score": round(event_score, 4),
            "type": "positive_surprise" if is_positive else "inline",
        })

    if not peer_events:
        result["warnings"].append("no positive peer earnings events found")
        return result

    # Check if target has already rallied (reduce inference value)
    target_history = histories.get(symbol, pd.DataFrame())
    target_already_rallied = False
    if not target_history.empty and "Close" in target_history and len(target_history) >= 10:
        close = target_history["Close"].astype(float)
        recent_return = close.iloc[-1] / close.iloc[-10] - 1.0 if close.iloc[-10] > 0 else 0
        if recent_return > 0.15:  # Already up 15%+ in 10 days
            target_already_rallied = True

    # Aggregate peer scores
    max_event_score = max(e["event_score"] for e in peer_events)
    avg_event_score = sum(e["event_score"] for e in peer_events) / len(peer_events)

    # Use max score as primary, with diminishing returns for multiple events
    base_score = max_event_score * (1.0 + 0.1 * min(len(peer_events) - 1, 3))
    base_score = min(1.0, base_score)

    # Reduce if target already rallied
    if target_already_rallied:
        base_score *= 0.6
        result["warnings"].append("target has already rallied 15%+ in 10 days, reducing inference value")

    result["score"] = round(base_score, 4)
    result["peer_events"] = peer_events
    result["components"]["max_event_score"] = round(max_event_score, 4)
    result["components"]["avg_event_score"] = round(avg_event_score, 4)
    result["components"]["event_count"] = len(peer_events)
    result["components"]["target_already_rallied"] = target_already_rallied

    # Build detail string
    event_summaries = [f"{e['symbol']}: {'positive' if e['type'] == 'positive_surprise' else 'inline'}" for e in peer_events[:3]]
    result["detail"] = f"{len(peer_events)}个同行超预期: {', '.join(event_summaries)}"

    return result


def _relative_momentum_score(momentum_data: dict[str, Any]) -> dict[str, Any]:
    """Calculate relative momentum score.

    Based on stock_return_10d - benchmark_return_10d.
    Positive relative return = stock outperforming = bullish.

    Returns:
        Dict with score (0-1), detail string, and components.
    """
    result: dict[str, Any] = {
        "score": None,
        "detail": "数据不足",
        "components": {},
        "warnings": [],
    }

    relative_return = momentum_data.get("relative_return")
    stock_return = momentum_data.get("stock_return")
    benchmark_return = momentum_data.get("benchmark_return")

    if relative_return is None:
        result["warnings"].append("relative momentum data not available")
        return result

    # Map relative return to score using sigmoid
    # relative_return of 0.05 (5% outperformance) -> ~0.73
    # relative_return of 0.10 (10% outperformance) -> ~0.88
    # relative_return of -0.05 (5% underperformance) -> ~0.27
    score = 1.0 / (1.0 + math.exp(-8.0 * relative_return))

    result["score"] = round(score, 4)
    result["components"]["relative_return"] = round(relative_return, 6)
    result["components"]["stock_return"] = round(stock_return, 6) if stock_return is not None else None
    result["components"]["benchmark_return"] = round(benchmark_return, 6) if benchmark_return is not None else None
    result["detail"] = f"相对动量: {relative_return:.1%} (股票: {stock_return:.1%}, 基准: {benchmark_return:.1%})" if stock_return is not None and benchmark_return is not None else f"相对动量: {relative_return:.1%}"

    return result


def _calculate_data_quality(
    eps_data: dict[str, Any],
    revenue_data: dict[str, Any],
    peer_data: dict[str, Any],
    momentum_data: dict[str, Any],
    report_date_confidence: float,
) -> dict[str, Any]:
    """Calculate overall data quality score.

    Score = available_required_fields / total_required_fields

    Required fields:
    1. EPS revision data available
    2. Revenue estimate data available
    3. Peer event data available (or no peers in group)
    4. Price history data complete
    5. Benchmark data available
    6. Report date confidence >= threshold

    Returns:
        Dict with score (0-1), missing_fields list, and warnings.
    """
    required_fields = {
        "eps_revision": eps_data.get("data_available", False),
        "revenue_estimate": revenue_data.get("data_available", False),
        "price_history": momentum_data.get("stock_return") is not None,
        "benchmark_data": momentum_data.get("benchmark_return") is not None,
        "report_date": report_date_confidence >= 0.7,
    }

    # Peer data is optional if no peers exist
    if peer_data.get("peer_events"):
        required_fields["peer_events"] = True
    # If no peer events but warnings say "no positive peer earnings events found",
    # that's acceptable (group exists but no peers reported recently)
    elif "no positive peer earnings events found" in peer_data.get("warnings", []):
        required_fields["peer_events"] = True  # Data is "available" (just empty)

    available = sum(1 for v in required_fields.values() if v)
    total = len(required_fields)
    score = available / total if total > 0 else 0.0

    missing = [k for k, v in required_fields.items() if not v]
    warnings = []
    warnings.extend(eps_data.get("warnings", []))
    warnings.extend(revenue_data.get("warnings", []))
    warnings.extend(peer_data.get("warnings", []))
    warnings.extend(momentum_data.get("warnings", []))

    return {
        "score": round(score, 4),
        "missing_fields": missing,
        "warnings": warnings,
    }


def _check_eligibility(
    symbol: str,
    report_date: date,
    now: date,
    history: pd.DataFrame,
    data_quality_score: float,
    report_date_confidence: float,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Check hard eligibility criteria for pre-earnings signal.

    Criteria (all must be met):
    1. trading_days_to_report in [min, max]
    2. close > MA20
    3. avg_dollar_volume_20d >= threshold
    4. price >= min_price
    5. data_quality_score >= threshold
    6. report_date_confidence >= threshold

    Returns:
        Dict with eligible (bool), reasons list, and metrics.
    """
    cfg = {**_PRE_EARNINGS_DEFAULT_CONFIG, **(config or {})}
    reasons = []
    metrics: dict[str, Any] = {}

    # Calculate trading days to report
    # Simple approximation: count business days between now and report_date
    # A proper implementation would use a trading calendar
    trading_days = 0
    current = now
    while current < report_date:
        current += timedelta(days=1)
        if current.weekday() < 5:  # Monday=0, Friday=4
            trading_days += 1
    metrics["trading_days_to_report"] = trading_days

    # Check trading day window
    min_days = cfg.get("entry_window_min_trading_days", 6)
    max_days = cfg.get("entry_window_max_trading_days", 10)
    if not (min_days <= trading_days <= max_days):
        reasons.append(f"trading_days_to_report={trading_days}, not in [{min_days}, {max_days}]")

    # Check price and volume
    if history is not None and not history.empty and "Close" in history:
        close = history["Close"].astype(float)
        if len(close) >= 20:
            current_price = float(close.iloc[-1])
            ma20 = float(close.rolling(20).mean().iloc[-1])
            metrics["current_price"] = round(current_price, 2)
            metrics["ma20"] = round(ma20, 2)

            if current_price < cfg.get("min_price", 5.0):
                reasons.append(f"price={current_price:.2f} < min_price={cfg.get('min_price', 5.0)}")

            if current_price < ma20:
                reasons.append(f"close={current_price:.2f} < MA20={ma20:.2f}")

            # Calculate dollar volume
            volume = history["Volume"].astype(float).fillna(0)
            dollar_volume = (close * volume).tail(20).mean()
            metrics["avg_dollar_volume_20d"] = round(dollar_volume, 2)

            if dollar_volume < cfg.get("min_avg_dollar_volume_20d", 20_000_000):
                reasons.append(f"avg_dollar_volume={dollar_volume:.0f} < min={cfg.get('min_avg_dollar_volume_20d', 20_000_000)}")
        else:
            reasons.append("insufficient price history (need 20+ days)")
    else:
        reasons.append("no price history available")

    # Check data quality
    if data_quality_score < cfg.get("min_data_quality_score", 0.8):
        reasons.append(f"data_quality={data_quality_score:.2f} < min={cfg.get('min_data_quality_score', 0.8)}")

    # Check report date confidence
    if report_date_confidence < cfg.get("min_report_date_confidence", 0.7):
        reasons.append(f"report_date_confidence={report_date_confidence:.2f} < min={cfg.get('min_report_date_confidence', 0.7)}")

    return {
        "eligible": len(reasons) == 0,
        "reasons": reasons,
        "metrics": metrics,
    }


def calculate_pre_earnings_expectation_revision_signal(
    symbol: str,
    earnings: dict[str, Any],
    history: pd.DataFrame,
    peer_group: dict[str, Any],
    all_earnings: dict[str, dict[str, Any]],
    all_histories: dict[str, pd.DataFrame],
    benchmark_history: pd.DataFrame,
    now: date,
    config: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Calculate pre-earnings expectation revision signal for a single symbol.

    This is the main entry point for the pre-earnings strategy.  It:
    1. Fetches EPS revision data
    2. Fetches revenue estimate data
    3. Calculates peer inference score
    4. Calculates relative momentum score
    5. Computes raw score with configurable weights
    6. Applies risk adjustments
    7. Checks eligibility criteria
    8. Returns standardized signal JSON

    Args:
        symbol: Stock ticker symbol.
        earnings: Earnings data for this symbol.
        history: OHLCV DataFrame for this symbol.
        peer_group: Peer group definition for this symbol.
        all_earnings: Earnings data for all symbols in the group.
        all_histories: OHLCV DataFrames for all symbols.
        benchmark_history: Benchmark OHLCV DataFrame (e.g., SPY).
        now: Current date (for point-in-time correctness).
        config: Optional configuration overrides.

    Returns:
        Dict with complete signal data, or None if no report date.
    """
    cfg = {**_PRE_EARNINGS_DEFAULT_CONFIG, **(config or {})}

    # Get target report date
    report_date_str = earnings.get("next_report_date")
    report_date = _iso_day(report_date_str)
    if not report_date:
        return None

    # Calculate trading days to report
    trading_days = 0
    current = now
    while current < report_date:
        current += timedelta(days=1)
        if current.weekday() < 5:
            trading_days += 1

    # Check if in target window (for highlighting)
    min_days = cfg.get("entry_window_min_trading_days", 6)
    max_days = cfg.get("entry_window_max_trading_days", 10)
    in_target_window = min_days <= trading_days <= max_days

    # Determine report session (BMO/AMC/UNKNOWN)
    report_session = "UNKNOWN"
    # yfinance doesn't reliably provide this; mark as unknown

    # Fetch EPS revision data
    eps_data = _get_eps_revisions(symbol)

    # Fetch revenue estimate data
    revenue_data = _get_revenue_estimate(symbol)

    # Calculate peer inference score
    peer_result = _peer_inference_score(symbol, peer_group, all_earnings, all_histories, now)

    # Calculate relative momentum score
    momentum_data = calculate_relative_momentum(history, benchmark_history, 10)
    momentum_result = _relative_momentum_score(momentum_data)

    # Calculate analyst revision score
    revision_result = _analyst_revision_score(eps_data)

    # Calculate revenue revision score
    revenue_result = _revenue_revision_score(revenue_data)

    # Calculate data quality
    report_date_confidence = 0.9 if earnings.get("next_report_date") else 0.3
    data_quality = _calculate_data_quality(eps_data, revenue_data, peer_result, momentum_data, report_date_confidence)

    # Check eligibility
    eligibility = _check_eligibility(
        symbol, report_date, now, history,
        data_quality["score"], report_date_confidence, cfg
    )

    # Calculate raw score with weights
    weights = cfg.get("score_weights", _PRE_EARNINGS_DEFAULT_CONFIG["score_weights"])
    score_components = {}
    weighted_sum = 0.0
    weight_sum = 0.0

    if revision_result["score"] is not None:
        w = weights.get("analyst_revision", 0.40)
        score_components["analyst_revision"] = revision_result["score"]
        weighted_sum += w * revision_result["score"]
        weight_sum += w

    if peer_result["score"] is not None:
        w = weights.get("peer_inference", 0.25)
        score_components["peer_inference"] = peer_result["score"]
        weighted_sum += w * peer_result["score"]
        weight_sum += w

    if momentum_result["score"] is not None:
        w = weights.get("relative_momentum", 0.20)
        score_components["relative_momentum"] = momentum_result["score"]
        weighted_sum += w * momentum_result["score"]
        weight_sum += w

    if revenue_result["score"] is not None:
        w = weights.get("revenue_revision", 0.15)
        score_components["revenue_revision"] = revenue_result["score"]
        weighted_sum += w * revenue_result["score"]
        weight_sum += w

    raw_score = weighted_sum / weight_sum if weight_sum > 0 else 0.0

    # Risk adjustments (MVP defaults to 1.0)
    risk_adjustments = {
        "data_quality_factor": min(1.0, data_quality["score"] / cfg.get("min_data_quality_score", 0.8)),
        "valuation_factor": 1.0,  # TODO: implement valuation check
        "volatility_factor": 1.0,  # TODO: implement volatility adjustment
        "concentration_factor": 1.0,  # TODO: implement concentration check
    }

    final_score = raw_score
    for factor in risk_adjustments.values():
        final_score *= factor

    # Calculate exit plan
    exit_days_before = cfg.get("scheduled_exit_days_before_report", 2)
    scheduled_exit_date = report_date - timedelta(days=exit_days_before)
    # Adjust for weekends (move to Friday if exit falls on weekend)
    while scheduled_exit_date.weekday() >= 5:
        scheduled_exit_date -= timedelta(days=1)

    # Calculate entry zone from current price and ATR
    current_price = float(history["Close"].iloc[-1]) if history is not None and not history.empty and "Close" in history else None
    atr_14 = None
    if history is not None and not history.empty:
        atr_series = calculate_atr(history, 14)
        if not atr_series.empty and not pd.isna(atr_series.iloc[-1]):
            atr_14 = round(float(atr_series.iloc[-1]), 2)

    # MA20 for stop loss
    ma20 = None
    if history is not None and not history.empty and "Close" in history and len(history) >= 20:
        ma20 = round(float(history["Close"].astype(float).rolling(20).mean().iloc[-1]), 2)

    # Build signal
    signal = {
        "symbol": symbol,
        "signal_type": "pre_earnings_expectation_revision",
        "signal_version": cfg.get("signal_version", "v0.2.0"),
        "as_of": datetime(now.year, now.month, now.day, 16, 10, 0).isoformat(),
        "in_target_window": in_target_window,
        "report": {
            "target_date": report_date_str,
            "session": report_session,
            "trading_days_to_report": trading_days,
            "date_confidence": report_date_confidence,
        },
        "score": {
            "final": round(final_score, 4),
            "raw": round(raw_score, 4),
            "components": score_components,
            "risk_adjustments": {k: round(v, 4) for k, v in risk_adjustments.items()},
        },
        "evidence": {
            "eps_revision_up_30d": eps_data.get("revision_up_30d"),
            "eps_revision_down_30d": eps_data.get("revision_down_30d"),
            "eps_change_30d": eps_data.get("eps_change_30d"),
            "revenue_change_30d": revenue_data.get("revenue_change_30d"),
            "peer_events": [e["symbol"] + ": " + e["type"] for e in peer_result.get("peer_events", [])],
            "volume_ratio_20d": calculate_volume_ratio(history, 20),
            "stock_return_10d": momentum_data.get("stock_return"),
            "benchmark_return_10d": momentum_data.get("benchmark_return"),
            "relative_return_10d": momentum_data.get("relative_return"),
        },
        "execution": {
            "signal_generated_at": datetime(now.year, now.month, now.day, 16, 10, 0).isoformat(),
            "entry_rule": "next_open",
            "entry_zone": {
                "low": round(current_price * 0.98, 2) if current_price else None,
                "high": round(current_price * 1.02, 2) if current_price else None,
                "current": round(current_price, 2) if current_price else None,
            },
            "scheduled_exit": scheduled_exit_date.isoformat(),
            "exit_rule": "T-2 close",
        },
        "risk_control": {
            "signal_stop": ma20,
            "stop_trigger": "close_below_ma20",
            "execution_rule": "next_open",
            "gap_risk": "not_capped",
            "atr_14": atr_14,
            "max_holding_days": cfg.get("max_holding_days", 8),
            "max_position_weight": cfg.get("max_position_weight", 0.03),
        },
        "expected_return": {
            "method": "insufficient_point_in_time_samples",
            "sample_size": 0,
            "median_return": None,
            "win_rate": None,
            "risk_reward_ratio": None,
        },
        "data_quality": data_quality,
    }

    return signal


def build_pre_earnings_signals(
    groups: Iterable[dict[str, Any]],
    earnings: dict[str, dict[str, Any]],
    histories: dict[str, pd.DataFrame],
    benchmark_history: pd.DataFrame,
    now: date,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build pre-earnings expectation revision signals for all eligible symbols.

    Iterates through all groups and symbols, calculates signals, and returns
    sorted list of eligible signals.

    Args:
        groups: List of peer group definitions.
        earnings: Earnings data for all symbols.
        histories: OHLCV DataFrames for all symbols.
        benchmark_history: Benchmark OHLCV DataFrame.
        now: Current date.
        config: Optional configuration overrides.

    Returns:
        List of signal dicts, sorted by: in_target_window first, then trading_days ascending, then final_score descending.
    """
    signals = []
    cfg = {**_PRE_EARNINGS_DEFAULT_CONFIG, **(config or {})}
    min_score = cfg.get("min_final_score", 0.65)

    for group in groups:
        for symbol in group.get("symbols", []):
            symbol_earnings = earnings.get(symbol, {})
            symbol_history = histories.get(symbol, pd.DataFrame())

            signal = calculate_pre_earnings_expectation_revision_signal(
                symbol=symbol,
                earnings=symbol_earnings,
                history=symbol_history,
                peer_group=group,
                all_earnings=earnings,
                all_histories=histories,
                benchmark_history=benchmark_history,
                now=now,
                config=config,
            )

            if signal is not None:
                # Include all signals with report dates, regardless of window
                # Only filter by minimum score for quality
                if signal["score"]["final"] >= min_score * 0.5:  # Lower threshold for upcoming
                    signals.append(signal)

    # Sort: in_target_window first, then by trading_days ascending, then by final_score descending
    signals.sort(key=lambda s: (
        not s.get("in_target_window", False),  # True sorts before False
        s.get("report", {}).get("trading_days_to_report", 999),  # Closer dates first
        -s["score"]["final"],  # Higher scores first
    ))
    return signals


def scan_pre_earnings_signals(
    group_id: str = "all",
    progress: Callable[[str, float], None] | None = None,
    cache_path: Path | None = None,
    cache_ttl_seconds: int = 6 * 3600,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Scan for pre-earnings expectation revision signals.

    This is the main scanning entry point, similar to scan_peer_earnings_signals().
    It fetches data, calculates signals, and returns a complete payload.

    Args:
        group_id: Peer group ID to scan ("all" for all groups).
        progress: Optional progress callback.
        cache_path: Optional file path for caching.
        cache_ttl_seconds: Cache TTL in seconds.
        config: Optional configuration overrides.

    Returns:
        Dict with scan metadata, signals list, and method notes.
    """
    emit = progress or (lambda _phase, _pct: None)
    cfg = {**_PRE_EARNINGS_DEFAULT_CONFIG, **(config or {})}
    started = time.time()

    # Select groups
    groups = PEER_GROUPS if group_id == "all" else [g for g in PEER_GROUPS if g["id"] == group_id]
    if not groups:
        raise ValueError(f"unsupported peer group: {group_id}")

    symbols = sorted({symbol for group in groups for symbol in group["symbols"]})
    emit(f"读取 {len(symbols)} 家公司财报日历", 0.12)

    today = datetime.now(timezone.utc).date()

    # Fetch earnings data (reuse existing function)
    earnings: dict[str, dict[str, Any]] = {}
    overrides = _load_calendar_overrides()
    with ThreadPoolExecutor(max_workers=min(12, max(1, len(symbols)))) as executor:
        futures = {executor.submit(_earnings_record, symbol, today): symbol for symbol in symbols}
        for index, future in enumerate(as_completed(futures), start=1):
            symbol = futures[future]
            try:
                earnings[symbol] = _apply_calendar_override(future.result(), overrides.get(symbol))
            except Exception:
                earnings[symbol] = _apply_calendar_override(
                    {"symbol": symbol, "latest_report_date": None, "next_report_date": None},
                    overrides.get(symbol),
                )
            if index % 10 == 0 or index == len(symbols):
                emit(f"读取财报节点 {index}/{len(symbols)}", 0.12 + 0.38 * index / max(1, len(symbols)))

    emit("批量拉取量价数据", 0.55)
    download, market_data_sources = download_daily_history(symbols + ["SPY"], period="4mo")
    histories = {symbol: _ticker_frame(download, symbol) for symbol in symbols}
    benchmark_history = _ticker_frame(download, "SPY")

    emit("计算财报前预期修正信号", 0.82)
    signals = build_pre_earnings_signals(groups, earnings, histories, benchmark_history, today, config)

    # Calculate statistics
    eligible_count = sum(1 for s in signals if s.get("score", {}).get("final", 0) >= cfg.get("min_final_score", 0.65))
    avg_score = sum(s["score"]["final"] for s in signals) / len(signals) if signals else 0
    avg_days = sum(s["report"]["trading_days_to_report"] for s in signals) / len(signals) if signals else 0

    payload = {
        "scan_type": "pre_earnings_expectation_revision",
        "scan_time": datetime.utcnow().isoformat() + "Z",
        "signal_version": cfg.get("signal_version", "v0.2.0"),
        "parameters": {
            "group_id": group_id,
            "entry_window_min_trading_days": cfg.get("entry_window_min_trading_days"),
            "entry_window_max_trading_days": cfg.get("entry_window_max_trading_days"),
            "min_final_score": cfg.get("min_final_score"),
        },
        "statistics": {
            "total_symbols_scanned": len(symbols),
            "signals_detected": len(signals),
            "eligible_signals": eligible_count,
            "avg_score": round(avg_score, 4),
            "avg_days_to_report": round(avg_days, 1),
        },
        "signals": signals,
        "market_data_sources": market_data_sources,
        "elapsed_seconds": round(time.time() - started, 3),
        "method_note": (
            "财报前预期修正事件驱动策略：通过分析师EPS预期修正、同行财报推断、相对动量、营收预期修正四个维度"
            "检测预增信号，在财报前6-10个交易日生成信号，下一交易日开盘执行，财报前第2个交易日收盘退出。"
            "该策略承担股票方向性风险，不是无风险套利。所有阈值为启发式阈值，尚未经过point-in-time历史回测校准。"
        ),
        "risk_warnings": [
            "该策略承担股票方向性风险，不是无风险套利",
            "所有阈值为启发式阈值，尚未经过point-in-time历史回测校准",
            "guidance signal unavailable: structured guidance source not implemented",
            "expected_return计算需要积累足够历史样本后才能启用",
        ],
    }

    # Save to cache
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp = cache_path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(cache_path)

        # Append to history
        history_path = cache_path.parent / "_pre_earnings_signal_history.jsonl"
        with history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    emit("整理财报前预期修正候选", 0.96)
    return payload


def build_pre_earnings_signals_for_test(
    groups: Iterable[dict[str, Any]],
    earnings: dict[str, dict[str, Any]],
    histories: dict[str, pd.DataFrame],
    benchmark_history: pd.DataFrame,
    as_of: date,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Deterministic seam for unit tests.

    This function bypasses network calls and accepts pre-built data.
    Use this for testing the signal calculation logic without yfinance.
    """
    return build_pre_earnings_signals(groups, earnings, histories, benchmark_history, as_of, config)
