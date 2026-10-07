#!/usr/bin/env python3
"""Tradable options win-rate screening framework.

This optimized version fixes four issues in the previous script:
- use real option-chain strikes instead of synthetic rounded strikes
- calculate executable premium, max loss, breakeven, ROC, and expected value
- score liquidity on the selected legs, not the whole chain
- keep model POP separate from expected value and tradability filters
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import argparse
import warnings
import html
import sys
from functools import lru_cache
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd
import yfinance as yf

AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from pullback_signal_service import detect_pullback_setup
from scipy.special import erf
from scipy.stats import norm

from fincept_adapters import cboe_chain_to_frames, get_cboe_option_chain
from market_data_service import get_daily_history
import cost_model

warnings.filterwarnings("ignore")

CONFIG = {
    "strategy_name": "Short-DTE Tradable POP Strategy",
    "target_expiry": "2026-06-26",
    "strategy_family": "auto",
    "total_budget": 2000.0,
    "max_position_pct": 0.30,
    "max_single_trade_pct": 0.50,
    "min_cash_reserve_pct": 0.25,
    "min_credit_roc": 0.10,
    "max_same_theme": 2,
    "max_pair_corr": 0.75,
    "history_period": "2y",
    "cache_enabled": True,
    "reuse_session_results": False,
    "cache_dir": os.environ.get("VIBE_YF_CACHE_DIR", ""),
    "universe_snapshot_dir": os.environ.get("VIBE_UNIVERSE_SNAPSHOT_DIR", ""),
    "history_refresh_overlap_days": 7,
    "options_expiry_cache_ttl_hours": 6,
    "option_chain_cache_ttl_minutes": 30,
    "cboe_option_chain_enabled": True,
    "fundamental_cache_ttl_hours": 24,
    "universe": "spx",
    "sp100_source_url": "https://en.wikipedia.org/wiki/S%26P_100",
    "us_volume_top_n": 50,
    "us_volume_source_url": "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=50&offset=0&download=true",
    "komp_source_urls": [
        "https://www.quiverquant.com/etf/State%20Street%20SPDR%20S%26P%20Kensho%20New%20Economies%20Composite%20ETF",
        "https://www.bestetf.net/etf/KOMP/holdings/",
        "https://www.financecharts.com/etfs/KOMP/holdings",
        "https://www.marketbeat.com/stocks/NYSEARCA/KOMP/holdings/",
        "https://stockanalysis.com/etf/komp/holdings/",
    ],
    "soxx_source_urls": [
        "https://www.quiverquant.com/etf/iShares%20Semiconductor%20ETF",
        "https://www.bestetf.net/etf/SOXX/holdings/",
        "https://stockanalysis.com/etf/soxx/holdings/",
        "https://www.financecharts.com/etfs/SOXX/holdings",
    ],
    "risk_free_rate": 0.045,
    "iv_hv_expensive": 1.15,
    "iv_hv_cheap": 0.85,
    "allow_estimated_quotes": False,
    "sell_fallback_haircut": 0.92,
    "buy_fallback_markup": 1.08,
    "min_option_leg_volume": 25,
    "min_option_leg_open_interest": 100,
    "max_option_bid_ask_spread_pct": 0.35,
    "min_trade_liquidity_score": 0.35,
    "gex_enabled": True,
    "gex_contract_multiplier": 100.0,
    "gex_flip_scan_pct": 0.20,
    "gex_high_abs_pct": 0.08,
    "gex_low_abs_pct": 0.02,
    "score_weights": {
        "pop": 0.30,
        "iv_hv": 0.15,
        "trend": 0.15,
        "roc": 0.15,
        "expected_value": 0.15,
        "liquidity": 0.10,
    },
    "research_weights": {
        "final": 0.45,
        "confidence": 0.25,
        "stress": 0.15,
        "historical": 0.15,
    },
}

SP100_FALLBACK = [
    "AAPL", "ABBV", "ABT", "ACN", "ADBE", "AMAT", "AMD", "AMGN", "AMT", "AMZN",
    "AVGO", "AXP", "BA", "BAC", "BKNG", "BLK", "BMY", "BNY", "BRK-B", "C",
    "CAT", "CL", "CMCSA", "COF", "COP", "COST", "CRM", "CSCO", "CVS", "CVX",
    "DE", "DHR", "DIS", "DUK", "EMR", "FDX", "GD", "GE", "GEV", "GILD",
    "GM", "GOOG", "GOOGL", "GS", "HD", "HON", "IBM", "INTC", "INTU", "ISRG",
    "JNJ", "JPM", "KO", "LIN", "LLY", "LMT", "LOW", "LRCX", "MA", "MCD",
    "MDLZ", "MDT", "META", "MMM", "MO", "MRK", "MS", "MSFT", "MU", "NEE",
    "NFLX", "NKE", "NOW", "NVDA", "ORCL", "PEP", "PFE", "PG", "PLTR", "PM",
    "QCOM", "RTX", "SBUX", "SCHW", "SO", "SPG", "T", "TMO", "TMUS", "TSLA",
    "TXN", "UBER", "UNH", "UNP", "UPS", "USB", "V", "VZ", "WFC", "WMT", "XOM",
]

KOMP_FALLBACK = [
    "AAPL", "ACHR", "ADSK", "AI", "ALAB", "AMAT", "AMD", "APP", "ASTS", "AVAV",
    "AVGO", "BE", "BBAI", "CDNS", "CIFR", "CLSK", "COHR", "COIN", "CRCL", "CRDO",
    "CRSP", "CRWD", "DDOG", "DNA", "ENPH", "ESLT", "FSLR", "GLW", "HIMX", "HOOD",
    "IONQ", "IREN", "ISRG", "JOBY", "KLAC", "KTOS", "LCID", "LRCX", "LUNR", "MARA",
    "MDB", "MP", "MU", "NET", "NOK", "NVDA", "OKLO", "PATH", "PLTR", "QBTS",
    "QCOM", "RGTI", "RIOT", "RKLB", "ROK", "RIVN", "RXRX", "SMCI", "SNOW", "SOFI",
    "SOUN", "SYM", "TDOC", "TDY", "TER", "TSLA", "TWST", "UBER", "UPST", "WOLF",
    "WULF",
]

SOXX_FALLBACK = [
    "ACLS", "ADI", "ALAB", "AMAT", "AMD", "ARM", "ASML", "AVGO", "CDNS", "COHR",
    "ENTG", "GFS", "INTC", "KLAC", "LRCX", "LSCC", "MCHP", "MPWR", "MRVL", "MU",
    "NVDA", "NXPI", "ON", "QCOM", "QRVO", "RMBS", "SMH", "SMTC", "SNPS", "SWKS",
    "TER", "TSM", "TXN", "UMC", "WOLF",
]

NDX_FALLBACK = [
    "AAPL", "ADBE", "ADI", "ADP", "ADSK", "AEP", "AMAT", "AMD", "AMGN", "AMZN",
    "APP", "ARM", "ASML", "AVGO", "AXON", "BKNG", "CDNS", "CEG", "CHTR", "CMCSA",
    "COST", "CPRT", "CRWD", "CSCO", "CSX", "CTAS", "DASH", "DDOG", "DXCM", "EA",
    "EXC", "FANG", "FAST", "FTNT", "GEHC", "GILD", "GOOG", "GOOGL", "HON", "IDXX",
    "INTC", "INTU", "ISRG", "KDP", "KHC", "KLAC", "LIN", "LRCX", "MAR", "MCHP",
    "MDLZ", "MELI", "META", "MNST", "MRVL", "MSFT", "MSTR", "MU", "NFLX", "NVDA",
    "ODFL", "ON", "ORLY", "PANW", "PAYX", "PCAR", "PDD", "PEP", "PLTR", "QCOM",
    "REGN", "ROP", "ROST", "SBUX", "SHOP", "SNPS", "TEAM", "TMUS", "TSLA", "TTD",
    "TXN", "VRTX", "WBD", "WDAY", "XEL", "ZS",
]

SPX_FALLBACK = sorted(set(SP100_FALLBACK + NDX_FALLBACK + [
    "A", "AAL", "AEE", "AIG", "AJG", "ALL", "AME", "AON", "APD", "APH",
    "ATO", "AWK", "AZO", "BDX", "BIIB", "BSX", "CARR", "CB", "CHD", "CI",
    "CINF", "CME", "CNC", "CTRA", "DAL", "DG", "DOW", "DPZ", "DTE", "ECL",
    "ED", "EFX", "EL", "EOG", "EQR", "ES", "ETN", "EXPE", "F", "FIS",
    "FISV", "FOX", "FOXA", "HAL", "HCA", "HLT", "HPQ", "HSY", "HUM", "ICE",
    "ILMN", "ITW", "KMB", "KMI", "KR", "LEN", "MPC", "MSCI", "NEM", "NOC",
    "O", "OXY", "PNC", "PSX", "PYPL", "REGN", "ROST", "SHW", "SLB", "STZ",
    "SYK", "TGT", "TJX", "TRV", "VRTX", "WM", "ZTS",
]))

DJIA_FALLBACK = [
    "AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS",
    "GS", "HD", "HON", "IBM", "JNJ", "JPM", "KO", "MCD", "MMM", "MRK",
    "MSFT", "NKE", "NVDA", "PG", "SHW", "TRV", "UNH", "V", "VZ", "WMT",
]

RUSSELL_LARGE_FALLBACK = sorted(set(SPX_FALLBACK + [
    "AFRM", "APP", "BILL", "BROS", "CAVA", "CELH", "COIN", "CRDO", "DASH", "DKNG",
    "DUOL", "ELF", "HOOD", "MDB", "NET", "NU", "OKTA", "ONON", "RBLX", "SNOW",
    "TOST", "U", "ZS",
]))

RUSSELL_SMALL_FALLBACK = [
    "AAOI", "ACLS", "AEHR", "ALNT", "AMBA", "APLD", "ARLO", "ASTS", "AVAV", "BE",
    "BHE", "BLDP", "CEVA", "CIFR", "CLSK", "CRDO", "DAKT", "ENVX", "FORM", "HIMS",
    "IONQ", "IREN", "JOBY", "KTOS", "LITE", "LUNR", "MARA", "MP", "NVTS", "ONDS",
    "OUST", "PDFS", "QBTS", "RGTI", "RIOT", "RKLB", "RXRX", "SERV", "SOUN", "TSSI",
    "UPST", "VECO", "VICR", "WOLF", "WULF", "ZETA",
]

MIDCAP_FALLBACK = [
    "AA", "ALB", "ALLY", "ARMK", "BURL", "CHWY", "CROX", "CZR", "DKS", "DOCU",
    "EXAS", "FIVE", "FND", "GME", "GNRC", "HUBS", "LAD", "LEVI", "LSCC", "MANH",
    "MIDD", "NTRA", "PEN", "RGEN", "RIVN", "SAIA", "SCI", "SFM", "SWKS", "TECH",
    "TREX", "TWLO", "UHAL", "VFC", "WING", "WSM",
]

SMALLCAP_QUALITY_FALLBACK = sorted(set(RUSSELL_SMALL_FALLBACK + [
    "ACA", "BCPC", "BOOT", "BOX", "CALM", "CRVL", "ENSG", "EXPO", "FCFS", "FN",
    "GATX", "HRI", "ITRI", "JBTM", "KAI", "MMSI", "MTH", "NOVT", "OLED", "PIPR",
    "POWI", "PSMT", "SKY", "SLAB", "TEX", "UFPI", "WTS",
]))

NASDAQ_COMPOSITE_FALLBACK = sorted(set(NDX_FALLBACK + [
    "AFRM", "ALAB", "BILI", "BKR", "BMBL", "BMRN", "COIN", "CRSP", "DJT", "DOCU",
    "DUOL", "ENPH", "EXAS", "FSLR", "HOOD", "IONQ", "LITE", "MDB", "MRNA", "NTES",
    "OKTA", "PCTY", "RBLX", "RIVN", "ROKU", "SMCI", "SOUN", "TMDX", "TTWO", "UPST",
]))

WILSHIRE_5000_FALLBACK = sorted(set(RUSSELL_LARGE_FALLBACK + MIDCAP_FALLBACK + SMALLCAP_QUALITY_FALLBACK))

THEMATIC_UNIVERSE_FALLBACKS = {
    "spmo": ["AVGO", "GEV", "META", "NFLX", "NVDA", "PLTR", "TSLA", "UBER", "WMT"],
    "dtcr": ["AMT", "ANET", "CCI", "CSCO", "DLR", "EQIX", "GLW", "IRM", "LUMN", "VRT"],
    "pave": ["CAT", "CSX", "DE", "EMR", "ETN", "FAST", "GEV", "MLM", "NUE", "PWR", "ROK", "UNP", "URI", "VMC"],
    "aiq": ["AMD", "ANET", "ASML", "AVGO", "CRM", "GOOGL", "META", "MSFT", "NOW", "NVDA", "ORCL", "PLTR", "SNOW", "TSM"],
    "ura": ["BWXT", "CCJ", "CEG", "DNN", "LEU", "NNE", "NXE", "OKLO", "SMR", "UEC", "UUUU"],
    "ppa": ["AVAV", "BA", "BWXT", "GD", "HII", "KTOS", "LDOS", "LHX", "LMT", "NOC", "PLTR", "RTX"],
    "aipo": ["AMD", "AVGO", "BE", "CCJ", "CEG", "ETN", "GEV", "NVDA", "PWR", "VRT"],
    "qtum": ["AMD", "ASML", "GOOGL", "IBM", "IONQ", "MSFT", "NVDA", "QBTS", "RGTI", "TSM"],
    "ai_supply_chain_smallcap": [
        "ACLS", "AEHR", "ALGM", "ALNT", "AMBA", "ARBE", "ATOM", "AUR", "AVAV", "BE",
        "BHE", "BLDP", "CEVA", "COHR", "CRDO", "DAKT", "DDD", "DM", "EAF", "EMKR",
        "ENVX", "FORM", "HIMX", "INDI", "IONQ", "IRBT", "IREN", "JBL", "JOBY", "KTOS",
        "LASR", "LITE", "LUMN", "LUNR", "MXL", "NVTS", "ONDS", "OUST", "PDFS", "POET",
        "QBTS", "QUIK", "RGTI", "RKLB", "SANM", "SERV", "SMTC", "SOUN", "SYM", "TSSI",
        "VECO", "VICR", "WOLF", "WULF", "ZETA",
    ],
}

UNIVERSE_REGISTRY = {
    "spx": {
        "label": "S&P 500 Index", "priority": 1, "tier": "broad",
        "aliases": {"sp500", "s&p500", "s&p_500", "spy"},
        "source_urls": ["https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "https://stockanalysis.com/etf/spy/holdings/"],
        "fallback": SPX_FALLBACK, "min_holdings": 80,
    },
    "ixic": {
        "label": "Nasdaq Composite Index", "priority": 2, "tier": "growth",
        "aliases": {"comp", "nasdaq_composite", "nasdaq", "oneq"},
        "source_urls": ["https://stockanalysis.com/etf/oneq/holdings/"],
        "fallback": NASDAQ_COMPOSITE_FALLBACK, "min_holdings": 60,
    },
    "djia": {
        "label": "Dow Jones Industrial Average", "priority": 3, "tier": "blue_chip",
        "aliases": {"dji", "dow", "dow30"},
        "source_urls": ["https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average", "https://stockanalysis.com/etf/dia/holdings/"],
        "fallback": DJIA_FALLBACK, "min_holdings": 25,
    },
    "ndx": {
        "label": "Nasdaq-100 / NDX", "priority": 4, "tier": "core",
        "aliases": {"nasdaq100", "nasdaq_100", "qqq"},
        "source_urls": ["https://indexes.nasdaqomx.com/Index/Overview/NDX", "https://en.wikipedia.org/wiki/Nasdaq-100", "https://stockanalysis.com/etf/qqq/holdings/"],
        "fallback": NDX_FALLBACK, "min_holdings": 75,
    },
    "rut": {
        "label": "Russell 2000 Index", "priority": 5, "tier": "small_cap",
        "aliases": {"russell2000", "russell_2000", "iwm"},
        "source_urls": ["https://stockanalysis.com/etf/iwm/holdings/"],
        "fallback": RUSSELL_SMALL_FALLBACK, "min_holdings": 30,
    },
    "rua": {
        "label": "Russell 3000 Index", "priority": 6, "tier": "total_market",
        "aliases": {"russell3000", "russell_3000", "iwv"},
        "source_urls": ["https://stockanalysis.com/etf/iwv/holdings/"],
        "fallback": RUSSELL_LARGE_FALLBACK, "min_holdings": 80,
    },
    "mid": {
        "label": "S&P MidCap 400 Index", "priority": 7, "tier": "mid_cap",
        "aliases": {"sp400", "s&p400", "s&p_midcap_400", "midcap400", "mdy"},
        "source_urls": ["https://stockanalysis.com/etf/mdy/holdings/"],
        "fallback": MIDCAP_FALLBACK, "min_holdings": 30,
    },
    "sml": {
        "label": "S&P SmallCap 600 Index", "priority": 8, "tier": "small_cap_quality",
        "aliases": {"sp600", "s&p600", "s&p_smallcap_600", "smallcap600", "ijr"},
        "source_urls": ["https://stockanalysis.com/etf/ijr/holdings/"],
        "fallback": SMALLCAP_QUALITY_FALLBACK, "min_holdings": 30,
    },
    "w5000": {
        "label": "FT Wilshire 5000 Index", "priority": 9, "tier": "total_market",
        "aliases": {"wilshire5000", "wilshire_5000", "vti"},
        "source_urls": ["https://stockanalysis.com/etf/vti/holdings/"],
        "fallback": WILSHIRE_5000_FALLBACK, "min_holdings": 100,
    },
    "sox": {
        "label": "PHLX Semiconductor Sector Index", "priority": 10, "tier": "semiconductor",
        "aliases": {"phlx_semiconductor", "soxx", "smh"},
        "source_urls": ["https://stockanalysis.com/etf/soxx/holdings/", "https://stockanalysis.com/etf/smh/holdings/"],
        "fallback": SOXX_FALLBACK, "min_holdings": 20,
    },
    "spmo": {
        "label": "S&P 500 Momentum / SPMO", "priority": 2, "tier": "factor",
        "source_urls": ["https://www.ssga.com/us/en/intermediary/etfs/spdr-portfolio-sp-500-momentum-etf-spmo", "https://stockanalysis.com/etf/spmo/holdings/"],
        "fallback": THEMATIC_UNIVERSE_FALLBACKS["spmo"], "min_holdings": 8,
    },
    "dtcr": {
        "label": "DTCR", "priority": 3, "tier": "theme",
        "source_urls": ["https://www.globalxetfs.com/funds/dtcr/", "https://stockanalysis.com/etf/dtcr/holdings/"],
        "fallback": THEMATIC_UNIVERSE_FALLBACKS["dtcr"], "min_holdings": 8,
    },
    "pave": {
        "label": "PAVE", "priority": 4, "tier": "theme",
        "source_urls": ["https://www.globalxetfs.com/funds/pave/", "https://stockanalysis.com/etf/pave/holdings/"],
        "fallback": THEMATIC_UNIVERSE_FALLBACKS["pave"], "min_holdings": 10,
    },
    "aiq": {
        "label": "AIQ", "priority": 5, "tier": "theme",
        "source_urls": ["https://www.globalxetfs.com/funds/aiq/", "https://stockanalysis.com/etf/aiq/holdings/"],
        "fallback": THEMATIC_UNIVERSE_FALLBACKS["aiq"], "min_holdings": 10,
    },
    "ura": {
        "label": "URA", "priority": 6, "tier": "satellite",
        "source_urls": ["https://www.globalxetfs.com/funds/ura/", "https://stockanalysis.com/etf/ura/holdings/"],
        "fallback": THEMATIC_UNIVERSE_FALLBACKS["ura"], "min_holdings": 8,
    },
    "ppa": {
        "label": "PPA", "priority": 7, "tier": "theme",
        "source_urls": ["https://www.invesco.com/us/financial-products/etfs/product-detail?productId=ETF-PPA", "https://stockanalysis.com/etf/ppa/holdings/"],
        "fallback": THEMATIC_UNIVERSE_FALLBACKS["ppa"], "min_holdings": 8,
    },
    "aipo": {
        "label": "AIPO", "priority": 8, "tier": "experimental",
        "source_urls": ["https://www.defianceetfs.com/aipo", "https://stockanalysis.com/etf/aipo/holdings/"],
        "fallback": THEMATIC_UNIVERSE_FALLBACKS["aipo"], "min_holdings": 6,
    },
    "qtum": {
        "label": "QTUM", "priority": 9, "tier": "experimental",
        "source_urls": ["https://www.defianceetfs.com/qtum", "https://stockanalysis.com/etf/qtum/holdings/"],
        "fallback": THEMATIC_UNIVERSE_FALLBACKS["qtum"], "min_holdings": 8,
    },
    "ai_supply_chain_smallcap": {
        "label": "AI Supply Chain Small Cap", "priority": 10, "tier": "speculative_theme",
        "aliases": {"ai_supply_chain", "serenity_ai", "ai_smallcap", "ai_picks"},
        "source_urls": [],
        "fallback": THEMATIC_UNIVERSE_FALLBACKS["ai_supply_chain_smallcap"], "min_holdings": 20,
    },
}

US_VOLUME_TOP_FALLBACK = [
    "NVDA", "TSLA", "AMD", "AAPL", "PLTR", "AMZN", "INTC", "SOFI", "F", "BAC",
    "MARA", "RIVN", "T", "PFE", "GOOGL", "GOOG", "MSFT", "META", "NIO", "WBD",
    "RIOT", "CLSK", "IREN", "WULF", "COIN", "HOOD", "AVGO", "MU", "SMCI", "QCOM",
    "BABA", "UBER", "CCL", "SNAP", "LCID", "AAL", "OPEN", "XOM", "WFC", "JPM",
    "CSCO", "CMCSA", "KO", "DIS", "NFLX", "SHOP", "PATH", "UPST", "RKLB", "ARM",
]

THEME_MAP = {
    "crypto_mining": {"IREN", "RIOT", "CORZ", "WULF", "CLSK", "MARA", "BTBT", "CIFR"},
    "semis_ai": {"AVGO", "AMD", "NVDA", "SMCI", "ALAB", "COHR", "HIMX", "GLW", "MU", "INTC", "AMAT", "LRCX", "KLAC", "QCOM", "MRVL", "ARM"},
    "fintech": {"SOFI", "UPST", "CRCL", "HOOD", "COIN", "AFRM", "PYPL", "SQ"},
    "ev_auto": {"TSLA", "RIVN", "LCID", "NIO", "XPEV", "LI", "F", "GM"},
    "meme_smallcap": {"GME", "AMC", "BB", "KOSS", "OPEN", "SOUN", "RGTI", "PLUG", "PTON"},
    "mega_tech": {"AAPL", "MSFT", "AMZN", "META", "GOOG", "GOOGL", "NFLX", "TSLA"},
    "biotech_event": {"OTLK", "MRNA", "BNTX", "NVAX", "SAVA", "DNA", "RXRX", "CRSP"},
}

NON_EQUITY_HOLDING_TICKERS = {"WFFUT", "XTSLA"}


def normalize_yfinance_symbol(symbol: str) -> str:
    """Normalize index symbols to yfinance symbols."""
    return symbol.strip().upper().replace(".", "-")


def is_equity_holding_ticker(symbol: str) -> bool:
    """Reject cash, futures and provider-specific synthetic holding rows."""
    value = normalize_yfinance_symbol(symbol)
    compact = value.replace("-", "")
    return bool(
        value
        and value not in {"CASH", "USD", "US DOLLAR"}
        and value not in NON_EQUITY_HOLDING_TICKERS
        and not value.endswith("FUT")
        and compact.isalpha()
        and 1 <= len(compact) <= 6
        and value[0].isalpha()
    )


def cache_root() -> str:
    configured = str(CONFIG.get("cache_dir", "")).strip()
    if configured:
        return configured
    if os.path.isdir("/app/agent"):
        return "/app/agent/data_cache/yfinance"
    return os.path.join(os.getcwd(), "data_cache", "yfinance")


def cache_symbol(symbol: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in symbol.upper())


def ensure_cache_dir(*parts: str) -> str:
    path = os.path.join(cache_root(), *parts)
    os.makedirs(path, exist_ok=True)
    return path


def cache_is_fresh(path: str, ttl_seconds: int) -> bool:
    if not os.path.exists(path):
        return False
    return (datetime.now() - datetime.fromtimestamp(os.path.getmtime(path))).total_seconds() <= ttl_seconds


def read_csv_cache(path: str, parse_dates: Optional[list[str]] = None) -> Optional[pd.DataFrame]:
    try:
        if os.path.exists(path):
            return pd.read_csv(path, parse_dates=parse_dates)
    except Exception:
        return None
    return None


def write_csv_cache(frame: pd.DataFrame, path: str) -> None:
    try:
        frame.to_csv(path, index=False)
    except Exception as exc:
        print(f"[WARN] could not write cache {path}: {exc}")


def finite_float(value: object, default: Optional[float] = None) -> Optional[float]:
    try:
        number = float(value)
    except Exception:
        return default
    if not math.isfinite(number):
        return default
    return number


def load_sp100_universe() -> tuple[list[str], str]:
    """Load S&P 100 tickers, with a bundled fallback for offline runs.

    The live source is Wikipedia's S&P 100 constituents table. Wikipedia notes
    that the current table is "as of September 22, 2025" and contains 101
    entries because Alphabet has both GOOG and GOOGL share classes.
    """
    url = CONFIG["sp100_source_url"]
    try:
        req = Request(url, headers={"User-Agent": "Vibe-Trading/0.1 options screener"})
        with urlopen(req, timeout=20) as resp:
            html_text = resp.read().decode("utf-8", errors="replace")
        tables = pd.read_html(StringIO(html_text))
        for table in tables:
            symbol_col = next((c for c in table.columns if str(c).lower() in {"symbol", "ticker"}), None)
            if symbol_col is None:
                continue
            raw = [str(x).strip() for x in table[symbol_col].dropna().tolist()]
            tickers = sorted({normalize_yfinance_symbol(x) for x in raw if x and x.lower() != "nan"})
            if len(tickers) >= 90:
                return tickers, f"live:{url}"
    except Exception as exc:
        print(f"[WARN] S&P 100 live universe unavailable: {exc}; using fallback list.")
    return sorted(SP100_FALLBACK), "fallback:bundled_sp100"


def load_komp_universe() -> tuple[list[str], str]:
    """Load KOMP holdings, with a curated fallback for offline research runs.

    KOMP holdings move over time, so the live parser is intentionally permissive:
    it accepts common table columns such as Symbol, Ticker, or Holding Ticker.
    """
    last_error = None
    for url in CONFIG["komp_source_urls"]:
        try:
            req = Request(url, headers={"User-Agent": "Vibe-Trading/0.1 options screener"})
            with urlopen(req, timeout=20) as resp:
                html_text = resp.read().decode("utf-8", errors="replace")
            tables = pd.read_html(StringIO(html_text))
            for table in tables:
                symbol_col = next(
                    (
                        c for c in table.columns
                        if str(c).strip().lower() in {"symbol", "ticker", "holding ticker", "asset", "holdings"}
                    ),
                    None,
                )
                if symbol_col is None:
                    continue
                raw = []
                for x in table[symbol_col].dropna().tolist():
                    token = str(x).strip().split()[0]
                    token = token.split(":")[-1].split(".")[0]
                    raw.append(token)
                tickers = sorted({
                    normalize_yfinance_symbol(x)
                    for x in raw
                    if x and x.lower() != "nan" and x.upper() not in {"CASH", "USD"}
                })
                tickers = [x for x in tickers if is_equity_holding_ticker(x)]
                if len(tickers) >= 30:
                    return tickers, f"live:{url}"
        except Exception as exc:
            last_error = exc
    if last_error:
        print(f"[WARN] KOMP live universe unavailable: {last_error}; using fallback list.")
    return sorted(KOMP_FALLBACK), "fallback:bundled_komp"


def load_soxx_universe() -> tuple[list[str], str]:
    """Load SOXX semiconductor ETF holdings, with yfinance and bundled fallbacks."""
    last_error = None
    for url in CONFIG["soxx_source_urls"]:
        try:
            req = Request(url, headers={"User-Agent": "Vibe-Trading/0.1 options screener"})
            with urlopen(req, timeout=20) as resp:
                html_text = resp.read().decode("utf-8", errors="replace")
            tables = pd.read_html(StringIO(html_text))
            for table in tables:
                symbol_col = next(
                    (
                        c for c in table.columns
                        if str(c).strip().lower() in {"symbol", "ticker", "holding ticker", "asset", "holdings"}
                    ),
                    None,
                )
                if symbol_col is None:
                    continue
                raw = []
                for x in table[symbol_col].dropna().tolist():
                    token = str(x).strip().split()[0]
                    token = token.split(":")[-1].split(".")[0]
                    raw.append(token)
                tickers = sorted({
                    normalize_yfinance_symbol(x)
                    for x in raw
                    if x and x.lower() != "nan" and x.upper() not in {"CASH", "USD"}
                })
                tickers = [x for x in tickers if is_equity_holding_ticker(x)]
                if len(tickers) >= 20:
                    return tickers, f"live:{url}"
        except Exception as exc:
            last_error = exc
    try:
        top = yf.Ticker("SOXX").funds_data.top_holdings
        tickers = sorted({
            normalize_yfinance_symbol(str(x)) for x in top.index.tolist()
            if str(x).strip() and is_equity_holding_ticker(str(x))
        })
        if len(tickers) >= 8:
            return tickers, "live:yfinance:SOXX_top_holdings"
    except Exception as exc:
        last_error = exc
    if last_error:
        print(f"[WARN] SOXX live universe unavailable: {last_error}; using fallback list.")
    return sorted(SOXX_FALLBACK), "fallback:bundled_soxx"


def _table_tickers(tables: list[pd.DataFrame]) -> list[str]:
    """Extract equity tickers from common holdings-table layouts."""
    for table in tables:
        symbol_col = next(
            (
                c for c in table.columns
                if str(c).strip().lower() in {
                    "symbol", "ticker", "holding ticker", "asset", "holdings", "company ticker"
                }
            ),
            None,
        )
        if symbol_col is None:
            continue
        raw = []
        for value in table[symbol_col].dropna().tolist():
            token = str(value).strip().split()[0]
            token = token.split(":")[-1]
            raw.append(token)
        tickers = sorted({
            normalize_yfinance_symbol(x)
            for x in raw
            if x and x.lower() != "nan" and x.upper() not in {"CASH", "USD", "US DOLLAR"}
        })
        tickers = [x for x in tickers if is_equity_holding_ticker(x)]
        if tickers:
            return tickers
    return []


def load_registered_universe(key: str) -> tuple[list[str], str]:
    """Load a registry universe with a bundled offline research fallback."""
    spec = UNIVERSE_REGISTRY[key]
    last_error = None
    for url in spec["source_urls"]:
        try:
            req = Request(url, headers={"User-Agent": "Vibe-Trading/0.1 research universe archive"})
            with urlopen(req, timeout=20) as resp:
                html_text = resp.read().decode("utf-8", errors="replace")
            tickers = _table_tickers(pd.read_html(StringIO(html_text)))
            if len(tickers) >= int(spec["min_holdings"]):
                return tickers, f"live:{url}"
        except Exception as exc:
            last_error = exc
    fallback = sorted({normalize_yfinance_symbol(x) for x in spec["fallback"]})
    if last_error:
        print(f"[WARN] {spec['label']} live universe unavailable: {last_error}; using fallback list.")
    return fallback, f"fallback:bundled_{key}"


def universe_snapshot_root() -> Path:
    configured = str(CONFIG.get("universe_snapshot_dir", "")).strip()
    if configured:
        return Path(configured)
    if os.path.isdir("/app/agent"):
        return Path("/app/agent/data_cache/universe_snapshots")
    return Path(os.getcwd()) / "data_cache" / "universe_snapshots"


def archive_universe_snapshot(
    key: str,
    tickers: list[str],
    source: str,
    effective_date: Optional[str] = None,
) -> Path:
    """Archive the constituent list used by a live run for later as-of research."""
    snapshot_date = effective_date or date.today().isoformat()
    target_dir = universe_snapshot_root() / key
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{snapshot_date}.json"
    payload = {
        "universe": key,
        "effective_date": snapshot_date,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "ticker_count": len(tickers),
        "tickers": sorted(set(tickers)),
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def load_universe_snapshot(key: str, as_of: str) -> tuple[list[str], str] | None:
    """Read the latest archived constituents on or before an as-of date."""
    datetime.strptime(as_of, "%Y-%m-%d")
    target_dir = universe_snapshot_root() / key
    if not target_dir.exists():
        return None
    candidates = sorted(path for path in target_dir.glob("*.json") if path.stem <= as_of)
    if not candidates:
        return None
    selected = candidates[-1]
    payload = json.loads(selected.read_text(encoding="utf-8"))
    tickers = [
        normalize_yfinance_symbol(str(x)) for x in payload.get("tickers", [])
        if str(x).strip() and is_equity_holding_ticker(str(x))
    ]
    return sorted(set(tickers)), f"archive:{selected}"


def resolve_snapshot_aware_universe(
    key: str,
    label: str,
    loader,
    as_of: Optional[str] = None,
    archive_only: bool = False,
) -> tuple[list[str], str, dict[str, list[str]]]:
    """Resolve historical snapshots without silently substituting today's holdings."""
    if as_of:
        archived = load_universe_snapshot(key, as_of)
        if archived:
            tickers, source = archived
            return tickers, source, universe_memberships(tickers, label)
        if archive_only:
            raise ValueError(f"no archived snapshot for universe '{key}' on or before {as_of}")
    tickers, source = loader()
    snapshot_path = archive_universe_snapshot(key, tickers, source)
    return tickers, f"{source};snapshot:{snapshot_path}", universe_memberships(tickers, label)


def parse_int_field(value: object) -> int:
    try:
        return int(str(value).replace(",", "").replace("$", "").strip())
    except Exception:
        return 0


def is_common_equity_row(row: dict) -> bool:
    symbol = normalize_yfinance_symbol(str(row.get("symbol", "")))
    name = str(row.get("name", "")).lower()
    if not symbol or not symbol[0].isalpha() or len(symbol.replace("-", "")) > 6:
        return False
    bad_name_terms = [
        "warrant", "right", "unit", "preferred", "preference", "depositary share",
        "notes due", "bond", "debenture", "etf", "fund", "trust units",
    ]
    if any(term in name for term in bad_name_terms):
        return False
    if symbol.endswith(("W", "R", "U")) and any(term in name for term in ["warrant", "right", "unit"]):
        return False
    return True


def load_us_volume_top_universe(n: Optional[int] = None) -> tuple[list[str], str]:
    """Load the top-N US equities by latest reported NASDAQ screener volume."""
    top_n = int(n or CONFIG["us_volume_top_n"])
    url = CONFIG["us_volume_source_url"]
    try:
        req = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 Vibe-Trading/0.1",
                "Accept": "application/json,text/plain,*/*",
                "Origin": "https://www.nasdaq.com",
                "Referer": "https://www.nasdaq.com/",
            },
        )
        with urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        rows = ((payload.get("data") or {}).get("rows") or [])
        ranked = []
        for row in rows:
            if not isinstance(row, dict) or not is_common_equity_row(row):
                continue
            symbol = normalize_yfinance_symbol(str(row.get("symbol", "")))
            volume = parse_int_field(row.get("volume", 0))
            if volume <= 0:
                continue
            ranked.append((volume, symbol))
        ranked.sort(reverse=True)
        tickers = []
        seen = set()
        for _, symbol in ranked:
            if symbol in seen:
                continue
            seen.add(symbol)
            tickers.append(symbol)
            if len(tickers) >= top_n:
                break
        if len(tickers) >= min(top_n, 20):
            return tickers, f"live:nasdaq_volume_top{top_n}:{url}"
    except Exception as exc:
        print(f"[WARN] US volume top universe unavailable: {exc}; using fallback list.")
    return US_VOLUME_TOP_FALLBACK[:top_n], f"fallback:bundled_us_volume_top{top_n}"


def filter_optionable_tickers(tickers: list[str], n: int, scan_limit: int = 200) -> list[str]:
    optionable = []
    for symbol in tickers[:scan_limit]:
        try:
            if yf.Ticker(symbol).options:
                optionable.append(symbol)
        except Exception:
            continue
        if len(optionable) >= n:
            break
    return optionable


def load_us_volume_top_optionable_universe(n: Optional[int] = None) -> tuple[list[str], str]:
    top_n = int(n or CONFIG["us_volume_top_n"])
    broad, source = load_us_volume_top_universe(max(top_n * 4, 200))
    optionable = filter_optionable_tickers(broad, top_n, scan_limit=len(broad))
    if len(optionable) >= min(top_n, 20):
        return optionable[:top_n], f"{source};filter:optionable_top{top_n}"
    fallback = filter_optionable_tickers(US_VOLUME_TOP_FALLBACK, top_n, scan_limit=len(US_VOLUME_TOP_FALLBACK))
    return (fallback or US_VOLUME_TOP_FALLBACK[:top_n]), f"fallback:optionable_us_volume_top{top_n}"


def universe_memberships(tickers: list[str], label: str) -> dict[str, list[str]]:
    """Build a stable ticker-to-source-pools mapping for report attribution."""
    return {ticker: [label] for ticker in tickers}


def merge_universe_memberships(*parts: tuple[str, list[str]]) -> dict[str, list[str]]:
    """Merge source-pool memberships while preserving the requested pool order."""
    merged: dict[str, list[str]] = {}
    for label, tickers in parts:
        for ticker in tickers:
            merged.setdefault(ticker, [])
            if label not in merged[ticker]:
                merged[ticker].append(label)
    return merged


def resolve_universe(
    name: str,
    custom: Optional[str] = None,
    as_of: Optional[str] = None,
    archive_only: bool = False,
) -> tuple[list[str], str, dict[str, list[str]]]:
    if custom:
        tickers = sorted({normalize_yfinance_symbol(x) for x in custom.split(",") if x.strip()})
        return tickers, "custom", universe_memberships(tickers, "自定义")
    key = name.lower().replace("-", "_")
    supported_index_keys = {
        "spx", "sp500", "s&p500", "s&p_500", "spy",
        "ixic", "comp", "nasdaq_composite", "nasdaq", "oneq",
        "djia", "dji", "dow", "dow30",
        "ndx", "nasdaq100", "nasdaq_100", "qqq",
        "rut", "russell2000", "russell_2000", "iwm",
        "rua", "russell3000", "russell_3000", "iwv",
        "mid", "sp400", "s&p400", "s&p_midcap_400", "midcap400", "mdy",
        "sml", "sp600", "s&p600", "s&p_smallcap_600", "smallcap600", "ijr",
        "w5000", "wilshire5000", "wilshire_5000", "vti",
        "sox", "phlx_semiconductor",
    }
    if key not in supported_index_keys:
        raise ValueError(
            "unsupported universe: "
            f"{name}. Supported index universes: spx, ixic, djia, ndx, rut, rua, mid, sml, w5000, sox."
        )
    if key in {"speculative_peer_earnings", "peer_earnings", "peer_earnings_pool", "pre_earnings_revision", "pre_earnings"}:
        import sys
        agent_dir = Path(__file__).resolve().parent.parent
        if str(agent_dir) not in sys.path:
            sys.path.insert(0, str(agent_dir))
        from peer_earnings_signal_service import peer_group_symbols

        tickers = peer_group_symbols("all")
        label = "财报前预期修正池" if "pre_earnings" in key else "投机-同行财报接力池"
        return tickers, "curated:peer_similarity_catalog", universe_memberships(tickers, label)
    if key in {"us_volume_top50_raw", "volume_top50_raw", "most_active_raw", "us_most_active_raw"}:
        tickers, source = load_us_volume_top_universe(50)
        return tickers, source, universe_memberships(tickers, "成交量 Top50")
    if key.startswith("us_volume_top") and key.endswith("_raw"):
        try:
            n = int(key.replace("us_volume_top", "").replace("_raw", ""))
            tickers, source = load_us_volume_top_universe(n)
            return tickers, source, universe_memberships(tickers, f"成交量 Top{n}")
        except Exception:
            pass
    if key in {"us_volume_top50", "volume_top50", "most_active", "us_most_active"}:
        tickers, source = load_us_volume_top_optionable_universe(50)
        return tickers, source, universe_memberships(tickers, "成交量 Top50")
    if key in {"us_volume_top50_optionable", "volume_top50_optionable", "most_active_optionable"}:
        tickers, source = load_us_volume_top_optionable_universe(50)
        return tickers, source, universe_memberships(tickers, "成交量 Top50")
    if key.startswith("us_volume_top") and key.endswith("_optionable"):
        try:
            n = int(key.replace("us_volume_top", "").replace("_optionable", ""))
            tickers, source = load_us_volume_top_optionable_universe(n)
            return tickers, source, universe_memberships(tickers, f"成交量 Top{n}")
        except Exception:
            pass
    if key.startswith("us_volume_top"):
        try:
            n = int(key.replace("us_volume_top", ""))
            tickers, source = load_us_volume_top_optionable_universe(n)
            return tickers, source, universe_memberships(tickers, f"成交量 Top{n}")
        except Exception:
            pass
    if key in {"sp100", "s&p100", "s&p_100"}:
        return resolve_snapshot_aware_universe("sp100", "S&P 100", load_sp100_universe, as_of, archive_only)
    if key in {"komp", "spdr_komp"}:
        return resolve_snapshot_aware_universe("komp", "KOMP", load_komp_universe, as_of, archive_only)
    if key in {"soxx", "ishares_soxx"}:
        return resolve_snapshot_aware_universe("soxx", "SOXX", load_soxx_universe, as_of, archive_only)
    registered_key = next(
        (
            registry_key for registry_key, spec in UNIVERSE_REGISTRY.items()
            if key == registry_key or key in spec.get("aliases", set())
        ),
        None,
    )
    if registered_key:
        spec = UNIVERSE_REGISTRY[registered_key]
        return resolve_snapshot_aware_universe(
            registered_key,
            str(spec["label"]),
            lambda: load_registered_universe(registered_key),
            as_of,
            archive_only,
        )
    if key in {"sp100_komp", "sp100+komp", "s&p100_komp", "s&p100+komp"}:
        sp100, sp100_source, _ = resolve_universe("sp100", as_of=as_of, archive_only=archive_only)
        komp, komp_source, _ = resolve_universe("komp", as_of=as_of, archive_only=archive_only)
        tickers = sorted(set(sp100) | set(komp))
        memberships = merge_universe_memberships(("S&P 100", sp100), ("KOMP", komp))
        return tickers, f"{sp100_source};{komp_source}", memberships
    if key in {"sp100_komp_soxx", "sp100+komp+soxx", "s&p100_komp_soxx", "s&p100+komp+soxx"}:
        sp100, sp100_source, _ = resolve_universe("sp100", as_of=as_of, archive_only=archive_only)
        komp, komp_source, _ = resolve_universe("komp", as_of=as_of, archive_only=archive_only)
        soxx, soxx_source, _ = resolve_universe("soxx", as_of=as_of, archive_only=archive_only)
        tickers = sorted(set(sp100) | set(komp) | set(soxx))
        memberships = merge_universe_memberships(("S&P 100", sp100), ("KOMP", komp), ("SOXX", soxx))
        return tickers, f"{sp100_source};{komp_source};{soxx_source}", memberships
    raise ValueError(f"unsupported universe: {name}")

STRATEGY_ROUTING = {
    ("expensive", "strong_up"): "bull_put_spread",
    ("expensive", "up"): "bull_put_spread",
    ("expensive", "range"): "iron_condor",
    ("expensive", "down"): "bear_call_spread",
    ("expensive", "strong_down"): "bear_call_spread",
    ("fair", "strong_up"): "bull_put_spread",
    ("fair", "up"): "bull_put_spread",
    ("fair", "range"): "condor",
    ("fair", "down"): "bear_call_spread",
    ("fair", "strong_down"): "bear_call_spread",
    ("cheap", "strong_up"): "bull_call_spread",
    ("cheap", "up"): "single_call",
    ("cheap", "range"): "long_straddle",
    ("cheap", "down"): "single_put",
    ("cheap", "strong_down"): "bear_put_spread",
}

SUPPORTED_STRATEGY_FAMILIES = ["Single", "Spread", "Straddle", "Roll Position", "Butterfly", "Condor", "Iron Condor"]


def strategy_family(strategy: str) -> str:
    if strategy in {"single_call", "single_put", "long_call", "long_put"}:
        return "Single"
    if strategy in {"bull_put_spread", "bear_call_spread", "bull_call_spread", "bear_put_spread"}:
        return "Spread"
    if strategy in {"long_straddle", "long_strangle"}:
        return "Straddle"
    if strategy in {"butterfly", "call_butterfly", "put_butterfly"}:
        return "Butterfly"
    if strategy == "condor":
        return "Condor"
    if strategy == "iron_condor":
        return "Iron Condor"
    if strategy == "roll_position":
        return "Roll Position"
    return "Spread"


def forced_strategy_for_family(family: str, trend_class: str) -> Optional[str]:
    family = family.strip().lower().replace("_", " ")
    if family in {"", "auto"}:
        return None
    if family == "single":
        return "single_put" if trend_class in {"down", "strong_down"} else "single_call"
    if family == "spread":
        return "bear_call_spread" if trend_class in {"down", "strong_down"} else "bull_call_spread"
    if family == "straddle":
        return "long_straddle"
    if family == "butterfly":
        return "butterfly"
    if family == "condor":
        return "condor"
    if family == "iron condor":
        return "iron_condor"
    if family == "roll position":
        return None
    raise ValueError(f"unsupported strategy family: {family}")


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / math.sqrt(2.0)))


def compute_hv(close_prices: np.ndarray, window: int = 20) -> float:
    if len(close_prices) < window + 1:
        return 0.0
    rets = np.diff(np.log(close_prices[-(window + 1):]))
    return float(np.std(rets, ddof=1) * np.sqrt(252))


def classify_iv_hv(iv: float, hv: float) -> str:
    if hv <= 0:
        return "expensive"
    ratio = iv / hv
    if ratio >= CONFIG["iv_hv_expensive"]:
        return "expensive"
    if ratio <= CONFIG["iv_hv_cheap"]:
        return "cheap"
    return "fair"


def classify_trend(change_30d: float) -> str:
    if change_30d >= 0.15:
        return "strong_up"
    if change_30d >= 0.05:
        return "up"
    if change_30d <= -0.15:
        return "strong_down"
    if change_30d <= -0.05:
        return "down"
    return "range"


def bs_price(spot: float, strike: float, t: float, r: float, sigma: float, option_type: str) -> float:
    if t <= 0 or sigma <= 0:
        return max(0.0, spot - strike) if option_type == "call" else max(0.0, strike - spot)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    if option_type == "call":
        return spot * norm.cdf(d1) - strike * math.exp(-r * t) * norm.cdf(d2)
    return strike * math.exp(-r * t) * norm.cdf(-d2) - spot * norm.cdf(-d1)


def implied_vol(mid: float, spot: float, strike: float, t: float, r: float, option_type: str) -> float:
    if mid <= 0 or t <= 0:
        return 0.0
    intrinsic = max(0.0, spot - strike) if option_type == "call" else max(0.0, strike - spot)
    if mid <= intrinsic:
        return 0.01
    sigma = max(0.05, min(math.sqrt(2 * math.pi / t) * mid / spot, 5.0))
    for _ in range(50):
        d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
        vega = spot * math.sqrt(t) * norm.pdf(d1)
        if vega < 1e-8:
            break
        diff = bs_price(spot, strike, t, r, sigma, option_type) - mid
        if abs(diff) < 1e-5:
            return float(sigma)
        sigma = max(0.005, min(sigma - diff / vega, 5.0))
    return float(sigma)


def mid(row: pd.Series) -> float:
    bid = float(row.get("bid", 0) or 0)
    ask = float(row.get("ask", 0) or 0)
    last = float(row.get("lastPrice", 0) or 0)
    if bid > 0 and ask > 0 and ask >= bid:
        return (bid + ask) / 2
    return last if last > 0 else max(bid, ask, 0.0)


def row_iv(row: pd.Series, fallback_iv: float) -> float:
    iv = float(row.get("impliedVolatility", 0) or 0)
    if 0.01 <= iv <= 5.0:
        return iv
    return max(float(fallback_iv or 0), 0.05)


def theoretical_option_price(row: pd.Series, spot: float, iv: float, dte: int, option_type: str) -> float:
    strike = float(row["strike"])
    return bs_price(
        spot,
        strike,
        max(dte, 1) / 365.25,
        CONFIG["risk_free_rate"],
        row_iv(row, iv),
        option_type,
    )


def bs_gamma(spot: float, strike: float, t: float, r: float, sigma: float) -> float:
    if spot <= 0 or strike <= 0 or t <= 0 or sigma <= 0:
        return 0.0
    try:
        d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
        return float(norm.pdf(d1) / (spot * sigma * math.sqrt(t)))
    except Exception:
        return 0.0


def option_gex_row(row: pd.Series, spot: float, fallback_iv: float, dte: int, option_type: str) -> float:
    """Estimate dealer gamma exposure from one option row using OI.

    Sign convention is the common street approximation: customer long calls /
    short puts imply dealer short call gamma and long put gamma. Real dealer
    positioning is not observable from yfinance, so this is an OI proxy.
    """
    oi = max(float(row.get("openInterest", 0) or 0), 0.0)
    if oi <= 0:
        return 0.0
    strike = float(row.get("strike", 0) or 0)
    sigma = row_iv(row, fallback_iv)
    gamma = bs_gamma(spot, strike, max(dte, 1) / 365.25, CONFIG["risk_free_rate"], sigma)
    sign = 1.0 if option_type == "put" else -1.0
    return sign * gamma * oi * float(CONFIG["gex_contract_multiplier"]) * spot * spot / 100.0


def estimate_net_gex_at_price(calls: pd.DataFrame, puts: pd.DataFrame, price: float, fallback_iv: float, dte: int) -> float:
    total = 0.0
    for _, row in calls.iterrows():
        total += option_gex_row(row, price, fallback_iv, dte, "call")
    for _, row in puts.iterrows():
        total += option_gex_row(row, price, fallback_iv, dte, "put")
    return float(total)


def estimate_gamma_flip(calls: pd.DataFrame, puts: pd.DataFrame, spot: float, fallback_iv: float, dte: int) -> Optional[float]:
    pct = float(CONFIG.get("gex_flip_scan_pct", 0.20) or 0.20)
    grid = np.linspace(max(spot * (1.0 - pct), 0.01), spot * (1.0 + pct), 41)
    values = [estimate_net_gex_at_price(calls, puts, float(price), fallback_iv, dte) for price in grid]
    for idx in range(1, len(grid)):
        prev, curr = values[idx - 1], values[idx]
        if prev == 0:
            return round(float(grid[idx - 1]), 2)
        if (prev < 0 < curr) or (prev > 0 > curr):
            weight = abs(prev) / max(abs(prev) + abs(curr), 1e-9)
            return round(float(grid[idx - 1] + (grid[idx] - grid[idx - 1]) * weight), 2)
    return None


def gamma_exposure_summary(calls: pd.DataFrame, puts: pd.DataFrame, spot: float, fallback_iv: float, dte: int) -> dict:
    if not CONFIG.get("gex_enabled", True):
        return {"available": False, "reason": "disabled"}
    call_gex = sum(option_gex_row(row, spot, fallback_iv, dte, "call") for _, row in calls.iterrows())
    put_gex = sum(option_gex_row(row, spot, fallback_iv, dte, "put") for _, row in puts.iterrows())
    net_gex = float(call_gex + put_gex)
    total_abs = float(abs(call_gex) + abs(put_gex))
    gex_pct = net_gex / max(spot * float(CONFIG["gex_contract_multiplier"]), 1.0)
    abs_pct = abs(net_gex) / max(total_abs, 1.0)
    flip = estimate_gamma_flip(calls, puts, spot, fallback_iv, dte)
    distance_to_flip = None if flip is None or spot <= 0 else (spot - flip) / spot
    if net_gex < 0 and abs_pct >= float(CONFIG["gex_high_abs_pct"]):
        regime = "negative_high"
        risk_level = "high"
        interpretation = "负 Gamma 偏高：价格越跌越容易触发被动卖出/追空，对短约和卖方策略不友好。"
    elif net_gex < 0:
        regime = "negative"
        risk_level = "medium"
        interpretation = "负 Gamma：波动可能被放大，需要降低仓位或等待确认。"
    elif abs_pct <= float(CONFIG["gex_low_abs_pct"]):
        regime = "neutral"
        risk_level = "medium"
        interpretation = "Gamma 接近中性：方向判断优先，结构性缓冲有限。"
    else:
        regime = "positive"
        risk_level = "low"
        interpretation = "正 Gamma：做市商再平衡更可能抑制波动，有利于区间/收租类研究假设。"
    return {
        "available": True,
        "source": "estimated_from_option_chain_oi",
        "net_gex": round(net_gex, 2),
        "call_gex": round(float(call_gex), 2),
        "put_gex": round(float(put_gex), 2),
        "gex_pct_of_100_shares": round(gex_pct, 4),
        "abs_imbalance": round(abs_pct, 4),
        "gamma_flip": flip,
        "distance_to_flip": None if distance_to_flip is None else round(distance_to_flip, 4),
        "regime": regime,
        "risk_level": risk_level,
        "interpretation_cn": interpretation,
        "limitations": "OI proxy only; yfinance does not expose dealer positioning, intraday GEX, or customer trade direction.",
    }


def executable_price(
    row: pd.Series,
    action: str,
    option_type: str,
    spot: float,
    iv: float,
    dte: int,
) -> tuple[float, str]:
    """Return a conservative leg price and its quote source.

    yfinance often returns real strikes with stale or zero bid/ask. For order
    recommendations we require an actual bid for sells and ask for buys; stale
    lastPrice/theoretical fallbacks are research-only and must not be treated as
    tradable.
    """
    bid = float(row.get("bid", 0) or 0)
    ask = float(row.get("ask", 0) or 0)
    last = float(row.get("lastPrice", 0) or 0)
    m = mid(row)
    if action == "sell":
        if bid > 0:
            return bid, "bid"
        if not CONFIG.get("allow_estimated_quotes", False):
            return float("nan"), "missing_bid"
        fallback = m if m > 0 else theoretical_option_price(row, spot, iv, dte, option_type)
        return max(fallback * CONFIG["sell_fallback_haircut"], 0.01), "estimated"
    if ask > 0:
        return ask, "ask"
    if not CONFIG.get("allow_estimated_quotes", False):
        return float("nan"), "missing_ask"
    fallback = m if m > 0 else theoretical_option_price(row, spot, iv, dte, option_type)
    return max(fallback * CONFIG["buy_fallback_markup"], 0.01), "estimated"


def quote_quality(*sources: str) -> str:
    if all(src in {"bid", "ask"} for src in sources):
        return "executable_bid_ask"
    if any(src.startswith("missing_") for src in sources):
        return "missing_executable_quote"
    if any(src == "estimated" for src in sources):
        return "estimated_from_yfinance"
    return "mixed_yfinance"


def valid_leg_prices(*prices: float) -> bool:
    return all(np.isfinite(float(p)) and float(p) > 0 for p in prices)


def compute_atm_iv(spot: float, calls: pd.DataFrame, puts: pd.DataFrame, dte: int) -> float:
    t = max(dte, 1) / 365.25
    values = []
    for frame, option_type in [(calls, "call"), (puts, "put")]:
        if frame.empty:
            continue
        rows = frame.iloc[(frame["strike"] - spot).abs().argsort()[:3]]
        for _, row in rows.iterrows():
            m = mid(row)
            if m > 0:
                iv = implied_vol(m, spot, float(row["strike"]), t, CONFIG["risk_free_rate"], option_type)
                if 0.01 <= iv <= 5.0:
                    values.append(iv)
    return float(np.mean(values)) if values else 0.0


def probability_above(spot: float, strike: float, dte: int, iv: float) -> float:
    t = max(dte, 1) / 365.0
    sigma_t = max(iv, 0.001) * math.sqrt(t)
    d2 = (math.log(spot / strike) - 0.5 * sigma_t * sigma_t) / sigma_t
    return float(norm_cdf(d2))


def probability_between(spot: float, lower: float, upper: float, dte: int, iv: float) -> float:
    return probability_above(spot, lower, dte, iv) - probability_above(spot, upper, dte, iv)


def ticker_theme(ticker: str) -> str:
    for theme, members in THEME_MAP.items():
        if ticker in members:
            return theme
    return "other"


def historical_barrier_win_rate(close: np.ndarray, strategy: str, spot: float, result: dict, dte: int) -> float:
    """Underlying-only historical validation for the same relative barrier setup.

    This is not a full option backtest because historical option chains are not
    available from yfinance. It answers: in prior windows, how often did the
    underlying finish on the profitable side of the current relative strikes?
    """
    if len(close) < dte + 45:
        return float("nan")
    wins = 0
    total = 0
    for i in range(30, len(close) - dte):
        entry = float(close[i])
        exit_price = float(close[i + dte])
        if entry <= 0:
            continue
        if strategy == "bull_put_spread":
            barrier = entry * float(result["short_strike"]) / spot
            win = exit_price > barrier
        elif strategy == "bear_call_spread":
            barrier = entry * float(result["short_strike"]) / spot
            win = exit_price < barrier
        elif strategy == "iron_condor":
            lower = entry * float(result["put_short"]) / spot
            upper = entry * float(result["call_short"]) / spot
            win = lower < exit_price < upper
        elif strategy in {"bull_call_spread", "long_call"}:
            barrier = entry * float(result["breakeven"]) / spot
            win = exit_price > barrier
        elif strategy in {"bear_put_spread", "long_put"}:
            barrier = entry * float(result["breakeven"]) / spot
            win = exit_price < barrier
        else:
            continue
        wins += int(win)
        total += 1
    return round(wins / total, 4) if total else float("nan")


def leg_liquidity(row: pd.Series) -> float:
    oi = float(row.get("openInterest", 0) or 0)
    vol = float(row.get("volume", 0) or 0)
    m = mid(row)
    bid = float(row.get("bid", 0) or 0)
    ask = float(row.get("ask", 0) or 0)
    spread = ((ask - bid) / m) if bid > 0 and ask > 0 and ask >= bid and m > 0 else 0.75
    oi_score = min(oi / 1000.0, 1.0)
    vol_score = min(vol / 300.0, 1.0)
    spread_score = max(0.0, 1.0 - spread / 0.35)
    return 0.45 * oi_score + 0.25 * vol_score + 0.30 * spread_score


def legs_liquidity(*legs: pd.Series) -> float:
    return float(min(leg_liquidity(leg) for leg in legs)) if legs else 0.0


def leg_spread_pct(row: pd.Series) -> float:
    bid = float(row.get("bid", 0) or 0)
    ask = float(row.get("ask", 0) or 0)
    m = (bid + ask) / 2 if bid > 0 and ask > 0 and ask >= bid else 0.0
    if m <= 0:
        return 1.0
    return (ask - bid) / m


def leg_passes_liquidity(row: pd.Series) -> tuple[bool, str]:
    oi = float(row.get("openInterest", 0) or 0)
    vol = float(row.get("volume", 0) or 0)
    spread_pct = leg_spread_pct(row)
    liq = leg_liquidity(row)
    if vol < float(CONFIG["min_option_leg_volume"]):
        return False, f"volume {vol:.0f} < {CONFIG['min_option_leg_volume']}"
    if oi < float(CONFIG["min_option_leg_open_interest"]):
        return False, f"openInterest {oi:.0f} < {CONFIG['min_option_leg_open_interest']}"
    if spread_pct > float(CONFIG["max_option_bid_ask_spread_pct"]):
        return False, f"bid/ask spread {spread_pct*100:.1f}% > {CONFIG['max_option_bid_ask_spread_pct']*100:.0f}%"
    if liq < float(CONFIG["min_trade_liquidity_score"]):
        return False, f"liquidity_score {liq:.2f} < {CONFIG['min_trade_liquidity_score']}"
    return True, "ok"


def legs_pass_liquidity(*legs: pd.Series) -> tuple[bool, list[str]]:
    reasons = []
    for idx, leg in enumerate(legs, start=1):
        ok, reason = leg_passes_liquidity(leg)
        if not ok:
            reasons.append(f"leg{idx}: {reason}")
    return not reasons, reasons


def liquidity_grade(score: float) -> str:
    if score >= 0.75:
        return "excellent"
    if score >= 0.55:
        return "good"
    if score >= float(CONFIG["min_trade_liquidity_score"]):
        return "acceptable"
    return "thin"


def nearest(frame: pd.DataFrame, target: float, above: Optional[bool] = None) -> Optional[pd.Series]:
    if frame.empty:
        return None
    candidates = frame
    if above is True:
        candidates = candidates[candidates["strike"] >= target]
    elif above is False:
        candidates = candidates[candidates["strike"] <= target]
    if candidates.empty:
        candidates = frame
    return candidates.loc[(candidates["strike"] - target).abs().idxmin()]


def candidate_rows(frame: pd.DataFrame, target: float, above: Optional[bool], n: int = 5) -> list[pd.Series]:
    if frame.empty:
        return []
    candidates = frame
    if above is True:
        candidates = candidates[candidates["strike"] >= target]
    elif above is False:
        candidates = candidates[candidates["strike"] <= target]
    if candidates.empty:
        candidates = frame
    ranked = candidates.assign(_dist=(candidates["strike"] - target).abs()).sort_values("_dist").head(n)
    return [row for _, row in ranked.iterrows()]


def choose_credit_spread(
    side: pd.DataFrame,
    spot: float,
    iv: float,
    dte: int,
    strategy: str,
) -> Optional[dict]:
    is_put = strategy == "bull_put_spread"
    target = target_put_strike(spot, iv, dte, 0.25) if is_put else target_call_strike(spot, iv, dte, 0.25)
    short_rows = candidate_rows(side, target, above=False if is_put else True, n=7)
    best = None
    for short in short_rows:
        short_strike = float(short["strike"])
        long_side = side[side["strike"] < short_strike] if is_put else side[side["strike"] > short_strike]
        if long_side.empty:
            continue
        long_target = short_strike * (0.90 if is_put else 1.10)
        for long in candidate_rows(long_side, long_target, above=False if is_put else True, n=5):
            long_strike = float(long["strike"])
            width = abs(short_strike - long_strike)
            if width <= 0:
                continue
            short_px, short_src = executable_price(short, "sell", "put" if is_put else "call", spot, iv, dte)
            long_px, long_src = executable_price(long, "buy", "put" if is_put else "call", spot, iv, dte)
            if not valid_leg_prices(short_px, long_px):
                continue
            liquid, _liquidity_reasons = legs_pass_liquidity(short, long)
            if not liquid:
                continue
            credit = short_px - long_px
            if credit <= 0:
                continue
            pop = probability_above(spot, short_strike, dte, iv) if is_put else 1.0 - probability_above(spot, short_strike, dte, iv)
            max_profit = credit * 100
            max_loss = (width - credit) * 100
            if max_loss <= 0:
                continue
            commission = cost_model.option_trade_cost(2)
            ev_gross = pop * max_profit - (1 - pop) * max_loss
            ev = ev_gross - commission
            roc = (max_profit - commission) / max_loss
            liq = legs_liquidity(short, long)
            rank = (ev, roc, liq, pop)
            if best is None or rank > best["rank"]:
                best = {
                    "short": short,
                    "long": long,
                    "credit": credit,
                    "width": width,
                    "pop": pop,
                    "roc": roc,
                    "expected_value": ev,
                    "expected_value_gross": ev_gross,
                    "trade_cost": commission,
                    "liquidity_score": liq,
                    "quote_quality": quote_quality(short_src, long_src),
                    "quote_sources": {"short": short_src, "long": long_src},
                    "rank": rank,
                }
    return best


def choose_debit_spread(
    side: pd.DataFrame,
    spot: float,
    iv: float,
    dte: int,
    strategy: str,
) -> Optional[dict]:
    is_call = strategy in {"bull_call_spread", "long_call"}
    long_rows = candidate_rows(side, spot * (0.98 if is_call else 1.02), above=None, n=6)
    best = None
    for long in long_rows:
        long_strike = float(long["strike"])
        short_side = side[side["strike"] > long_strike] if is_call else side[side["strike"] < long_strike]
        if short_side.empty:
            continue
        target = long_strike * (1.12 if is_call else 0.88)
        for short in candidate_rows(short_side, target, above=True if is_call else False, n=5):
            short_strike = float(short["strike"])
            width = abs(short_strike - long_strike)
            long_px, long_src = executable_price(long, "buy", "call" if is_call else "put", spot, iv, dte)
            short_px, short_src = executable_price(short, "sell", "call" if is_call else "put", spot, iv, dte)
            if not valid_leg_prices(long_px, short_px):
                continue
            liquid, _liquidity_reasons = legs_pass_liquidity(long, short)
            if not liquid:
                continue
            debit = long_px - short_px
            if debit <= 0 or width <= debit:
                continue
            breakeven = long_strike + debit if is_call else long_strike - debit
            pop = probability_above(spot, breakeven, dte, iv) if is_call else 1.0 - probability_above(spot, breakeven, dte, iv)
            max_profit = (width - debit) * 100
            max_loss = debit * 100
            commission = cost_model.option_trade_cost(2)
            ev_gross = pop * max_profit - (1 - pop) * max_loss
            ev = ev_gross - commission
            roc = (max_profit - commission) / max_loss
            liq = legs_liquidity(long, short)
            rank = (ev, roc, liq, pop)
            if best is None or rank > best["rank"]:
                best = {
                    "long": long,
                    "short": short,
                    "debit": debit,
                    "width": width,
                    "breakeven": breakeven,
                    "pop": pop,
                    "roc": roc,
                    "expected_value": ev,
                    "expected_value_gross": ev_gross,
                    "trade_cost": commission,
                    "liquidity_score": liq,
                    "quote_quality": quote_quality(long_src, short_src),
                    "quote_sources": {"long": long_src, "short": short_src},
                    "rank": rank,
                }
    return best


def choose_single(frame: pd.DataFrame, spot: float, iv: float, dte: int, option_type: str) -> Optional[dict]:
    target = spot * (1.02 if option_type == "call" else 0.98)
    row = nearest(frame, target, above=True if option_type == "call" else False)
    if row is None:
        return None
    debit, src = executable_price(row, "buy", option_type, spot, iv, dte)
    if not valid_leg_prices(debit):
        return None
    liquid, _liquidity_reasons = legs_pass_liquidity(row)
    if not liquid:
        return None
    strike = float(row["strike"])
    breakeven = strike + debit if option_type == "call" else strike - debit
    pop = probability_above(spot, breakeven, dte, iv) if option_type == "call" else 1.0 - probability_above(spot, breakeven, dte, iv)
    expected_move = spot * max(iv, 0.001) * math.sqrt(max(dte, 1) / 365.0)
    target_price = spot + expected_move if option_type == "call" else spot - expected_move
    intrinsic = max(0.0, target_price - strike) if option_type == "call" else max(0.0, strike - target_price)
    max_profit_est = max(intrinsic - debit, 0.0) * 100
    max_loss = debit * 100
    commission = cost_model.option_trade_cost(1)
    ev_gross = pop * max_profit_est - (1 - pop) * max_loss
    return {
        "long": row,
        "debit": debit,
        "breakeven": breakeven,
        "pop": pop,
        "max_profit_est": max_profit_est,
        "max_loss": max_loss,
        "expected_value": ev_gross - commission,
        "expected_value_gross": ev_gross,
        "trade_cost": commission,
        "liquidity_score": leg_liquidity(row),
        "quote_quality": quote_quality(src),
        "quote_sources": {"long": src},
    }


def choose_long_straddle(calls: pd.DataFrame, puts: pd.DataFrame, spot: float, iv: float, dte: int) -> Optional[dict]:
    call = nearest(calls, spot, above=None)
    put = nearest(puts, spot, above=None)
    if call is None or put is None:
        return None
    call_px, call_src = executable_price(call, "buy", "call", spot, iv, dte)
    put_px, put_src = executable_price(put, "buy", "put", spot, iv, dte)
    if not valid_leg_prices(call_px, put_px):
        return None
    liquid, _liquidity_reasons = legs_pass_liquidity(call, put)
    if not liquid:
        return None
    debit = call_px + put_px
    strike = (float(call["strike"]) + float(put["strike"])) / 2
    lower = strike - debit
    upper = strike + debit
    pop = max(0.0, 1.0 - probability_between(spot, lower, upper, dte, iv))
    expected_move = spot * max(iv, 0.001) * math.sqrt(max(dte, 1) / 365.0)
    max_profit_est = max(expected_move - debit, 0.0) * 100
    max_loss = debit * 100
    commission = cost_model.option_trade_cost(2)
    ev_gross = pop * max_profit_est - (1 - pop) * max_loss
    return {
        "call": call, "put": put, "debit": debit, "lower_breakeven": lower, "upper_breakeven": upper,
        "pop": pop, "max_profit_est": max_profit_est, "max_loss": max_loss,
        "expected_value": ev_gross - commission,
        "expected_value_gross": ev_gross,
        "trade_cost": commission,
        "liquidity_score": legs_liquidity(call, put),
        "quote_quality": quote_quality(call_src, put_src),
        "quote_sources": {"call": call_src, "put": put_src},
    }


def choose_call_butterfly(calls: pd.DataFrame, spot: float, iv: float, dte: int) -> Optional[dict]:
    body = nearest(calls, spot, above=None)
    if body is None:
        return None
    body_strike = float(body["strike"])
    wing = max(spot * max(iv, 0.001) * math.sqrt(max(dte, 1) / 365.0) * 0.55, spot * 0.03)
    lower_side = calls[calls["strike"] < body_strike]
    upper_side = calls[calls["strike"] > body_strike]
    lower = nearest(lower_side, body_strike - wing, above=False)
    upper = nearest(upper_side, body_strike + wing, above=True)
    if lower is None or upper is None:
        return None
    lower_px, lower_src = executable_price(lower, "buy", "call", spot, iv, dte)
    body_px, body_src = executable_price(body, "sell", "call", spot, iv, dte)
    upper_px, upper_src = executable_price(upper, "buy", "call", spot, iv, dte)
    if not valid_leg_prices(lower_px, body_px, upper_px):
        return None
    liquid, _liquidity_reasons = legs_pass_liquidity(lower, body, upper)
    if not liquid:
        return None
    debit = lower_px + upper_px - 2 * body_px
    width = min(body_strike - float(lower["strike"]), float(upper["strike"]) - body_strike)
    if debit <= 0 or width <= debit:
        return None
    lower_be = float(lower["strike"]) + debit
    upper_be = float(upper["strike"]) - debit
    pop = probability_between(spot, lower_be, upper_be, dte, iv)
    max_profit = (width - debit) * 100
    max_loss = debit * 100
    commission = cost_model.option_trade_cost(4)
    ev_gross = pop * max_profit - (1 - pop) * max_loss
    return {
        "lower": lower, "body": body, "upper": upper, "debit": debit, "width": width,
        "lower_breakeven": lower_be, "upper_breakeven": upper_be, "pop": pop,
        "max_profit": max_profit, "max_loss": max_loss,
        "expected_value": ev_gross - commission,
        "expected_value_gross": ev_gross,
        "trade_cost": commission,
        "liquidity_score": legs_liquidity(lower, body, upper),
        "quote_quality": quote_quality(lower_src, body_src, upper_src),
        "quote_sources": {"lower": lower_src, "body": body_src, "upper": upper_src},
    }


def choose_call_condor(calls: pd.DataFrame, spot: float, iv: float, dte: int) -> Optional[dict]:
    spread1 = choose_debit_spread(calls, spot * 0.98, iv, dte, "bull_call_spread")
    if not spread1:
        return None
    lower = spread1["long"]
    inner_low = spread1["short"]
    inner_high_side = calls[calls["strike"] > float(inner_low["strike"])]
    inner_high = nearest(inner_high_side, spot * 1.04, above=True)
    if inner_high is None:
        return None
    outer_side = calls[calls["strike"] > float(inner_high["strike"])]
    upper = nearest(outer_side, float(inner_high["strike"]) * 1.04, above=True)
    if upper is None:
        return None
    lower_px, lower_src = executable_price(lower, "buy", "call", spot, iv, dte)
    il_px, il_src = executable_price(inner_low, "sell", "call", spot, iv, dte)
    ih_px, ih_src = executable_price(inner_high, "sell", "call", spot, iv, dte)
    upper_px, upper_src = executable_price(upper, "buy", "call", spot, iv, dte)
    if not valid_leg_prices(lower_px, il_px, ih_px, upper_px):
        return None
    liquid, _liquidity_reasons = legs_pass_liquidity(lower, inner_low, inner_high, upper)
    if not liquid:
        return None
    debit = lower_px + upper_px - il_px - ih_px
    width = min(float(inner_low["strike"]) - float(lower["strike"]), float(upper["strike"]) - float(inner_high["strike"]))
    if debit <= 0 or width <= debit:
        return None
    lower_be = float(lower["strike"]) + debit
    upper_be = float(upper["strike"]) - debit
    pop = probability_between(spot, lower_be, upper_be, dte, iv)
    max_profit = (width - debit) * 100
    max_loss = debit * 100
    commission = cost_model.option_trade_cost(4)
    ev_gross = pop * max_profit - (1 - pop) * max_loss
    return {
        "lower": lower, "inner_low": inner_low, "inner_high": inner_high, "upper": upper,
        "debit": debit, "width": width, "lower_breakeven": lower_be, "upper_breakeven": upper_be,
        "pop": pop, "max_profit": max_profit, "max_loss": max_loss,
        "expected_value": ev_gross - commission,
        "expected_value_gross": ev_gross,
        "trade_cost": commission,
        "liquidity_score": legs_liquidity(lower, inner_low, inner_high, upper),
        "quote_quality": quote_quality(lower_src, il_src, ih_src, upper_src),
        "quote_sources": {"lower": lower_src, "inner_low": il_src, "inner_high": ih_src, "upper": upper_src},
    }


def target_put_strike(spot: float, iv: float, dte: int, delta_abs: float) -> float:
    sigma_t = max(iv, 0.001) * math.sqrt(max(dte, 1) / 365.0)
    d1 = norm.ppf(1 - delta_abs)
    return spot * math.exp(-(d1 * sigma_t - 0.5 * sigma_t * sigma_t))


def target_call_strike(spot: float, iv: float, dte: int, delta_abs: float) -> float:
    sigma_t = max(iv, 0.001) * math.sqrt(max(dte, 1) / 365.0)
    d1 = norm.ppf(delta_abs)
    return spot * math.exp(-(d1 * sigma_t - 0.5 * sigma_t * sigma_t))


def credit_metrics(result: dict, credit: float, width: float, breakeven: float, pop: float, trade_cost: Optional[float] = None) -> None:
    max_profit = max(credit, 0.0) * 100
    max_loss = max(width - credit, 0.0) * 100
    commission = float(result.get("trade_cost", 0.0) if trade_cost is None else trade_cost)
    ev_gross = pop * max_profit - (1 - pop) * max_loss
    result.update({
        "credit": round(max(credit, 0.0), 2),
        "debit": 0.0,
        "max_profit": round(max_profit, 2),
        "max_loss": round(max_loss, 2),
        "breakeven": round(breakeven, 2),
        "roc": round((max_profit - commission) / max(max_loss, 1e-9), 4),
        "expected_value_gross": round(ev_gross, 2),
        "trade_cost": round(max(commission, 0.0), 2),
        "expected_value": round(ev_gross - commission, 2),
    })


def debit_metrics(result: dict, debit: float, width: float, breakeven: float, pop: float, trade_cost: Optional[float] = None) -> None:
    max_loss = max(debit, 0.0) * 100
    max_profit = max(width - debit, 0.0) * 100
    commission = float(result.get("trade_cost", 0.0) if trade_cost is None else trade_cost)
    ev_gross = pop * max_profit - (1 - pop) * max_loss
    result.update({
        "credit": 0.0,
        "debit": round(max(debit, 0.0), 2),
        "max_profit": round(max_profit, 2),
        "max_loss": round(max_loss, 2),
        "breakeven": round(breakeven, 2),
        "roc": round((max_profit - commission) / max(max_loss, 1e-9), 4),
        "expected_value_gross": round(ev_gross, 2),
        "trade_cost": round(max(commission, 0.0), 2),
        "expected_value": round(ev_gross - commission, 2),
    })


def add_quote_warning(result: dict) -> None:
    if result.get("quote_quality") != "executable_bid_ask":
        result["warnings"].append("premium estimated from yfinance last/mid/theoretical prices because bid/ask was incomplete")


def roll_plan_for_result(r: dict) -> dict:
    strategy = r.get("primary_strategy", "")
    if strategy in {"bull_put_spread", "bear_call_spread", "iron_condor"}:
        return {
            "family": "Roll Position",
            "trigger": "price touches short strike or loss reaches 50-60% of max loss",
            "action": "roll out to next monthly expiry and move challenged short strike farther OTM for a net credit where possible",
        }
    return {
        "family": "Roll Position",
        "trigger": "debit spread loses 50-60% of premium or thesis breaks",
        "action": "close or roll to next expiry only if the original directional thesis remains valid",
    }


def strategies_for_research(family: str, iv_class: str, trend_class: str) -> list[str]:
    key = family.strip().lower().replace("_", " ")
    family_map = {
        "single": ["single_call", "single_put"],
        "spread": ["bull_put_spread", "bear_call_spread", "bull_call_spread", "bear_put_spread"],
        "straddle": ["long_straddle"],
        "butterfly": ["butterfly"],
        "condor": ["condor"],
        "iron condor": ["iron_condor"],
    }
    if key in {"", "auto", "roll position"}:
        routed = STRATEGY_ROUTING.get((iv_class, trend_class), "iron_condor")
        candidates = [
            routed,
            "bull_put_spread", "bear_call_spread", "bull_call_spread", "bear_put_spread",
            "single_call", "single_put", "long_straddle", "butterfly", "condor", "iron_condor",
        ]
        return list(dict.fromkeys(candidates))
    if key in family_map:
        return family_map[key]
    return [forced_strategy_for_family(family, trend_class)]


def stress_test_score(r: dict) -> float:
    spot = float(r.get("spot", 0.0))
    iv = float(r.get("iv", 0.0))
    dte = int(r.get("dte", 1))
    expected_move = max(spot * max(iv, 0.001) * math.sqrt(max(dte, 1) / 365.0), 0.01)
    strategy = r.get("primary_strategy", "")
    if strategy in {"bull_put_spread", "bear_call_spread", "bull_call_spread", "bear_put_spread", "single_call", "single_put"}:
        breakeven = r.get("breakeven")
        if breakeven is None:
            return 4.0
        distance = abs(spot - float(breakeven)) / expected_move
        if strategy in {"bull_call_spread", "single_call"} and float(breakeven) > spot:
            return round(max(0.0, 10.0 - distance * 5.0), 1)
        if strategy in {"bear_put_spread", "single_put"} and float(breakeven) < spot:
            return round(max(0.0, 10.0 - distance * 5.0), 1)
        return round(min(distance * 4.0, 10.0), 1)
    lower = r.get("lower_breakeven")
    upper = r.get("upper_breakeven")
    if lower is not None and upper is not None:
        cushion = min(abs(spot - float(lower)), abs(float(upper) - spot)) / expected_move
        if strategy == "long_straddle":
            return round(max(0.0, 10.0 - cushion * 4.0), 1)
        return round(min(cushion * 4.0, 10.0), 1)
    return 5.0


def candidate_risk_flags(r: dict) -> list[str]:
    flags = []
    if r.get("quote_quality") != "executable_bid_ask":
        flags.append("quoted_price_estimated")
    if float(r.get("liquidity_score", 0.0)) < float(CONFIG["min_trade_liquidity_score"]):
        flags.append("thin_option_liquidity")
    if float(r.get("expected_value", 0.0)) <= 0:
        flags.append("negative_model_ev")
    if float(r.get("pop", 0.0)) < 0.45:
        flags.append("low_model_pop")
    if float(r.get("max_loss", 0.0)) > CONFIG["total_budget"] * CONFIG["max_position_pct"]:
        flags.append("above_preferred_position_size")
    if float(r.get("max_loss", 0.0)) > CONFIG["total_budget"]:
        flags.append("not_affordable_for_one_contract")
    gex = r.get("gamma_exposure") or {}
    if gex.get("regime") in {"negative", "negative_high"}:
        flags.append("negative_gamma_exposure")
    if gex.get("risk_level") == "high":
        flags.append("high_gamma_black_swan_risk")
    if strategy_family(str(r.get("primary_strategy", ""))) in {"Single", "Straddle"}:
        flags.append("debit_strategy_decay_risk")
    return flags


def confidence_score(r: dict, score_gap: float = 0.0) -> float:
    quote = 10.0 if r.get("quote_quality") == "executable_bid_ask" else 6.0
    liq = min(float(r.get("liquidity_score", 0.0)) * 10.0, 10.0)
    hist = 8.0 if r.get("historical_barrier_win_rate") is not None else 5.0
    ev = min(max((float(r.get("expected_value", 0.0)) / max(float(r.get("max_loss", 1.0)), 1.0) + 0.2) / 0.5, 0.0), 1.0) * 10
    flag_penalty = min(len(r.get("risk_flags", [])) * 1.2, 4.0)
    gap_bonus = min(max(score_gap, 0.0), 2.0)
    return round(min(max(0.25 * quote + 0.25 * liq + 0.25 * hist + 0.25 * ev - flag_penalty + gap_bonus, 0.0), 10.0), 1)


def research_score(r: dict) -> float:
    w = CONFIG["research_weights"]
    hist = r.get("historical_barrier_win_rate")
    hist_component = 5.0 if hist is None else float(hist) * 10.0
    total = (
        w["final"] * float(r.get("final_score", 0.0))
        + w["confidence"] * float(r.get("confidence_score", 0.0))
        + w["stress"] * float(r.get("stress_score", 0.0))
        + w["historical"] * hist_component
    )
    if float(r.get("max_loss", 0.0) or 0.0) > CONFIG["total_budget"]:
        total *= 0.25
    elif float(r.get("max_loss", 0.0) or 0.0) > CONFIG["total_budget"] * CONFIG["max_single_trade_pct"]:
        total *= 0.80
    return round(min(max(total, 0.0), 10.0), 1)


def apply_budget_fields(r: dict) -> dict:
    budget = float(CONFIG["total_budget"])
    max_loss = float(r.get("max_loss", 0.0) or 0.0)
    debit = float(r.get("debit", 0.0) or 0.0) * 100
    capital = max(max_loss, debit)
    if capital <= 0 and float(r.get("credit", 0.0) or 0.0) > 0 and float(r.get("width", 0.0) or 0.0) > 0:
        capital = max((float(r["width"]) - float(r.get("credit", 0.0))) * 100, 0.0)
    max_contracts = int(budget // capital) if capital > 0 else 0
    preferred_cap = budget * float(CONFIG["max_position_pct"])
    max_single_cap = budget * float(CONFIG["max_single_trade_pct"])
    reserve_floor = budget * float(CONFIG["min_cash_reserve_pct"])
    recommended_contracts = 0
    if capital > 0 and capital <= max_single_cap and budget - capital >= reserve_floor:
        recommended_contracts = 1
    elif capital > 0 and capital <= preferred_cap:
        recommended_contracts = 1
    r["capital_required"] = round(capital, 2)
    r["capital_usage_pct"] = round(capital / budget, 4) if budget > 0 else 0.0
    r["affordable_one_contract"] = bool(capital > 0 and capital <= budget)
    r["within_preferred_position_size"] = bool(capital > 0 and capital <= preferred_cap)
    r["within_max_single_trade_size"] = bool(capital > 0 and capital <= max_single_cap)
    r["max_contracts_by_budget"] = max_contracts
    r["recommended_contracts"] = recommended_contracts
    if capital <= 0:
        note = "未能计算资金占用"
    elif capital > budget:
        note = f"一张约需 ${capital:,.0f}，超过 ${budget:,.0f} 账户资金"
    elif capital > max_single_cap:
        note = f"一张约占账户 {capital / budget * 100:.1f}%，超过建议单笔上限 {CONFIG['max_single_trade_pct']*100:.0f}%"
    elif capital > preferred_cap:
        note = f"一张约占账户 {capital / budget * 100:.1f}%，可研究但高于舒适仓位 {CONFIG['max_position_pct']*100:.0f}%"
    else:
        note = f"一张约占账户 {capital / budget * 100:.1f}%，符合资金约束"
    r["budget_note"] = note
    return r


def conservative_pop_range(r: dict) -> dict:
    """A research range, not a live win-rate promise."""
    pop = float(r.get("pop", 0.0) or 0.0)
    confidence = float(r.get("confidence_score", 0.0) or 0.0)
    stress = float(r.get("stress_score", 0.0) or 0.0)
    penalty = 0.04 + max(0.0, 6.0 - confidence) * 0.015 + max(0.0, 6.0 - stress) * 0.01
    if r.get("quote_quality") != "executable_bid_ask":
        penalty += 0.03
    if float(r.get("liquidity_score", 0.0) or 0.0) < float(CONFIG["min_trade_liquidity_score"]):
        penalty += 0.04
    low = max(0.0, pop - penalty)
    high = min(0.95, pop + max(0.02, 0.06 - penalty / 2))
    return {"base": round(pop, 4), "low": round(low, 4), "high": round(high, 4)}


def rejection_reasons(r: dict) -> list[str]:
    reasons = []
    if not r.get("tradable"):
        reasons.append("无法构建真实期权腿")
    if float(r.get("spot", 0.0) or 0.0) < 5:
        reasons.append("股价过低，短约跳空和流动性风险偏高")
    if r.get("quote_quality") != "executable_bid_ask":
        reasons.append("期权报价不是完整 bid/ask，权利金需要人工复核")
    if float(r.get("liquidity_score", 0.0) or 0.0) < float(CONFIG["min_trade_liquidity_score"]):
        reasons.append("推荐腿流动性偏弱")
    if float(r.get("expected_value", 0.0) or 0.0) <= 0:
        reasons.append("模型期望值为负")
    if float(r.get("max_loss", 0.0) or 0.0) > CONFIG["total_budget"]:
        reasons.append("一张合约资金占用超过账户资金")
    if float(r.get("max_loss", 0.0) or 0.0) > CONFIG["total_budget"] * CONFIG["max_position_pct"]:
        reasons.append("单笔最大亏损超过舒适仓位上限")
    if abs(float(r.get("trend_30d", 0.0) or 0.0)) > 0.35:
        reasons.append("30日涨跌幅过大，存在过热或反转风险")
    if float(r.get("iv_hv_ratio", 1.0) or 1.0) > 2.2:
        reasons.append("IV/HV 极端偏高，可能是事件风险定价")
    if float(r.get("pop", 0.0) or 0.0) < 0.45:
        reasons.append("研究胜率偏低")
    return reasons


def model_failure_conditions(r: dict) -> list[str]:
    strategy = str(r.get("primary_strategy", ""))
    conditions = []
    if r.get("breakeven") is not None:
        conditions.append(f"价格接近或突破盈亏平衡点 {float(r['breakeven']):.2f}")
    if r.get("lower_breakeven") is not None and r.get("upper_breakeven") is not None:
        conditions.append(f"价格跌破 {float(r['lower_breakeven']):.2f} 或涨破 {float(r['upper_breakeven']):.2f}")
    if r.get("short_strike") is not None and strategy in {"bull_put_spread", "bear_call_spread"}:
        conditions.append(f"价格触及卖出腿 {float(r['short_strike']):.2f}")
    conditions.extend([
        "IV 继续快速扩张，组合价格恶化",
        "盘口 bid/ask 显著变宽，报告权利金失去参考意义",
        "出现财报、增发、并购、FDA、诉讼或评级突变等事件",
    ])
    return conditions[:5]


def watch_levels(r: dict) -> dict:
    levels = {}
    if r.get("breakeven") is not None:
        levels["breakeven"] = round(float(r["breakeven"]), 2)
    if r.get("short_strike") is not None:
        levels["short_strike"] = round(float(r["short_strike"]), 2)
    if r.get("long_strike") is not None:
        levels["long_strike"] = round(float(r["long_strike"]), 2)
    if r.get("lower_breakeven") is not None:
        levels["lower_breakeven"] = round(float(r["lower_breakeven"]), 2)
    if r.get("upper_breakeven") is not None:
        levels["upper_breakeven"] = round(float(r["upper_breakeven"]), 2)
    spot = float(r.get("spot", 0.0) or 0.0)
    iv = float(r.get("iv", 0.0) or 0.0)
    dte = int(r.get("dte", 1) or 1)
    expected_move = spot * max(iv, 0.001) * math.sqrt(max(dte, 1) / 365.0)
    levels["one_sigma_down"] = round(max(0.0, spot - expected_move), 2)
    levels["one_sigma_up"] = round(spot + expected_move, 2)
    return levels


def price_sparkline_points(values: list[float], width: int = 160, height: int = 40) -> str:
    vals = [float(v) for v in values if v is not None and np.isfinite(float(v))]
    if len(vals) < 2:
        return ""
    vals = vals[-30:]
    lo, hi = min(vals), max(vals)
    span = hi - lo if hi > lo else 1.0
    step = width / max(len(vals) - 1, 1)
    points = []
    for i, value in enumerate(vals):
        x = i * step
        y = height - ((value - lo) / span * (height - 4) + 2)
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def market_snapshot(r: dict) -> dict:
    recent = r.get("recent_returns") or []
    spot = float(r.get("spot", 0.0) or 0.0)
    prices = [float(v) for v in (r.get("recent_closes") or []) if v is not None]
    if spot > 0:
        prices = prices[-30:]
    if not prices and spot > 0:
        prices = [spot]
        for ret in reversed(recent[-29:]):
            try:
                prev = prices[-1] / (1.0 + float(ret))
            except Exception:
                prev = prices[-1]
            prices.append(prev)
        prices = list(reversed(prices))
    points = price_sparkline_points(prices)
    trend = float(r.get("trend_30d", 0.0) or 0.0)
    levels = r.get("watch_levels") or {}
    return {
        "spot": round(spot, 2),
        "trend_30d_pct": round(trend * 100, 1),
        "iv_hv_ratio": r.get("iv_hv_ratio"),
        "one_sigma_down": levels.get("one_sigma_down"),
        "one_sigma_up": levels.get("one_sigma_up"),
        "sparkline_points": points,
        "sparkline_period": "30 trading days",
        "sparkline_basis": "daily close",
    }


def firstrade_strategy_label(strategy: str) -> str:
    if strategy in {"single_call", "single_put"}:
        return "Single"
    if strategy in {"bull_put_spread", "bear_call_spread", "bull_call_spread", "bear_put_spread"}:
        return "Spread"
    if strategy == "long_straddle":
        return "Straddle"
    if strategy == "butterfly":
        return "Butterfly"
    if strategy == "condor":
        return "Condor"
    if strategy == "iron_condor":
        return "Iron Condor"
    return strategy_family(strategy)


def option_order_legs(r: dict) -> list[dict]:
    strategy = str(r.get("primary_strategy", ""))
    expiry = str(r.get("expiry_used", ""))
    legs: list[dict] = []

    def add(action: str, qty: int, strike_key: str, opt_type: str) -> None:
        strike = r.get(strike_key)
        if strike is not None:
            legs.append({"action": action, "qty": qty, "expiry": expiry, "strike": float(strike), "type": opt_type})

    if strategy == "bull_call_spread":
        add("BUY", 1, "long_strike", "Call")
        add("SELL", 1, "short_strike", "Call")
    elif strategy == "bear_put_spread":
        add("BUY", 1, "long_strike", "Put")
        add("SELL", 1, "short_strike", "Put")
    elif strategy == "bull_put_spread":
        add("SELL", 1, "short_strike", "Put")
        add("BUY", 1, "long_strike", "Put")
    elif strategy == "bear_call_spread":
        add("SELL", 1, "short_strike", "Call")
        add("BUY", 1, "long_strike", "Call")
    elif strategy == "single_call":
        add("BUY", 1, "long_strike", "Call")
    elif strategy == "single_put":
        add("BUY", 1, "long_strike", "Put")
    elif strategy == "long_straddle":
        add("BUY", 1, "call_strike", "Call")
        add("BUY", 1, "put_strike", "Put")
    elif strategy == "butterfly":
        add("BUY", 1, "lower_strike", "Call")
        add("SELL", 2, "body_strike", "Call")
        add("BUY", 1, "upper_strike", "Call")
    elif strategy == "condor":
        add("BUY", 1, "lower_strike", "Call")
        add("SELL", 1, "inner_low", "Call")
        add("SELL", 1, "inner_high", "Call")
        add("BUY", 1, "upper_strike", "Call")
    elif strategy == "iron_condor":
        add("SELL", 1, "put_short", "Put")
        add("BUY", 1, "put_long", "Put")
        add("SELL", 1, "call_short", "Call")
        add("BUY", 1, "call_long", "Call")
    return legs


def option_payoff_at_price(r: dict, underlying_price: float) -> float:
    strategy = str(r.get("primary_strategy", ""))
    s = float(underlying_price)
    credit = float(r.get("credit", 0.0) or 0.0) * 100
    debit = float(r.get("debit", 0.0) or 0.0) * 100

    def call_payoff(strike: float) -> float:
        return max(s - float(strike), 0.0) * 100

    def put_payoff(strike: float) -> float:
        return max(float(strike) - s, 0.0) * 100

    try:
        if strategy == "bull_call_spread":
            return call_payoff(r["long_strike"]) - call_payoff(r["short_strike"]) - debit
        if strategy == "bear_put_spread":
            return put_payoff(r["long_strike"]) - put_payoff(r["short_strike"]) - debit
        if strategy == "bull_put_spread":
            return credit - put_payoff(r["short_strike"]) + put_payoff(r["long_strike"])
        if strategy == "bear_call_spread":
            return credit - call_payoff(r["short_strike"]) + call_payoff(r["long_strike"])
        if strategy == "single_call":
            return call_payoff(r["long_strike"]) - debit
        if strategy == "single_put":
            return put_payoff(r["long_strike"]) - debit
        if strategy == "long_straddle":
            return call_payoff(r["call_strike"]) + put_payoff(r["put_strike"]) - debit
        if strategy == "butterfly":
            return call_payoff(r["lower_strike"]) - 2 * call_payoff(r["body_strike"]) + call_payoff(r["upper_strike"]) - debit
        if strategy == "condor":
            return call_payoff(r["lower_strike"]) - call_payoff(r["inner_low"]) - call_payoff(r["inner_high"]) + call_payoff(r["upper_strike"]) - debit
        if strategy == "iron_condor":
            return credit - put_payoff(r["put_short"]) + put_payoff(r["put_long"]) - call_payoff(r["call_short"]) + call_payoff(r["call_long"])
    except Exception:
        return 0.0
    return 0.0


def payoff_scenarios(r: dict) -> list[dict]:
    spot = float(r.get("spot", 0.0) or 0.0)
    levels = r.get("watch_levels") or {}
    raw = [
        ("current", spot),
        ("1sigma_down", levels.get("one_sigma_down")),
        ("breakeven", levels.get("breakeven")),
        ("short_strike", levels.get("short_strike")),
        ("1sigma_up", levels.get("one_sigma_up")),
    ]
    seen = set()
    rows = []
    max_loss = max(float(r.get("max_loss", 0.0) or 0.0), 1.0)
    for label, value in raw:
        if value is None:
            continue
        price = round(float(value), 2)
        if price <= 0 or price in seen:
            continue
        seen.add(price)
        pnl = round(option_payoff_at_price(r, price), 2)
        rows.append({
            "label": label,
            "underlying_price": price,
            "price_move_pct": round((price / spot - 1) * 100, 1) if spot > 0 else 0.0,
            "estimated_pnl_at_expiry": pnl,
            "pnl_pct_of_max_loss": round(pnl / max_loss * 100, 1),
        })
    return rows


def firstrade_order_guide(r: dict) -> dict:
    credit = float(r.get("credit", 0.0) or 0.0)
    debit = float(r.get("debit", 0.0) or 0.0)
    recommended_contracts = int(r.get("recommended_contracts", 0) or 0)
    return {
        "strategy_dropdown": firstrade_strategy_label(str(r.get("primary_strategy", ""))),
        "legs": option_order_legs(r),
        "example_quantity": 1,
        "recommended_contracts": recommended_contracts,
        "net_price_type": "Credit" if credit > 0 else "Debit",
        "net_price": round(credit if credit > 0 else debit, 2),
        "other_conditions": "None",
        "submit_note": "先点检视/预览，再确认下单；只使用组合限价单，不要拆腿下单，不要使用市价单。",
    }


def single_only_fallback_guide(r: dict) -> dict:
    """Beginner-safe order-ticket guidance when the account only supports Single."""
    strategy = str(r.get("primary_strategy", ""))
    expiry = str(r.get("expiry_used", ""))
    debit = float(r.get("debit", 0.0) or 0.0)
    spot = float(r.get("spot", 0.0) or 0.0)

    def guide(status: str, action: str = "-", strike: object = None, opt_type: str = "-", note: str = "") -> dict:
        strike_value = None if strike is None else round(float(strike), 2)
        return {
            "status": status,
            "strategy_dropdown": "Single",
            "transaction": action,
            "quantity": 1,
            "expiry": expiry,
            "strike": strike_value,
            "option_type": opt_type,
            "limit_type": "Limit",
            "limit_reference": f"限价 <= ${debit:.2f}" if debit > 0 and status == "native" else "参考券商实时中间价/卖价；不要使用市价单",
            "duration": "当日有效",
            "other_conditions": "None",
            "note": note,
        }

    if strategy == "single_call":
        return guide(
            "native",
            "Buy Open",
            r.get("long_strike"),
            "Call",
            "该策略本身就是 Single 单腿订单。最大亏损为支付的权利金 x 100。",
        )
    if strategy == "single_put":
        return guide(
            "native",
            "Buy Open",
            r.get("long_strike"),
            "Put",
            "该策略本身就是 Single 单腿订单。最大亏损为支付的权利金 x 100。",
        )
    if strategy == "bull_call_spread":
        return guide(
            "proxy",
            "Buy Open",
            r.get("long_strike"),
            "Call",
            "这是看涨观点的单腿替代方案，不等同于价差组合：成本和时间损耗更高，也没有卖出 Call 来抵消权利金。",
        )
    if strategy == "bear_put_spread":
        return guide(
            "proxy",
            "Buy Open",
            r.get("long_strike"),
            "Put",
            "这是看跌观点的单腿替代方案，不等同于价差组合：成本和时间损耗更高，也没有卖出 Put 来抵消权利金。",
        )
    if strategy == "bull_put_spread":
        strike = r.get("short_strike") or round(spot, 2)
        return guide(
            "proxy",
            "Buy Open",
            strike,
            "Call",
            "Put Credit Spread 没有安全的 Single 等价替代。这里的 Call 只是看涨代理；不要用裸卖 Put 替代信用价差。",
        )
    if strategy == "bear_call_spread":
        strike = r.get("short_strike") or round(spot, 2)
        return guide(
            "proxy",
            "Buy Open",
            strike,
            "Put",
            "Call Credit Spread 没有安全的 Single 等价替代。这里的 Put 只是看跌代理；不要用裸卖 Call 替代信用价差。",
        )
    return guide(
        "not_recommended",
        "-",
        None,
        "-",
        "该策略需要多腿组合权限。如果账户只能做 Single，建议跳过，不要手动拆腿模拟。",
    )


def research_tier(r: dict) -> str:
    reasons = rejection_reasons(r)
    score = float(r.get("research_score", r.get("final_score", 0.0)) or 0.0)
    confidence = float(r.get("confidence_score", 0.0) or 0.0)
    if any(reason in reasons for reason in ["无法构建真实期权腿", "模型期望值为负", "一张合约资金占用超过账户资金"]):
        return "C-谨慎/剔除"
    if score >= 6.5 and confidence >= 5.5 and len(reasons) <= 2:
        return "A-重点研究"
    if score >= 4.8 and len(reasons) <= 5:
        return "B-观察复核"
    return "C-谨慎/剔除"


def enrich_research_fields(r: dict) -> dict:
    apply_budget_fields(r)
    r["rejection_reasons"] = rejection_reasons(r)
    r["model_failure_conditions"] = model_failure_conditions(r)
    r["watch_levels"] = watch_levels(r)
    r["market_snapshot"] = market_snapshot(r)
    r["firstrade_order_guide"] = firstrade_order_guide(r)
    r["single_only_fallback"] = single_only_fallback_guide(r)
    r["payoff_scenarios"] = payoff_scenarios(r)
    r["conservative_pop_range"] = conservative_pop_range(r)
    r["research_tier"] = research_tier(r)
    return r


def slim_candidate(r: dict) -> dict:
    keys = [
        "primary_strategy", "strategy_family", "strategy_desc", "pop", "model_pop",
        "historical_barrier_win_rate", "credit", "debit", "max_profit", "max_loss",
        "breakeven", "lower_breakeven", "upper_breakeven", "roc", "expected_value",
        "liquidity_score", "quote_quality", "risk_flags", "stress_score",
        "gamma_exposure", "gamma_risk_level",
        "confidence_score", "final_score", "research_score", "research_tier",
        "rejection_reasons", "watch_levels", "conservative_pop_range",
        "capital_required", "capital_usage_pct", "affordable_one_contract",
        "within_preferred_position_size", "max_contracts_by_budget",
        "recommended_contracts", "budget_note", "market_snapshot", "liquidity_grade",
        "firstrade_order_guide", "single_only_fallback", "payoff_scenarios",
    ]
    return {k: r.get(k) for k in keys if k in r}


def strategy_family_candidates(candidates: list[dict]) -> list[dict]:
    """Expose the best evaluated candidate per strategy family for audit."""
    families = [f for f in SUPPORTED_STRATEGY_FAMILIES if f != "Roll Position"]
    grouped: dict[str, list[dict]] = {}
    for candidate in candidates:
        grouped.setdefault(str(candidate.get("strategy_family", "Unknown")), []).append(candidate)

    summary = []
    for family in families:
        items = grouped.get(family, [])
        if not items:
            summary.append({
                "strategy_family": family,
                "status": "not_available",
                "reason": "could_not_construct_tradable_legs_or_quote_data_missing",
            })
            continue
        items.sort(key=lambda r: (
            not r.get("affordable_one_contract", False),
            not r.get("within_max_single_trade_size", False),
            -float(r.get("research_score", 0.0) or 0.0),
            -float(r.get("final_score", 0.0) or 0.0),
        ))
        best = slim_candidate(items[0])
        best["status"] = "evaluated"
        summary.append(best)
    return summary


def evaluate_ticker(ticker: str, data: dict) -> dict:
    close = data["close_prices"]
    hv = compute_hv(close)
    iv = data["iv"] if data["iv"] > 0 else hv
    trend_30d = (close[-1] - close[-31]) / close[-31] if len(close) > 30 else 0.0
    iv_class = classify_iv_hv(iv, hv)
    trend_class = classify_trend(trend_30d)
    strategies = strategies_for_research(CONFIG.get("strategy_family", "auto"), iv_class, trend_class)
    candidates = []
    for strategy in strategies:
        try:
            candidate = build_strategy_result(ticker, data, strategy)
        except Exception as exc:
            continue
        if not candidate.get("tradable"):
            continue
        candidate["risk_flags"] = candidate_risk_flags(candidate)
        candidate["stress_score"] = stress_test_score(candidate)
        candidate["confidence_score"] = confidence_score(candidate)
        candidate["research_score"] = research_score(candidate)
        enrich_research_fields(candidate)
        candidates.append(candidate)

    if not candidates:
        result = build_strategy_result(ticker, data, strategies[0] if strategies else None)
        result["risk_flags"] = candidate_risk_flags(result)
        result["stress_score"] = stress_test_score(result)
        result["confidence_score"] = 0.0
        result["research_score"] = 0.0
        enrich_research_fields(result)
        result["strategy_candidates"] = []
        result["strategy_family_candidates"] = strategy_family_candidates([])
        result["strategy_family_coverage"] = {
            "evaluated": [],
            "not_available": [f for f in SUPPORTED_STRATEGY_FAMILIES if f != "Roll Position"],
        }
        return result

    candidates.sort(key=lambda r: (
        not r.get("affordable_one_contract", False),
        not r.get("within_max_single_trade_size", False),
        -r.get("research_score", 0.0),
        -r.get("final_score", 0.0),
        -r.get("expected_value", -999999),
    ))
    gap = candidates[0].get("research_score", 0.0) - (candidates[1].get("research_score", 0.0) if len(candidates) > 1 else 0.0)
    candidates[0]["confidence_score"] = confidence_score(candidates[0], gap)
    candidates[0]["research_score"] = research_score(candidates[0])
    enrich_research_fields(candidates[0])
    best = candidates[0]
    best["strategy_candidates"] = [slim_candidate(c) for c in candidates[:8]]
    family_candidates = strategy_family_candidates(candidates)
    best["strategy_family_candidates"] = family_candidates
    best["strategy_family_coverage"] = {
        "evaluated": [c["strategy_family"] for c in family_candidates if c.get("status") == "evaluated"],
        "not_available": [c["strategy_family"] for c in family_candidates if c.get("status") == "not_available"],
    }
    if len(candidates) > 1:
        best["alternative_strategy"] = slim_candidate(candidates[1])
        best["avoid_strategy"] = slim_candidate(candidates[-1])
    best["research_notes"] = {
        "candidate_count": len(candidates),
        "ranking_method": "final_score + confidence + stress cushion + historical barrier win rate",
        "not_live_win_rate": "POP is a research estimate, not verified live execution win rate.",
    }
    return best


def option_chain_diagnostics(calls: pd.DataFrame, puts: pd.DataFrame) -> dict:
    def frame_stats(frame: pd.DataFrame) -> dict:
        if frame is None or frame.empty:
            return {
                "rows": 0,
                "bid_ask_rows": 0,
                "volume_rows": 0,
                "open_interest_rows": 0,
                "iv_median": None,
            }
        bid = pd.to_numeric(frame.get("bid", 0), errors="coerce").fillna(0)
        ask = pd.to_numeric(frame.get("ask", 0), errors="coerce").fillna(0)
        volume = pd.to_numeric(frame.get("volume", 0), errors="coerce").fillna(0)
        open_interest = pd.to_numeric(frame.get("openInterest", 0), errors="coerce").fillna(0)
        iv = pd.to_numeric(frame.get("impliedVolatility", pd.Series(dtype=float)), errors="coerce").dropna()
        return {
            "rows": int(len(frame)),
            "bid_ask_rows": int(((bid > 0) & (ask > 0)).sum()),
            "volume_rows": int((volume > 0).sum()),
            "open_interest_rows": int((open_interest > 0).sum()),
            "iv_median": round(float(iv.median()), 4) if not iv.empty else None,
        }

    call_stats = frame_stats(calls)
    put_stats = frame_stats(puts)
    total_rows = call_stats["rows"] + put_stats["rows"]
    total_bid_ask = call_stats["bid_ask_rows"] + put_stats["bid_ask_rows"]
    return {
        "listed_options": total_rows > 0,
        "total_rows": total_rows,
        "bid_ask_rows": total_bid_ask,
        "quote_completeness": round(total_bid_ask / total_rows, 4) if total_rows else 0.0,
        "calls": call_stats,
        "puts": put_stats,
    }


def finalize_option_market_status(result: dict) -> None:
    diagnostics = result.get("option_chain_diagnostics") or {}
    if not diagnostics.get("listed_options"):
        result["option_market_status"] = "no_listed_options"
        result["option_evidence_status"] = "no_options_trading"
        result["option_evidence_message"] = "未发现上市期权链；这属于没有期权交易，不应作为期权证据。"
        return

    result["option_market_status"] = "listed_options"
    if result.get("tradable"):
        result["option_evidence_status"] = "tradable_option_structure"
        result["option_evidence_message"] = "已获取期权链，并能按当前规则构建真实可交易组合腿。"
        return

    quote_completeness = float(diagnostics.get("quote_completeness", 0.0) or 0.0)
    if quote_completeness <= 0:
        reason = "期权链存在，但 bid/ask 报价为空或不可用，无法构建真实可交易组合腿。"
    else:
        reason = (
            f"期权链存在，但按当前策略、报价、流动性和预算规则无法构建真实可交易组合腿；"
            f"链报价完整度约 {quote_completeness:.0%}。"
        )
    result["option_evidence_status"] = "chain_available_but_no_tradable_legs"
    result["option_evidence_message"] = reason


def build_strategy_result(ticker: str, data: dict, strategy_override: Optional[str] = None) -> dict:
    spot = float(data["spot"])
    close = data["close_prices"]
    calls = data["calls"]
    puts = data["puts"]
    dte = int(data["dte"])
    hv = compute_hv(close)
    iv = data["iv"] if data["iv"] > 0 else hv
    trend_30d = (close[-1] - close[-31]) / close[-31] if len(close) > 30 else 0.0
    iv_class = classify_iv_hv(iv, hv)
    trend_class = classify_trend(trend_30d)
    strategy = strategy_override or forced_strategy_for_family(CONFIG.get("strategy_family", "auto"), trend_class) or STRATEGY_ROUTING.get((iv_class, trend_class), "iron_condor")
    fundamentals = data.get("fundamentals") or {}
    chain_diagnostics = option_chain_diagnostics(calls, puts)
    result = {
        "ticker": ticker,
        "spot": round(spot, 2),
        "market_cap": fundamentals.get("market_cap"),
        "trailing_pe": fundamentals.get("trailing_pe"),
        "forward_pe": fundamentals.get("forward_pe"),
        "shares_outstanding": fundamentals.get("shares_outstanding"),
        "iv": round(iv, 4),
        "hv": round(hv, 4),
        "iv_hv_ratio": round(iv / hv, 2) if hv > 0 else 99.0,
        "iv_class": iv_class,
        "trend_30d": round(trend_30d, 4),
        "trend_class": trend_class,
        "primary_strategy": strategy,
        "strategy_family": strategy_family(strategy),
        "supported_strategy_families": SUPPORTED_STRATEGY_FAMILIES,
        "dte": dte,
        "expiry_used": data["expiry_used"],
        "cache_sources": data.get("cache_sources", {}),
        "option_chain_diagnostics": chain_diagnostics,
        "option_market_status": "listed_options" if chain_diagnostics.get("listed_options") else "no_listed_options",
        "option_evidence_status": "pending",
        "option_evidence_message": "",
        "tradable": False,
        "warnings": [],
        "recent_closes": [round(float(x), 4) for x in close[-30:]],
        "recent_ohlcv": data.get("recent_ohlcv") or [],
        "price_as_of": data.get("price_as_of"),
        "pullback_rejection": data.get("pullback_rejection") or {},
        "pullback_confirmation": data.get("pullback_confirmation") or {},
        "recent_returns": pd.Series(close).pct_change().dropna().tail(60).round(6).tolist(),
    }

    if strategy == "bull_put_spread":
        spread = choose_credit_spread(puts, spot, iv, dte, strategy)
        if spread:
            short, long = spread["short"], spread["long"]
            credit, width, pop = spread["credit"], spread["width"], spread["pop"]
            result.update({"short_strike": float(short["strike"]), "long_strike": float(long["strike"]), "width": round(width, 2), "pop": round(pop, 4), "strategy_desc": f"Sell {short['strike']}P / Buy {long['strike']}P"})
            credit_metrics(result, credit, width, float(short["strike"]) - credit, pop, spread.get("trade_cost"))
            result["liquidity_score"] = round(spread["liquidity_score"], 4)
            result["quote_quality"] = spread["quote_quality"]
            result["quote_sources"] = spread["quote_sources"]
            add_quote_warning(result)
            result["tradable"] = True

    elif strategy == "bear_call_spread":
        spread = choose_credit_spread(calls, spot, iv, dte, strategy)
        if spread:
            short, long = spread["short"], spread["long"]
            credit, width, pop = spread["credit"], spread["width"], spread["pop"]
            result.update({"short_strike": float(short["strike"]), "long_strike": float(long["strike"]), "width": round(width, 2), "pop": round(pop, 4), "strategy_desc": f"Sell {short['strike']}C / Buy {long['strike']}C"})
            credit_metrics(result, credit, width, float(short["strike"]) + credit, pop, spread.get("trade_cost"))
            result["liquidity_score"] = round(spread["liquidity_score"], 4)
            result["quote_quality"] = spread["quote_quality"]
            result["quote_sources"] = spread["quote_sources"]
            add_quote_warning(result)
            result["tradable"] = True

    elif strategy == "iron_condor":
        ps = nearest(puts, target_put_strike(spot, iv, dte, 0.20), above=False)
        pl = nearest(puts[puts["strike"] < ps["strike"]] if ps is not None else puts, float(ps["strike"]) * 0.93 if ps is not None else spot * 0.93, above=False)
        cs = nearest(calls, target_call_strike(spot, iv, dte, 0.20), above=True)
        cl = nearest(calls[calls["strike"] > cs["strike"]] if cs is not None else calls, float(cs["strike"]) * 1.07 if cs is not None else spot * 1.07, above=True)
        if all(x is not None for x in [ps, pl, cs, cl]):
            ps_px, ps_src = executable_price(ps, "sell", "put", spot, iv, dte)
            pl_px, pl_src = executable_price(pl, "buy", "put", spot, iv, dte)
            cs_px, cs_src = executable_price(cs, "sell", "call", spot, iv, dte)
            cl_px, cl_src = executable_price(cl, "buy", "call", spot, iv, dte)
            if not valid_leg_prices(ps_px, pl_px, cs_px, cl_px):
                result["warnings"].append("missing executable bid/ask on one or more iron condor legs")
                finalize_option_market_status(result)
                return result
            liquid, liquidity_reasons = legs_pass_liquidity(ps, pl, cs, cl)
            if not liquid:
                result["warnings"].append("failed liquidity filter: " + "; ".join(liquidity_reasons[:4]))
                finalize_option_market_status(result)
                return result
            credit = ps_px - pl_px + cs_px - cl_px
            width = max(float(ps["strike"] - pl["strike"]), float(cl["strike"] - cs["strike"]))
            pop = probability_between(spot, float(ps["strike"]), float(cs["strike"]), dte, iv)
            if credit > 0 and width > 0:
                result.update({"put_short": float(ps["strike"]), "put_long": float(pl["strike"]), "call_short": float(cs["strike"]), "call_long": float(cl["strike"]), "width": round(width, 2), "pop": round(pop, 4), "strategy_desc": f"IC: {ps['strike']}/{pl['strike']}P + {cs['strike']}/{cl['strike']}C"})
                credit_metrics(result, credit, width, 0.0, pop, cost_model.option_trade_cost(4))
                result["lower_breakeven"] = round(float(ps["strike"]) - credit, 2)
                result["upper_breakeven"] = round(float(cs["strike"]) + credit, 2)
                result["liquidity_score"] = round(legs_liquidity(ps, pl, cs, cl), 4)
                result["quote_quality"] = quote_quality(ps_src, pl_src, cs_src, cl_src)
                result["quote_sources"] = {"put_short": ps_src, "put_long": pl_src, "call_short": cs_src, "call_long": cl_src}
                add_quote_warning(result)
                result["tradable"] = True

    elif strategy in {"single_call", "single_put"}:
        option_type = "call" if strategy == "single_call" else "put"
        frame = calls if option_type == "call" else puts
        single = choose_single(frame, spot, iv, dte, option_type)
        if single:
            leg = single["long"]
            result.update({
                "long_strike": float(leg["strike"]),
                "pop": round(single["pop"], 4),
                "strategy_desc": f"Buy {leg['strike']}{option_type[0].upper()}",
                "breakeven": round(single["breakeven"], 2),
                "credit": 0.0,
                "debit": round(single["debit"], 2),
                "max_profit": round(single["max_profit_est"], 2),
                "max_loss": round(single["max_loss"], 2),
                "roc": round((single["max_profit_est"] - single.get("trade_cost", 0.0)) / max(single["max_loss"], 1e-9), 4),
                "expected_value": round(single["expected_value"], 2),
                "expected_value_gross": round(single.get("expected_value_gross", single["expected_value"]), 2),
                "trade_cost": round(single.get("trade_cost", 0.0), 2),
                "liquidity_score": round(single["liquidity_score"], 4),
                "quote_quality": single["quote_quality"],
                "quote_sources": single["quote_sources"],
            })
            add_quote_warning(result)
            result["tradable"] = True

    elif strategy == "long_straddle":
        st = choose_long_straddle(calls, puts, spot, iv, dte)
        if st:
            c, p = st["call"], st["put"]
            result.update({
                "call_strike": float(c["strike"]), "put_strike": float(p["strike"]),
                "lower_breakeven": round(st["lower_breakeven"], 2), "upper_breakeven": round(st["upper_breakeven"], 2),
                "pop": round(st["pop"], 4), "strategy_desc": f"Buy {c['strike']}C + Buy {p['strike']}P",
                "credit": 0.0, "debit": round(st["debit"], 2), "max_profit": round(st["max_profit_est"], 2),
                "max_loss": round(st["max_loss"], 2), "roc": round(st["max_profit_est"] / max(st["max_loss"], 1e-9), 4),
                "expected_value": round(st["expected_value"], 2), "expected_value_gross": round(st.get("expected_value_gross", st["expected_value"]), 2),
                "trade_cost": round(st.get("trade_cost", 0.0), 2), "liquidity_score": round(st["liquidity_score"], 4),
                "quote_quality": st["quote_quality"], "quote_sources": st["quote_sources"],
            })
            add_quote_warning(result)
            result["tradable"] = True

    elif strategy == "butterfly":
        fly = choose_call_butterfly(calls, spot, iv, dte)
        if fly:
            result.update({
                "lower_strike": float(fly["lower"]["strike"]), "body_strike": float(fly["body"]["strike"]), "upper_strike": float(fly["upper"]["strike"]),
                "lower_breakeven": round(fly["lower_breakeven"], 2), "upper_breakeven": round(fly["upper_breakeven"], 2),
                "pop": round(fly["pop"], 4), "strategy_desc": f"Call Butterfly: Buy {fly['lower']['strike']}C / Sell 2x {fly['body']['strike']}C / Buy {fly['upper']['strike']}C",
                "credit": 0.0, "debit": round(fly["debit"], 2), "max_profit": round(fly["max_profit"], 2), "max_loss": round(fly["max_loss"], 2),
                "roc": round((fly["max_profit"] - fly.get("trade_cost", 0.0)) / max(fly["max_loss"], 1e-9), 4),
                "expected_value": round(fly["expected_value"], 2), "expected_value_gross": round(fly.get("expected_value_gross", fly["expected_value"]), 2),
                "trade_cost": round(fly.get("trade_cost", 0.0), 2),
                "liquidity_score": round(fly["liquidity_score"], 4), "quote_quality": fly["quote_quality"], "quote_sources": fly["quote_sources"],
            })
            add_quote_warning(result)
            result["tradable"] = True

    elif strategy == "condor":
        condor = choose_call_condor(calls, spot, iv, dte)
        if condor:
            result.update({
                "lower_strike": float(condor["lower"]["strike"]), "inner_low": float(condor["inner_low"]["strike"]),
                "inner_high": float(condor["inner_high"]["strike"]), "upper_strike": float(condor["upper"]["strike"]),
                "lower_breakeven": round(condor["lower_breakeven"], 2), "upper_breakeven": round(condor["upper_breakeven"], 2),
                "pop": round(condor["pop"], 4), "strategy_desc": f"Call Condor: Buy {condor['lower']['strike']}C / Sell {condor['inner_low']['strike']}C / Sell {condor['inner_high']['strike']}C / Buy {condor['upper']['strike']}C",
                "credit": 0.0, "debit": round(condor["debit"], 2), "max_profit": round(condor["max_profit"], 2), "max_loss": round(condor["max_loss"], 2),
                "roc": round((condor["max_profit"] - condor.get("trade_cost", 0.0)) / max(condor["max_loss"], 1e-9), 4),
                "expected_value": round(condor["expected_value"], 2), "expected_value_gross": round(condor.get("expected_value_gross", condor["expected_value"]), 2),
                "trade_cost": round(condor.get("trade_cost", 0.0), 2),
                "liquidity_score": round(condor["liquidity_score"], 4), "quote_quality": condor["quote_quality"], "quote_sources": condor["quote_sources"],
            })
            add_quote_warning(result)
            result["tradable"] = True

    else:
        option_type = "call" if strategy in ("bull_call_spread", "long_call") else "put"
        frame = calls if option_type == "call" else puts
        spread = choose_debit_spread(frame, spot, iv, dte, strategy)
        if spread:
            long, short = spread["long"], spread["short"]
            debit, width, breakeven, pop = spread["debit"], spread["width"], spread["breakeven"], spread["pop"]
            result.update({"long_strike": float(long["strike"]), "short_strike": float(short["strike"]), "width": round(width, 2), "pop": round(pop, 4), "strategy_desc": f"Buy {long['strike']}{option_type[0].upper()} / Sell {short['strike']}{option_type[0].upper()}"})
            debit_metrics(result, debit, width, breakeven, pop, spread.get("trade_cost"))
            result["liquidity_score"] = round(spread["liquidity_score"], 4)
            result["quote_quality"] = spread["quote_quality"]
            result["quote_sources"] = spread["quote_sources"]
            add_quote_warning(result)
            result["tradable"] = True

    if "pop" not in result:
        result.update({"pop": 0.0, "liquidity_score": 0.0, "max_loss": 0.0, "roc": 0.0, "expected_value": 0.0})
        result["warnings"].append("could not construct real tradable legs")
    result["model_pop"] = result.get("pop", 0.0)
    if result.get("tradable"):
        hist_wr = historical_barrier_win_rate(close, strategy, spot, result, dte)
        result["historical_barrier_win_rate"] = None if math.isnan(hist_wr) else hist_wr
        if not math.isnan(hist_wr):
            result["pop"] = round(0.65 * result["model_pop"] + 0.35 * hist_wr, 4)
            if "credit" in result and result.get("credit", 0) > 0:
                width = float(result.get("width", 0))
                credit = float(result.get("credit", 0))
                if strategy == "bull_put_spread":
                    credit_metrics(result, credit, width, float(result["short_strike"]) - credit, result["pop"])
                elif strategy == "bear_call_spread":
                    credit_metrics(result, credit, width, float(result["short_strike"]) + credit, result["pop"])
                elif strategy == "iron_condor":
                    credit_metrics(result, credit, width, 0.0, result["pop"])
                    result["lower_breakeven"] = round(float(result["put_short"]) - credit, 2)
                    result["upper_breakeven"] = round(float(result["call_short"]) + credit, 2)
            elif "debit" in result and result.get("debit", 0) > 0 and "breakeven" in result:
                debit = float(result["debit"])
                width = float(result.get("width", 0))
                debit_metrics(result, debit, width, float(result["breakeven"]), result["pop"])
    result["theme"] = ticker_theme(ticker)
    result["gamma_exposure"] = data.get("gamma_exposure", {})
    result["gamma_risk_level"] = (result["gamma_exposure"] or {}).get("risk_level", "unknown")
    if result.get("gamma_risk_level") == "high":
        result["warnings"].append("gamma exposure risk is high; treat short-DTE recommendations as research-only until structure cools down")
    result["liquidity_grade"] = liquidity_grade(float(result.get("liquidity_score", 0.0) or 0.0))
    finalize_option_market_status(result)
    result["strategy_family"] = strategy_family(str(result.get("primary_strategy", strategy)))
    result["roll_position_plan"] = roll_plan_for_result(result)
    result["final_score"] = score_result(result)
    return result


def score_result(r: dict) -> float:
    if not r.get("tradable"):
        return 0.0
    w = CONFIG["score_weights"]
    strategy = r["primary_strategy"]
    is_sell = strategy in {"bull_put_spread", "bear_call_spread", "iron_condor"}
    pop_score = r["pop"] * 10
    iv_score = min(r["iv_hv_ratio"] / 1.5, 1.0) * 10 if is_sell else min(max((1.2 - r["iv_hv_ratio"]) / 0.5, 0.0), 1.0) * 10
    trend = r["trend_class"]
    trend_score = 10.0 if (
        strategy in {"bull_put_spread", "bull_call_spread", "single_call"} and trend in {"up", "strong_up"}
        or strategy in {"bear_call_spread", "bear_put_spread"} and trend in {"down", "strong_down"}
        or strategy in {"iron_condor", "condor", "butterfly", "long_straddle"} and trend == "range"
    ) else (7.0 if trend == "range" else max(0.0, 7.0 - abs(r["trend_30d"]) * 30))
    roc = float(r.get("roc", 0.0))
    roc_score = (roc / CONFIG["min_credit_roc"] * 4.0) if is_sell and roc < CONFIG["min_credit_roc"] else min(roc / 0.35, 1.0) * 10
    ev_ratio = float(r.get("expected_value", 0.0)) / max(float(r.get("max_loss", 1.0)), 1.0)
    ev_score = min(max((ev_ratio + 0.25) / 0.5, 0.0), 1.0) * 10
    liq_score = float(r.get("liquidity_score", 0.0)) * 10
    gex = r.get("gamma_exposure") or {}
    gamma_penalty = 1.0
    if gex.get("risk_level") == "high":
        gamma_penalty = 0.82
    elif gex.get("risk_level") == "medium" and gex.get("regime") == "negative":
        gamma_penalty = 0.92
    final = (
        w["pop"] * pop_score
        + w["iv_hv"] * iv_score
        + w["trend"] * trend_score
        + w["roc"] * roc_score
        + w["expected_value"] * ev_score
        + w["liquidity"] * liq_score
    )
    if r.get("expected_value", 0.0) <= 0:
        final *= 0.75
    final *= gamma_penalty
    return round(min(max(final, 0.0), 10.0), 1)


def cached_history(stock: yf.Ticker, ticker: str, period: str) -> tuple[pd.DataFrame, str]:
    routed, routed_source = get_daily_history(
        ticker, period=period,
        allow_yfinance_fallback=not CONFIG.get("daily_price_cache_only", False),
    )
    if routed is not None and not routed.empty:
        frame = routed.reset_index()
        frame["Date"] = pd.to_datetime(frame["Date"]).dt.tz_localize(None)
        return frame, routed_source
    if CONFIG.get("daily_price_cache_only", False):
        raise ValueError("daily price cache missing; bulk sync required before scan")
    if not CONFIG.get("cache_enabled", True):
        return stock.history(period=period), "live:history"

    path = os.path.join(ensure_cache_dir("history"), f"{cache_symbol(ticker)}.csv")
    cached = read_csv_cache(path, parse_dates=["Date"])
    if cached is not None and not cached.empty:
        cached = cached.dropna(subset=["Date"]).sort_values("Date")
        last_date = pd.to_datetime(cached["Date"]).max().date()
        start_date = last_date - timedelta(days=int(CONFIG["history_refresh_overlap_days"]))
        try:
            fresh = stock.history(start=start_date.isoformat())
            if fresh is not None and not fresh.empty:
                fresh = fresh.reset_index()
                fresh["Date"] = pd.to_datetime(fresh["Date"]).dt.tz_localize(None)
                merged = pd.concat([cached, fresh], ignore_index=True)
                merged["Date"] = pd.to_datetime(merged["Date"]).dt.tz_localize(None)
                merged = merged.drop_duplicates(subset=["Date"], keep="last").sort_values("Date")
                write_csv_cache(merged, path)
                return merged, "cache+incremental:history"
        except Exception as exc:
            print(f"[WARN] incremental history refresh failed for {ticker}: {exc}; using cached history.")
        return cached, "cache:history"

    hist = stock.history(period=period)
    if hist is not None and not hist.empty:
        frame = hist.reset_index()
        frame["Date"] = pd.to_datetime(frame["Date"]).dt.tz_localize(None)
        write_csv_cache(frame, path)
        return frame, "live:history"
    return hist, "live:history"


def cached_options(stock: yf.Ticker, ticker: str) -> tuple[tuple[str, ...], str]:
    if not CONFIG.get("cache_enabled", True):
        expiries = _cboe_expiries(ticker)
        if expiries:
            return expiries, "live:cboe_options"
        return tuple(stock.options), "live:options"

    path = os.path.join(ensure_cache_dir("options"), f"{cache_symbol(ticker)}_expiries.json")
    ttl = int(float(CONFIG["options_expiry_cache_ttl_hours"]) * 3600)
    if cache_is_fresh(path, ttl):
        try:
            with open(path, "r", encoding="utf-8") as f:
                expiries = tuple(json.load(f).get("expiries", []))
            if expiries:
                return expiries, "cache:options"
        except Exception:
            pass
    expiries = _cboe_expiries(ticker)
    options_source = "live:cboe_options" if expiries else "live:options"
    if not expiries:
        expiries = tuple(stock.options)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": datetime.now().isoformat(), "expiries": list(expiries)}, f, indent=2)
    except Exception as exc:
        print(f"[WARN] could not write options cache for {ticker}: {exc}")
    return expiries, options_source


def _cboe_expiries(ticker: str) -> tuple[str, ...]:
    if not CONFIG.get("cboe_option_chain_enabled", True):
        return tuple()
    chain = get_cboe_option_chain(ticker)
    expiries = tuple(str(x) for x in chain.get("expiries", []) if str(x))
    return expiries


def cached_option_chain(stock: yf.Ticker, ticker: str, expiry: str) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    if not CONFIG.get("cache_enabled", True):
        if CONFIG.get("cboe_option_chain_enabled", True):
            chain_payload = get_cboe_option_chain(ticker, expiry, ttl_seconds=0)
            if chain_payload.get("available"):
                calls, puts = cboe_chain_to_frames(chain_payload)
                if not calls.empty and not puts.empty:
                    return calls, puts, "live:cboe_option_chain"
        chain = stock.option_chain(expiry)
        return chain.calls.copy(), chain.puts.copy(), "live:option_chain"

    chain_dir = ensure_cache_dir("option_chains", cache_symbol(ticker), expiry)
    calls_path = os.path.join(chain_dir, "calls.csv")
    puts_path = os.path.join(chain_dir, "puts.csv")
    meta_path = os.path.join(chain_dir, "meta.json")
    ttl = int(float(CONFIG["option_chain_cache_ttl_minutes"]) * 60)
    if cache_is_fresh(meta_path, ttl) and os.path.exists(calls_path) and os.path.exists(puts_path):
        calls = read_csv_cache(calls_path)
        puts = read_csv_cache(puts_path)
        if calls is not None and puts is not None and not calls.empty and not puts.empty:
            return calls, puts, "cache:option_chain"

    chain_source = "live:option_chain"
    if CONFIG.get("cboe_option_chain_enabled", True):
        chain_payload = get_cboe_option_chain(ticker, expiry, ttl_seconds=ttl)
        if chain_payload.get("available"):
            calls, puts = cboe_chain_to_frames(chain_payload)
            if not calls.empty and not puts.empty:
                chain_source = "live:cboe_option_chain" if not chain_payload.get("cache_hit") else "cache:cboe_option_chain"
            else:
                calls = pd.DataFrame()
                puts = pd.DataFrame()
        else:
            calls = pd.DataFrame()
            puts = pd.DataFrame()
    else:
        calls = pd.DataFrame()
        puts = pd.DataFrame()
    if calls.empty or puts.empty:
        chain = stock.option_chain(expiry)
        calls = chain.calls.copy()
        puts = chain.puts.copy()
        chain_source = "live:option_chain"
    write_csv_cache(calls, calls_path)
    write_csv_cache(puts, puts_path)
    try:
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": datetime.now().isoformat(), "ticker": ticker, "expiry": expiry, "source": chain_source}, f, indent=2)
    except Exception as exc:
        print(f"[WARN] could not write option chain cache for {ticker}: {exc}")
    return calls, puts, chain_source


def cached_fundamentals(stock: yf.Ticker, ticker: str) -> tuple[dict, str]:
    """Fetch a small fundamental snapshot for display, with a local TTL cache."""
    empty = {
        "market_cap": None,
        "trailing_pe": None,
        "forward_pe": None,
        "shares_outstanding": None,
    }
    if not CONFIG.get("cache_enabled", True):
        return fetch_fundamentals_live(stock, ticker), "live:fundamentals"

    path = os.path.join(ensure_cache_dir("fundamentals"), f"{cache_symbol(ticker)}.json")
    ttl = int(float(CONFIG.get("fundamental_cache_ttl_hours", 24)) * 3600)
    if cache_is_fresh(path, ttl):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if isinstance(cached, dict):
                return {**empty, **cached}, "cache:fundamentals"
        except Exception:
            pass

    snapshot = {**empty, **fetch_fundamentals_live(stock, ticker)}
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": datetime.now().isoformat(), **snapshot}, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"[WARN] could not write fundamentals cache for {ticker}: {exc}")
    return snapshot, "live:fundamentals"


def fetch_fundamentals_live(stock: yf.Ticker, ticker: str) -> dict:
    """Best-effort market cap and PE snapshot from yfinance.

    yfinance fundamentals are not guaranteed for every small cap.  We keep the
    fields nullable and let the UI show "--" when Yahoo does not provide them.
    """
    data: dict = {}
    try:
        fast = getattr(stock, "fast_info", {}) or {}
        if hasattr(fast, "get"):
            data["market_cap"] = finite_float(fast.get("market_cap"))
            data["shares_outstanding"] = finite_float(fast.get("shares"))
    except Exception:
        pass
    try:
        info = stock.get_info() if hasattr(stock, "get_info") else stock.info
        if isinstance(info, dict):
            data["market_cap"] = data.get("market_cap") or finite_float(info.get("marketCap"))
            data["trailing_pe"] = finite_float(info.get("trailingPE"))
            data["forward_pe"] = finite_float(info.get("forwardPE"))
            data["shares_outstanding"] = data.get("shares_outstanding") or finite_float(info.get("sharesOutstanding"))
    except Exception as exc:
        print(f"[WARN] fundamentals fetch failed for {ticker}: {exc}")
    return {
        "market_cap": data.get("market_cap"),
        "trailing_pe": data.get("trailing_pe"),
        "forward_pe": data.get("forward_pe"),
        "shares_outstanding": data.get("shares_outstanding"),
    }


def fetch_ticker(ticker: str, target_expiry: str) -> dict:
    stock = yf.Ticker(ticker)
    hist, history_source = cached_history(stock, ticker, CONFIG["history_period"])
    if hist.empty or len(hist) < 31:
        raise ValueError("insufficient history")
    close = hist["Close"].values
    spot = float(close[-1])
    expiries, options_source = cached_options(stock, ticker)
    if not expiries:
        raise ValueError("no options")
    target = datetime.strptime(target_expiry, "%Y-%m-%d")
    expiry = min(expiries, key=lambda x: abs((datetime.strptime(x, "%Y-%m-%d") - target).days))
    dte = max((datetime.strptime(expiry, "%Y-%m-%d") - datetime.now()).days, 1)
    calls, puts, chain_source = cached_option_chain(stock, ticker, expiry)
    for frame in [calls, puts]:
        frame["strike"] = pd.to_numeric(frame["strike"], errors="coerce")
        frame["bid"] = pd.to_numeric(frame["bid"], errors="coerce").fillna(0.0)
        frame["ask"] = pd.to_numeric(frame["ask"], errors="coerce").fillna(0.0)
        frame["lastPrice"] = pd.to_numeric(frame.get("lastPrice", 0), errors="coerce").fillna(0.0)
        frame["impliedVolatility"] = pd.to_numeric(frame.get("impliedVolatility", 0), errors="coerce").fillna(0.0)
        frame["openInterest"] = pd.to_numeric(frame.get("openInterest", 0), errors="coerce").fillna(0)
        frame["volume"] = pd.to_numeric(frame.get("volume", 0), errors="coerce").fillna(0)
    calls = calls.dropna(subset=["strike"]).sort_values("strike").reset_index(drop=True)
    puts = puts.dropna(subset=["strike"]).sort_values("strike").reset_index(drop=True)
    iv = compute_atm_iv(spot, calls, puts, dte)
    gamma_exposure = gamma_exposure_summary(calls, puts, spot, iv or compute_hv(close), dte)
    fundamentals, fundamentals_source = cached_fundamentals(stock, ticker)
    recent_frame = hist.tail(35).copy()
    recent_dates = pd.to_datetime(recent_frame["Date"], errors="coerce")
    recent_frame["Date"] = recent_dates.dt.strftime("%Y-%m-%d")
    recent_ohlcv = recent_frame[["Date", "Open", "High", "Low", "Close", "Volume"]].round(4).to_dict(orient="records")
    pullback_rejection, pullback_confirmation = detect_pullback_setup(recent_ohlcv, ticker)
    return {
        "spot": spot,
        "close_prices": close,
        "calls": calls,
        "puts": puts,
        "iv": iv,
        "gamma_exposure": gamma_exposure,
        "fundamentals": fundamentals,
        "recent_ohlcv": recent_ohlcv,
        "price_as_of": recent_ohlcv[-1]["Date"] if recent_ohlcv else None,
        "pullback_rejection": pullback_rejection,
        "pullback_confirmation": pullback_confirmation,
        "dte": dte,
        "expiry_used": expiry,
        "cache_sources": {
            "history": history_source,
            "options": options_source,
            "option_chain": chain_source,
            "fundamentals": fundamentals_source,
            "cache_root": cache_root(),
        },
    }


def build_portfolio(results: list[dict]) -> list[dict]:
    budget = CONFIG["total_budget"]
    remaining = budget
    max_pos = budget * CONFIG["max_position_pct"]
    chosen = []
    theme_counts: dict[str, int] = {}

    def corr_ok(candidate: dict) -> bool:
        a = candidate.get("recent_returns") or []
        if len(a) < 20:
            return True
        for existing in chosen:
            b = existing.get("recent_returns") or []
            n = min(len(a), len(b))
            if n < 20:
                continue
            corr = float(np.corrcoef(a[-n:], b[-n:])[0, 1])
            if np.isfinite(corr) and corr >= CONFIG["max_pair_corr"]:
                return False
        return True

    for r in results:
        max_loss = float(r.get("max_loss", 0.0))
        is_credit = r["primary_strategy"] in {"bull_put_spread", "bear_call_spread", "iron_condor"}
        theme = r.get("theme", "other")
        if not r.get("tradable") or r["final_score"] < 5.0 or max_loss <= 0 or max_loss > max_pos:
            continue
        if not r.get("within_preferred_position_size", max_loss <= max_pos):
            continue
        if is_credit and r.get("roc", 0.0) < CONFIG["min_credit_roc"]:
            continue
        if r.get("expected_value", 0.0) <= 0:
            continue
        if theme != "other" and theme_counts.get(theme, 0) >= CONFIG["max_same_theme"]:
            continue
        if not corr_ok(r):
            continue
        if max_loss <= remaining:
            chosen.append({**r, "estimated_capital": round(max_loss, 2)})
            theme_counts[theme] = theme_counts.get(theme, 0) + 1
            remaining -= max_loss
        if len(chosen) >= 5:
            break
    return chosen


def research_summary(results: list[dict]) -> dict:
    tiers: dict[str, int] = {}
    themes: dict[str, int] = {}
    for r in results:
        tiers[str(r.get("research_tier", "未分层"))] = tiers.get(str(r.get("research_tier", "未分层")), 0) + 1
        theme = str(r.get("theme", "other"))
        themes[theme] = themes.get(theme, 0) + 1
    crowded = [
        {"theme": theme, "count": count, "risk": "high" if count >= 5 else "medium"}
        for theme, count in sorted(themes.items(), key=lambda x: -x[1])
        if theme != "other" and count >= 3
    ]
    return {
        "tier_counts": tiers,
        "theme_counts": themes,
        "theme_crowding": crowded,
        "manual_review_checklist": [
            "是否存在财报/FDA/并购/增发/诉讼/评级调整等事件",
            "推荐腿是否真实存在，bid/ask 是否可接受",
            "组合限价是否能成交，是否需要放弃而不是追价",
            "单笔最大亏损是否低于账户预设上限",
            "是否同主题持仓过多，避免 AI/半导体/加密/Meme 过度集中",
            "到期日前 5-7 天是否有明确处理计划",
            "若价格触及观察价位，是否立即复核而不是机械持有",
        ],
    }


def write_pdf_pretty(output: dict, path: str) -> bool:
    """Render the concise Chinese options order report expected by the UI."""
    try:
        from weasyprint import HTML
    except Exception as exc:
        print(f"[WARN] PDF renderer unavailable: {exc}")
        return False

    results = output.get("results", [])
    tradable = [r for r in results if r.get("tradable")]
    portfolio = output.get("portfolio", [])
    affordable = [r for r in tradable if r.get("affordable_one_contract")]
    ranked_source = tradable
    trade_source = portfolio or affordable[:5] or tradable[:5]

    def money(v: object) -> str:
        try:
            return f"${float(v):,.0f}"
        except Exception:
            return "-"

    def pct(v: object) -> str:
        try:
            return f"{float(v) * 100:.1f}%"
        except Exception:
            return "-"

    def strategy_cn(name: str) -> str:
        return {
            "single_call": "Single - 买入看涨期权",
            "single_put": "Single - 买入看跌期权",
            "bull_put_spread": "牛市看跌价差 Put Credit Spread",
            "bear_call_spread": "熊市看涨价差 Call Credit Spread",
            "bull_call_spread": "牛市看涨价差 Bull Call Spread",
            "bear_put_spread": "熊市看跌价差 Bear Put Spread",
            "long_straddle": "跨式组合 Long Straddle",
            "butterfly": "蝶式价差 Butterfly",
            "condor": "秃鹰价差 Condor",
            "iron_condor": "Iron Condor",
        }.get(name, name)

    def price_text(r: dict) -> str:
        credit = float(r.get("credit", 0) or 0)
        debit = float(r.get("debit", 0) or 0)
        return f"${credit:.2f} Cr" if credit > 0 else f"${debit:.2f} Dr"

    def gamma_cell(r: dict) -> str:
        gex = r.get("gamma_exposure") or {}
        if not gex.get("available"):
            return "n/a"
        flip = gex.get("gamma_flip")
        flip_text = "-" if flip is None else f"${float(flip):.2f}"
        return (
            f"{str(gex.get('risk_level', '-')).upper()} / {str(gex.get('regime', '-'))}"
            f"<br>Flip {html.escape(flip_text)}"
            f"<br>Net {float(gex.get('net_gex', 0) or 0) / 1_000_000:.1f}M"
        )

    def gamma_html(r: dict) -> str:
        gex = r.get("gamma_exposure") or {}
        if not gex.get("available"):
            return '<div class="gex-note">Gamma Exposure: unavailable.</div>'
        return (
            '<div class="gex-note">'
            f"Gamma Exposure: {html.escape(str(gex.get('risk_level', '-')).upper())} / "
            f"{html.escape(str(gex.get('regime', '-')))}; "
            f"Flip={html.escape(str(gex.get('gamma_flip', '-')))}. "
            f"{html.escape(str(gex.get('interpretation_cn') or gex.get('limitations') or ''))}"
            "</div>"
        )

    def source_html(r: dict) -> str:
        sources = r.get("cache_sources") or {}
        if not sources:
            return '<div class="gex-note">Data source: not recorded.</div>'
        labels = {"history": "history", "options": "expiries", "option_chain": "option chain", "fundamentals": "fundamentals"}
        parts = [f"{labels.get(k, k)}={v}" for k, v in sources.items() if v]
        return f'<div class="gex-note">Data source: {html.escape("; ".join(parts) or "not recorded")}</div>'

    def order_action(r: dict) -> str:
        credit = float(r.get("credit", 0) or 0)
        debit = float(r.get("debit", 0) or 0)
        if credit > 0:
            return f"SELL combo @ Limit >= ${credit:.2f} Credit"
        return f"BUY combo @ Limit <= ${debit:.2f} Debit"

    def signed_money(v: object) -> str:
        try:
            value = float(v)
            sign = "+" if value >= 0 else "-"
            return f"{sign}${abs(value):,.0f}"
        except Exception:
            return "-"

    def guide_rows(r: dict) -> str:
        guide = r.get("firstrade_order_guide") or firstrade_order_guide(r)
        fields = [
            ("策略", guide.get("strategy_dropdown", "-")),
            ("示例数量", guide.get("example_quantity", 1)),
            ("建议数量", guide.get("recommended_contracts", r.get("recommended_contracts", 0))),
            ("Net Price", f"{guide.get('net_price_type', '-')} ${float(guide.get('net_price', 0) or 0):.2f}"),
            ("其他条件", guide.get("other_conditions", "None")),
        ]
        return "".join(
            f"<tr><td>{html.escape(str(label))}</td><td>{html.escape(str(value))}</td></tr>"
            for label, value in fields
        )

    def leg_rows(r: dict) -> str:
        guide = r.get("firstrade_order_guide") or firstrade_order_guide(r)
        rows = []
        for leg in guide.get("legs", []):
            rows.append(
                "<tr>"
                f"<td>{html.escape(str(leg.get('action', '-')))}</td>"
                f"<td>{html.escape(str(leg.get('qty', 1)))}</td>"
                f"<td>{html.escape(str(leg.get('expiry', '-')))}</td>"
                f"<td>${float(leg.get('strike', 0) or 0):.2f}</td>"
                f"<td>{html.escape(str(leg.get('type', '-')))}</td>"
                "</tr>"
            )
        return "".join(rows) or '<tr><td colspan="5">No leg detail available</td></tr>'

    def scenario_rows(r: dict) -> str:
        label_map = {
            "current": "当前价",
            "1sigma_down": "1σ 下沿",
            "breakeven": "盈亏平衡点",
            "short_strike": "风险行权价",
            "1sigma_up": "1σ 上沿",
        }
        rows = []
        for item in r.get("payoff_scenarios") or payoff_scenarios(r):
            rows.append(
                "<tr>"
                f"<td>{html.escape(label_map.get(str(item.get('label', '-')), str(item.get('label', '-'))))}</td>"
                f"<td>${float(item.get('underlying_price', 0) or 0):.2f}</td>"
                f"<td>{float(item.get('price_move_pct', 0) or 0):+.1f}%</td>"
                f"<td>{signed_money(item.get('estimated_pnl_at_expiry', 0))}</td>"
                f"<td>{float(item.get('pnl_pct_of_max_loss', 0) or 0):+.1f}%</td>"
                "</tr>"
            )
        return "".join(rows) or '<tr><td colspan="5">No payoff scenario available</td></tr>'

    def single_only_rows(r: dict) -> str:
        guide = r.get("single_only_fallback") or single_only_fallback_guide(r)
        fields = [
            ("状态", {"native": "原生 Single", "proxy": "单腿替代", "not_recommended": "不建议"}.get(str(guide.get("status", "-")), guide.get("status", "-"))),
            ("策略", guide.get("strategy_dropdown", "Single")),
            ("交易类别", guide.get("transaction", "-")),
            ("数量", guide.get("quantity", 1)),
            ("到期日", guide.get("expiry", "-")),
            ("履约价", "-" if guide.get("strike") is None else f"${float(guide.get('strike', 0) or 0):.2f}"),
            ("期权类型", guide.get("option_type", "-")),
            ("限价", guide.get("limit_reference", "-")),
            ("有效期", guide.get("duration", "当日有效")),
            ("其他条件", guide.get("other_conditions", "None")),
        ]
        return "".join(
            f"<tr><td>{html.escape(str(label))}</td><td>{html.escape(str(value))}</td></tr>"
            for label, value in fields
        )

    def single_only_class(r: dict) -> str:
        status = str((r.get("single_only_fallback") or {}).get("status", ""))
        return "single-warn" if status == "not_recommended" else "single-ok"

    def distance(spot: float, price: float, direction: str) -> str:
        if spot <= 0:
            return "-"
        raw = (spot - price) / spot if direction == "down" else (price - spot) / spot
        return f"{max(raw, 0) * 100:.1f}%"

    def knockout_text(r: dict) -> str:
        strategy = str(r.get("primary_strategy", ""))
        spot = float(r.get("spot", 0) or 0)
        max_loss = money(r.get("max_loss", 0))
        if strategy == "bull_put_spread":
            short = float(r.get("short_strike", 0) or 0)
            breakeven = float(r.get("breakeven", short) or short)
            long = float(r.get("long_strike", 0) or 0)
            return f"跌破 ${short:.2f} 进入风险区；硬敲出参考 ${breakeven:.2f}，距现价 {distance(spot, breakeven, 'down')}；跌破保护腿 ${long:.2f} 接近最大亏损 {max_loss}。"
        if strategy == "bear_call_spread":
            short = float(r.get("short_strike", 0) or 0)
            breakeven = float(r.get("breakeven", short) or short)
            long = float(r.get("long_strike", 0) or 0)
            return f"涨破 ${short:.2f} 进入风险区；硬敲出参考 ${breakeven:.2f}，距现价 {distance(spot, breakeven, 'up')}；涨破保护腿 ${long:.2f} 接近最大亏损 {max_loss}。"
        if strategy == "iron_condor":
            lower = float(r.get("lower_breakeven", r.get("put_short", 0)) or 0)
            upper = float(r.get("upper_breakeven", r.get("call_short", 0)) or 0)
            return f"下破 ${lower:.2f} 或上破 ${upper:.2f} 视为敲出预警，距现价分别 {distance(spot, lower, 'down')} / {distance(spot, upper, 'up')}；边界外继续持有会快速接近最大亏损 {max_loss}。"
        if strategy == "bull_call_spread":
            long = float(r.get("long_strike", 0) or 0)
            breakeven = float(r.get("breakeven", long) or long)
            short = float(r.get("short_strike", 0) or 0)
            return f"回落并持续低于买入腿 ${long:.2f} 视为动量失效；盈亏平衡 ${breakeven:.2f}，距现价 {distance(spot, breakeven, 'up')}；涨至 ${short:.2f} 附近接近最大盈利。"
        if strategy == "bear_put_spread":
            long = float(r.get("long_strike", 0) or 0)
            breakeven = float(r.get("breakeven", long) or long)
            short = float(r.get("short_strike", 0) or 0)
            return f"反弹并持续高于买入腿 ${long:.2f} 视为下跌逻辑失效；盈亏平衡 ${breakeven:.2f}，距现价 {distance(spot, breakeven, 'down')}；跌至 ${short:.2f} 附近接近最大盈利。"
        return "该策略暂无自动敲出边界，请以权利金止损、盈亏平衡点和趋势失效条件管理。"

    def gamma_text(r: dict) -> str:
        gex = r.get("gamma_exposure") or {}
        if not gex.get("available"):
            return "GEX: n/a"
        flip = gex.get("gamma_flip")
        flip_text = "-" if flip is None else f"${float(flip):.2f}"
        return (
            f"{str(gex.get('risk_level', '-')).upper()} / {str(gex.get('regime', '-'))}"
            f"<br>Flip {html.escape(flip_text)}"
            f"<br>Net {float(gex.get('net_gex', 0) or 0) / 1_000_000:.1f}M"
        )

    def gamma_note(r: dict) -> str:
        gex = r.get("gamma_exposure") or {}
        if not gex.get("available"):
            return "Gamma Exposure: unavailable."
        return (
            f"Gamma Exposure: {str(gex.get('risk_level', '-')).upper()} / {str(gex.get('regime', '-'))}; "
            f"Flip={gex.get('gamma_flip', '-')}; "
            f"{gex.get('interpretation_cn') or gex.get('limitations') or ''}"
        )

    def source_note(r: dict) -> str:
        sources = r.get("cache_sources") or {}
        if not sources:
            return "Data source: not recorded."
        labels = {
            "history": "history",
            "options": "expiries",
            "option_chain": "option chain",
            "fundamentals": "fundamentals",
        }
        parts = [f"{labels.get(k, k)}={v}" for k, v in sources.items() if v]
        return "Data source: " + "; ".join(parts) if parts else "Data source: not recorded."

    def ranking_row(r: dict, rank: int) -> str:
        snap = r.get("market_snapshot") or {}
        spark = str(snap.get("sparkline_points", ""))
        memberships = " / ".join(r.get("universe_memberships") or ["-"])
        spark_html = (
            f'<svg class="rank-spark" viewBox="0 0 160 40" preserveAspectRatio="none">'
            f'<polyline fill="none" stroke="#0f766e" stroke-width="2" points="{html.escape(spark)}" />'
            f'</svg>'
            if spark else "-"
        )
        return (
            "<tr>"
            f"<td>{rank}</td>"
            f"<td>{html.escape(str(r.get('ticker', '')))}</td>"
            f"<td>{html.escape(memberships)}</td>"
            f"<td>{spark_html}</td>"
            f"<td>${float(snap.get('spot', r.get('spot', 0)) or 0):.2f}</td>"
            f"<td>{html.escape(strategy_cn(str(r.get('primary_strategy', ''))))}</td>"
            f"<td>{pct(r.get('pop', 0))}</td>"
            f"<td>{float(r.get('roc', 0) or 0) * 100:.1f}%</td>"
            f"<td>{money(r.get('expected_value', 0))}</td>"
            f"<td>{money(r.get('max_loss', 0))}</td>"
            f"<td>{gamma_text(r)}</td>"
            f"<td>{html.escape(str(r.get('liquidity_grade', '-')))} / {float(r.get('final_score', 0) or 0):.1f}</td>"
            "</tr>"
        )

    def trade_card(r: dict, idx: int) -> str:
        guide = r.get("firstrade_order_guide") or firstrade_order_guide(r)
        return f"""
        <div class="trade">
          <div class="trade-title">Trade {idx} - {html.escape(str(r.get('ticker', '')))} | {html.escape(strategy_cn(str(r.get('primary_strategy', ''))))}</div>
          <div class="legs">{html.escape(str(r.get('strategy_desc', '-')))}</div>
          <div class="trade-grid">
            <span>Price: <b>{price_text(r)}</b></span>
            <span>POP: <b>{pct(r.get('pop', 0))}</b></span>
            <span>Max Profit: <b>{money(r.get('max_profit', 0))}</b></span>
            <span>Max Loss: <b>{money(r.get('max_loss', 0))}</b></span>
            <span>ROC: <b>{float(r.get('roc', 0) or 0) * 100:.1f}%</b></span>
            <span>Liquidity: <b>{html.escape(str(r.get('liquidity_grade', '-')))} ({float(r.get('liquidity_score', 0) or 0):.2f})</b></span>
          </div>
          <div class="knockout">Risk: {html.escape(knockout_text(r))}</div>
          <div class="mini-note">{gamma_note(r)}</div>
          <div class="mini-note">{html.escape(source_note(r))}</div>
          <div class="order-line">{order_action(r)}；使用组合限价单，先检视/预览，不要拆腿，不要市价单。</div>
          <div class="guide-grid">
            <div>
              <div class="mini-title">Firstrade 填单指引</div>
              <table class="mini-table"><tbody>{guide_rows(r)}</tbody></table>
            </div>
            <div>
              <div class="mini-title">期权腿填写</div>
              <table class="mini-table"><thead><tr><th>买/卖</th><th>数量</th><th>到期日</th><th>履约价</th><th>类型</th></tr></thead><tbody>{leg_rows(r)}</tbody></table>
            </div>
          </div>
          <div class="mini-note">{html.escape(str(guide.get('submit_note', '先检视/预览。')))} 敲出/复核价不是券商自动止损单，只是本报告给你的人工复核触发线。</div>
          <div class="{single_only_class(r)}">
            <div class="mini-title">如果账户只能做 Single</div>
            <table class="mini-table"><tbody>{single_only_rows(r)}</tbody></table>
            <div class="single-note">{html.escape(str((r.get('single_only_fallback') or {}).get('note', '')))}</div>
          </div>
          <div class="mini-title">股价检查点与到期预估盈亏</div>
          <table class="scenario-table"><thead><tr><th>检查点</th><th>股价</th><th>涨跌幅</th><th>预估盈亏</th><th>占最大亏损</th></tr></thead><tbody>{scenario_rows(r)}</tbody></table>
        </div>"""

    def order_row(r: dict) -> str:
        return (
            "<tr>"
            f"<td>{html.escape(str(r.get('ticker', '')))}</td>"
            f"<td>{html.escape(str(r.get('strategy_desc', '-')))}</td>"
            f"<td>{order_action(r)}</td>"
            "<td>止盈：赚到权利金或最大盈利的 70-80% 平仓；止损：亏损达到最大亏损 50-60% 评估平仓；到期前 5-7 天不扛 Gamma。</td>"
            "</tr>"
        )

    top_rows = "\n".join(ranking_row(r, i) for i, r in enumerate(ranked_source[:10], 1))
    if not top_rows:
        top_rows = '<tr><td colspan="12">没有标的通过真实 bid/ask 可执行期权腿验证。</td></tr>'
    trade_cards = "\n".join(trade_card(r, i) for i, r in enumerate(trade_source, 1))
    if not trade_cards:
        trade_cards = '<div class="note">没有可执行期权组合。报告不会为缺少真实 bid/ask 或无券商可交易期权链的标的生成下单指引。</div>'
    order_rows = "\n".join(order_row(r) for r in trade_source)
    if not order_rows:
        order_rows = '<tr><td colspan="4">没有可执行组合。</td></tr>'
    total_credit = sum(float(r.get("credit", 0) or 0) * 100 for r in trade_source)
    total_debit = sum(float(r.get("debit", 0) or 0) * 100 for r in trade_source)
    total_max_profit = sum(float(r.get("max_profit", 0) or 0) for r in trade_source)
    total_max_loss = sum(float(r.get("max_loss", 0) or 0) for r in trade_source)
    any_win = 1.0
    for r in trade_source:
        any_win *= 1.0 - float(r.get("pop", 0) or 0)
    combo_any_win = 1.0 - any_win if trade_source else 0.0
    generated = html.escape(str(output.get("timestamp", "")))
    expiry = html.escape(str(output.get("target_expiry", "")))
    doc = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    @page {{ size: A4; margin: 12mm; }}
    body {{ font-family: "Noto Sans CJK SC", "Microsoft YaHei", Arial, sans-serif; color: #102033; font-size: 10.5px; line-height: 1.45; }}
    .cover {{ background: #0f172a; color: white; border-radius: 10px; padding: 18px 20px; margin-bottom: 12px; }}
    h1 {{ margin: 0 0 6px; font-size: 23px; }}
    .sub {{ color: #dbeafe; }}
    .cards {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin: 10px 0 12px; }}
    .card {{ border: 1px solid #d8e2ef; border-radius: 7px; padding: 8px; background: #f8fafc; }}
    .label {{ color: #64748b; font-size: 8px; text-transform: uppercase; }}
    .value {{ font-size: 14px; font-weight: 700; margin-top: 3px; }}
    h2 {{ color: #0f172a; font-size: 14px; margin: 14px 0 7px; border-left: 4px solid #0f766e; padding-left: 7px; }}
    table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
    th {{ background: #164e63; color: white; font-size: 8px; padding: 6px 5px; text-align: left; }}
    td {{ border-bottom: 1px solid #e2e8f0; padding: 5px; vertical-align: top; word-break: break-word; }}
    tr:nth-child(even) td {{ background: #f8fafc; }}
    .note {{ background: #fff7ed; border: 1px solid #fed7aa; color: #7c2d12; border-radius: 8px; padding: 8px 10px; margin: 8px 0 12px; }}
    .trade {{ border: 1px solid #d8e2ef; border-radius: 8px; padding: 9px 10px; margin: 8px 0; break-inside: avoid; }}
    .trade-title {{ font-size: 12px; font-weight: 700; color: #0f172a; margin-bottom: 4px; }}
    .ranking-table th:nth-child(1), .ranking-table td:nth-child(1) {{ width: 26px; }}
    .ranking-table th:nth-child(2), .ranking-table td:nth-child(2) {{ width: 42px; }}
    .ranking-table th:nth-child(3), .ranking-table td:nth-child(3) {{ width: 62px; }}
    .ranking-table th:nth-child(4), .ranking-table td:nth-child(4) {{ width: 58px; overflow: hidden; }}
    .ranking-table th:nth-child(5), .ranking-table td:nth-child(5) {{ width: 44px; white-space: nowrap; }}
    .rank-spark {{ width: 58px; height: 18px; display: block; background: transparent; border: 0; overflow: hidden; }}
    .legs {{ font-family: Consolas, "Microsoft YaHei", monospace; color: #334155; margin-bottom: 6px; }}
    .trade-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 4px 10px; }}
    .knockout {{ background: #fef2f2; border: 1px solid #fecaca; border-radius: 7px; color: #7f1d1d; margin-top: 7px; padding: 7px 8px; }}
    .gex-note {{ background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 7px; color: #1e3a8a; margin-top: 7px; padding: 7px 8px; }}
    .order-line {{ margin-top: 6px; color: #0f766e; font-weight: 700; }}
    .guide-grid {{ display: grid; grid-template-columns: 0.72fr 1.28fr; gap: 8px; margin-top: 8px; }}
    .mini-title {{ margin: 7px 0 4px; font-size: 9px; font-weight: 700; color: #334155; text-transform: uppercase; }}
    .mini-note {{ margin-top: 6px; padding: 6px 7px; border-radius: 6px; background: #eff6ff; border: 1px solid #bfdbfe; color: #1e3a8a; }}
    .single-ok, .single-warn {{ margin-top: 7px; padding: 7px; border-radius: 7px; }}
    .single-ok {{ background: #f0fdf4; border: 1px solid #bbf7d0; color: #14532d; }}
    .single-warn {{ background: #fff7ed; border: 1px solid #fed7aa; color: #7c2d12; }}
    .single-note {{ margin-top: 5px; font-size: 8.5px; }}
    .mini-table th, .mini-table td, .scenario-table th, .scenario-table td {{ font-size: 8px; padding: 4px; }}
    .mini-table th, .scenario-table th {{ background: #334155; }}
    .scenario-table {{ margin-top: 2px; }}
    .rules {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; }}
    .rule {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 7px; padding: 8px; }}
  </style>
</head>
<body>
  <section class="cover">
    <h1>期权组合下单胜率排名与执行方案</h1>
    <div class="sub">到期日: {expiry} | 资金: ${CONFIG['total_budget']:,.0f} | 生成时间: {generated}</div>
  </section>
  <section class="cards">
    <div class="card"><div class="label">标的数</div><div class="value">{output.get('ticker_count', 0)}</div></div>
    <div class="card"><div class="label">有效策略</div><div class="value">{len(results)}</div></div>
    <div class="card"><div class="label">可构建组合</div><div class="value">{len(tradable)}</div></div>
    <div class="card"><div class="label">推荐下单</div><div class="value">{len(trade_source)}</div></div>
  </section>
  <div class="note">核心口径：POP 是模型胜率与历史障碍胜率的融合估计，不等同于实盘胜率；权利金若来自 yfinance 估算，实盘必须在券商端用实时 bid/ask 复核后再挂组合限价单。</div>
  <h2>策略评分排名 Top 10</h2>
  <table class="ranking-table"><thead><tr><th>排名</th><th>标的</th><th>来源池</th><th>走势</th><th>现价</th><th>最佳策略</th><th>胜率</th><th>ROC</th><th>期望值</th><th>最大亏损</th><th>GEX</th><th>流动性/评分</th></tr></thead><tbody>{top_rows}</tbody></table>
  <h2>具体交易构建</h2>
  {trade_cards}
  <h2>组合推荐</h2>
  <section class="cards">
    <div class="card"><div class="label">净收/付权利金</div><div class="value">${total_credit - total_debit:,.0f}</div></div>
    <div class="card"><div class="label">最大盈利</div><div class="value">${total_max_profit:,.0f}</div></div>
    <div class="card"><div class="label">最大亏损</div><div class="value">${total_max_loss:,.0f}</div></div>
    <div class="card"><div class="label">至少一单盈利</div><div class="value">{combo_any_win * 100:.1f}%</div></div>
  </section>
  <h2>一键下单速查</h2>
  <table><thead><tr><th>标的</th><th>具体组合</th><th>下单指令</th><th>管理规则</th></tr></thead><tbody>{order_rows}</tbody></table>
  <h2>日频交易执行守则</h2>
  <div class="rules">
    <div class="rule"><b>每日检查</b><br>价格 vs short strike/盈亏平衡点；IV 是否快速扩张；5 日趋势是否反向。</div>
    <div class="rule"><b>止盈</b><br>卖方价差赚到权利金 70-80% 平仓；买方价差达到最大盈利 70-80% 分批止盈。</div>
    <div class="rule"><b>止损</b><br>亏损达到最大亏损 50-60% 或价格触碰 short strike 附近，立即评估减仓或平仓。</div>
    <div class="rule"><b>到期管理</b><br>到期前 5-7 天处理，避免最后一周 Gamma 风险；不要硬扛到期。</div>
  </div>
  <h2>核心建议</h2>
  <div class="note">优先执行推荐组合中的前 3-5 单；全部使用组合限价单。若实时价格低于报告估算的 Credit 或高于报告估算的 Debit，取消等待，不追价。</div>
</body>
</html>"""
    HTML(string=doc, base_url=os.getcwd()).write_pdf(path)
    return True


def write_pdf(output: dict, path: str) -> bool:
    if write_pdf_pretty(output, path):
        return True
    try:
        from fpdf import FPDF
    except Exception:
        return write_pdf_weasyprint(output, path)
    pdf = FPDF()
    pdf.set_auto_page_break(True, 15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "Optimized Tradable Options Screening", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.multi_cell(0, 5, f"Generated: {output['timestamp']} | Expiry: {output['target_expiry']}")
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 8)
    headers = ["Ticker", "Strategy", "ModelPOP", "HistWin", "Blend", "Credit/Debit", "MaxLoss", "ROC", "EV", "Score"]
    widths = [16, 32, 16, 16, 14, 22, 18, 14, 16, 12]
    for w, h in zip(widths, headers):
        pdf.cell(w, 6, h, border=1)
    pdf.ln()
    pdf.set_font("Helvetica", "", 7)
    for r in output["results"][:25]:
        hist = r.get("historical_barrier_win_rate")
        vals = [
            r["ticker"],
            r["primary_strategy"][:18],
            f"{r.get('model_pop', r.get('pop', 0))*100:.1f}%",
            "-" if hist is None else f"{hist*100:.1f}%",
            f"{r.get('pop', 0)*100:.1f}%",
            f"{r.get('credit', 0):.2f}/{r.get('debit', 0):.2f}",
            f"${r.get('max_loss', 0):.0f}",
            f"{r.get('roc', 0)*100:.1f}%",
            f"${r.get('expected_value', 0):.0f}",
            f"{r.get('final_score', 0):.1f}",
        ]
        for w, v in zip(widths, vals):
            pdf.cell(w, 5, str(v), border=1)
        pdf.ln()
    pdf.output(path)
    return True


def write_research_brief(output: dict, path: str) -> bool:
    try:
        summary = output.get("research_summary", {})
        results = output.get("results", [])
        lines = [
            "# 短约期权研究筛查报告",
            "",
            f"- 生成时间: {output.get('timestamp', '')}",
            f"- 到期日: {output.get('target_expiry', '')}",
            f"- 股票池: {(output.get('universe') or {}).get('name', '')}",
            f"- 有效标的: {output.get('valid_tickers', 0)} / {output.get('ticker_count', 0)}",
            "",
            "## 研究分层统计",
        ]
        for tier, count in (summary.get("tier_counts") or {}).items():
            lines.append(f"- {tier}: {count}")
        lines.extend(["", "## 主题拥挤风险"])
        crowding = summary.get("theme_crowding") or []
        if crowding:
            for item in crowding:
                lines.append(f"- {item.get('theme')}: {item.get('count')} 只，风险 {item.get('risk')}")
        else:
            lines.append("- 暂未发现明显主题拥挤。")
        lines.extend(["", "## Top 10 研究清单"])
        for i, r in enumerate(results[:10], 1):
            pop_range = r.get("conservative_pop_range") or {}
            reasons = "；".join(r.get("rejection_reasons") or []) or "无明显硬伤"
            watch = ", ".join(f"{k}={v}" for k, v in (r.get("watch_levels") or {}).items())
            fail = "；".join(r.get("model_failure_conditions") or [])
            snap = r.get("market_snapshot") or {}
            family_lines = []
            for candidate in r.get("strategy_family_candidates") or []:
                family = candidate.get("strategy_family", candidate.get("status", ""))
                if candidate.get("status") == "not_available":
                    family_lines.append(f"{family}: 未构建")
                else:
                    family_lines.append(
                        f"{family}: {candidate.get('primary_strategy')} "
                        f"score={candidate.get('research_score')} "
                        f"cap=${float(candidate.get('capital_required', 0) or 0):,.0f} "
                        f"tier={candidate.get('research_tier', '')}"
                    )
            lines.extend([
                f"### {i}. {r.get('ticker')} - {r.get('research_tier', '')}",
                f"- 行情快照: 现价 ${float(snap.get('spot', 0) or 0):.2f}；30日涨跌 {float(snap.get('trend_30d_pct', 0) or 0):+.1f}%；IV/HV {snap.get('iv_hv_ratio', '-')}; 1σ区间 {snap.get('one_sigma_down', '-')} - {snap.get('one_sigma_up', '-')}",
                f"- 策略: {r.get('primary_strategy')} | {r.get('strategy_desc', '')}",
                f"- 策略族横向评估: {'；'.join(family_lines) if family_lines else '无'}",
                f"- 研究胜率区间: {float(pop_range.get('low', 0))*100:.1f}% - {float(pop_range.get('high', 0))*100:.1f}%；基础 POP {float(pop_range.get('base', 0))*100:.1f}%",
                f"- 资金约束: 一张资金占用 ${float(r.get('capital_required', 0) or 0):,.0f}，占账户 {float(r.get('capital_usage_pct', 0) or 0)*100:.1f}%；建议张数 {int(r.get('recommended_contracts', 0) or 0)}；{r.get('budget_note', '')}",
                f"- 评分: research={r.get('research_score')} final={r.get('final_score')} confidence={r.get('confidence_score')} stress={r.get('stress_score')}",
                f"- 否决/复核原因: {reasons}",
                f"- 观察价位: {watch}",
                f"- 模型失效条件: {fail}",
                "",
            ])
        lines.extend(["## 下单前人工复核清单"])
        for item in summary.get("manual_review_checklist") or []:
            lines.append(f"- [ ] {item}")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines).rstrip() + "\n")
        return True
    except Exception as exc:
        print(f"[WARN] could not write research brief: {exc}")
        return False


def write_pdf_weasyprint(output: dict, path: str) -> bool:
    try:
        from weasyprint import HTML
    except Exception as exc:
        print(f"[WARN] PDF renderer unavailable: {exc}")
        return False

    results = output.get("results", [])
    tradable = [r for r in results if r.get("tradable")]
    portfolio = output.get("portfolio", [])

    def money(v: object) -> str:
        try:
            return f"${float(v):,.0f}"
        except Exception:
            return "-"

    def pct(v: object) -> str:
        try:
            return f"{float(v) * 100:.1f}%"
        except Exception:
            return "-"

    def strategy_cn(name: str) -> str:
        mapping = {
            "single_call": "Single · Long Call",
            "single_put": "Single · Long Put",
            "bull_put_spread": "卖方 Put Spread",
            "bear_call_spread": "卖方 Call Spread",
            "bull_call_spread": "买方 Bull Call Spread",
            "bear_put_spread": "买方 Bear Put Spread",
            "long_straddle": "Straddle · Long Straddle",
            "butterfly": "Butterfly · Call Butterfly",
            "condor": "Condor · Call Condor",
            "iron_condor": "Iron Condor",
            "long_strangle": "Long Strangle",
        }
        return mapping.get(name, name)

    def price_text(r: dict) -> str:
        credit = float(r.get("credit", 0) or 0)
        debit = float(r.get("debit", 0) or 0)
        return f"${credit:.2f} Cr" if credit > 0 else f"${debit:.2f} Dr"

    def gamma_cell(r: dict) -> str:
        gex = r.get("gamma_exposure") or {}
        if not gex.get("available"):
            return "n/a"
        flip = gex.get("gamma_flip")
        flip_text = "-" if flip is None else f"${float(flip):.2f}"
        return (
            f"{str(gex.get('risk_level', '-')).upper()} / {str(gex.get('regime', '-'))}"
            f"<br>Flip {html.escape(flip_text)}"
            f"<br>Net {float(gex.get('net_gex', 0) or 0) / 1_000_000:.1f}M"
        )

    def gamma_html(r: dict) -> str:
        gex = r.get("gamma_exposure") or {}
        if not gex.get("available"):
            return '<div class="gex-note">Gamma Exposure: unavailable.</div>'
        return (
            '<div class="gex-note">'
            f"Gamma Exposure: {html.escape(str(gex.get('risk_level', '-')).upper())} / "
            f"{html.escape(str(gex.get('regime', '-')))}; "
            f"Flip={html.escape(str(gex.get('gamma_flip', '-')))}. "
            f"{html.escape(str(gex.get('interpretation_cn') or gex.get('limitations') or ''))}"
            "</div>"
        )

    def order_action(r: dict) -> str:
        credit = float(r.get("credit", 0) or 0)
        debit = float(r.get("debit", 0) or 0)
        if credit > 0:
            return f"SELL combo @ Limit >= ${credit:.2f} Credit"
        return f"BUY combo @ Limit <= ${debit:.2f} Debit"

    def knockout_html(r: dict) -> str:
        strategy = str(r.get("primary_strategy", ""))
        spot = float(r.get("spot", 0) or 0)
        max_loss = money(r.get("max_loss", 0))
        if spot <= 0:
            return '<div class="knockout">敲出价位：缺少现价，无法计算距离比例。</div>'

        def dist(price: float, direction: str) -> str:
            if direction == "down":
                return f"{max((spot - price) / spot, 0) * 100:.1f}%"
            return f"{max((price - spot) / spot, 0) * 100:.1f}%"

        if strategy == "bull_put_spread":
            warn = float(r.get("short_strike", 0) or 0)
            ko = float(r.get("breakeven", warn) or warn)
            long = float(r.get("long_strike", 0) or 0)
            return f'<div class="knockout">敲出/风控：跌破 ${warn:.2f} 进入风险区；硬敲出参考 ${ko:.2f}（距现价 {dist(ko, "down")}）。到期在该价附近约盈亏平衡；跌破保护腿 ${long:.2f} 接近最大亏损 {max_loss}。</div>'
        if strategy == "bear_call_spread":
            warn = float(r.get("short_strike", 0) or 0)
            ko = float(r.get("breakeven", warn) or warn)
            long = float(r.get("long_strike", 0) or 0)
            return f'<div class="knockout">敲出/风控：涨破 ${warn:.2f} 进入风险区；硬敲出参考 ${ko:.2f}（距现价 {dist(ko, "up")}）。到期在该价附近约盈亏平衡；涨破保护腿 ${long:.2f} 接近最大亏损 {max_loss}。</div>'
        if strategy == "iron_condor":
            lower = float(r.get("lower_breakeven", r.get("put_short", 0)) or 0)
            upper = float(r.get("upper_breakeven", r.get("call_short", 0)) or 0)
            pl = float(r.get("put_long", 0) or 0)
            cl = float(r.get("call_long", 0) or 0)
            return f'<div class="knockout">敲出/风控区间：下破 ${lower:.2f} 或上破 ${upper:.2f} 视为敲出预警（距现价分别 {dist(lower, "down")} / {dist(upper, "up")}）。到期在边界约盈亏平衡；跌破 ${pl:.2f} 或涨破 ${cl:.2f} 接近最大亏损 {max_loss}。</div>'
        if strategy == "bull_call_spread":
            invalid = float(r.get("long_strike", 0) or 0)
            be = float(r.get("breakeven", invalid) or invalid)
            short = float(r.get("short_strike", 0) or 0)
            return f'<div class="knockout">失效/风控：若价格回落并持续低于买入腿 ${invalid:.2f}，动量失效；盈亏平衡 ${be:.2f}（距现价 {dist(be, "up")}）。到期低于买入腿接近最大亏损 {max_loss}，涨至 ${short:.2f} 附近接近最大盈利。</div>'
        if strategy == "bear_put_spread":
            invalid = float(r.get("long_strike", 0) or 0)
            be = float(r.get("breakeven", invalid) or invalid)
            short = float(r.get("short_strike", 0) or 0)
            return f'<div class="knockout">失效/风控：若价格反弹并持续高于买入腿 ${invalid:.2f}，下跌逻辑失效；盈亏平衡 ${be:.2f}（距现价 {dist(be, "down")}）。到期高于买入腿接近最大亏损 {max_loss}，跌至 ${short:.2f} 附近接近最大盈利。</div>'
        return '<div class="knockout">敲出价位：该策略暂无自动风控边界，请以权利金止损和盈亏平衡点管理。</div>'

    def row_html(r: dict, rank: int) -> str:
        return (
            "<tr>"
            f"<td>{rank}</td>"
            f"<td>{html.escape(str(r.get('ticker', '')))}</td>"
            f"<td>{html.escape(strategy_cn(str(r.get('primary_strategy', ''))))}</td>"
            f"<td>{pct(r.get('pop', 0))}</td>"
            f"<td>{float(r.get('roc', 0)) * 100:.1f}%</td>"
            f"<td>{money(r.get('expected_value', 0))}</td>"
            f"<td>{money(r.get('max_loss', 0))}</td>"
            f"<td>{gamma_cell(r)}</td>"
            f"<td>{float(r.get('final_score', 0)):.1f}</td>"
            "</tr>"
        )

    def trade_card(r: dict, idx: int) -> str:
        max_profit = money(r.get("max_profit", 0))
        max_loss = money(r.get("max_loss", 0))
        return f"""
        <div class="trade">
          <div class="trade-title">Trade {idx} — {html.escape(str(r.get('ticker', '')))} · {html.escape(strategy_cn(str(r.get('primary_strategy', ''))))}</div>
          <div class="legs">{html.escape(str(r.get('strategy_desc', '-')))}</div>
          <div class="trade-grid">
            <span>Price: <b>{price_text(r)}</b></span>
            <span>POP: <b>{pct(r.get('pop', 0))}</b></span>
            <span>Max Profit: <b>{max_profit}</b></span>
            <span>Max Loss: <b>{max_loss}</b></span>
            <span>ROC: <b>{float(r.get('roc', 0) or 0) * 100:.1f}%</b></span>
            <span>Score: <b>{float(r.get('final_score', 0) or 0):.1f}</b></span>
          </div>
          {knockout_html(r)}
          {gamma_html(r)}
          {source_html(r)}
          <div class="order-line">{order_action(r)}；使用组合单一次性下单，不拆腿，不用市价单。</div>
        </div>"""

    def order_row(r: dict) -> str:
        return (
            "<tr>"
            f"<td>{html.escape(str(r.get('ticker', '')))}</td>"
            f"<td>{html.escape(str(r.get('strategy_desc', '-')))}</td>"
            f"<td>{order_action(r)}</td>"
            f"<td>止盈：赚到权利金/最大盈利 70-80% 平仓；止损：亏损达最大亏损 50-60% 评估平仓；到期前 5-7 天不扛 Gamma。</td>"
            "</tr>"
        )

    top_rows = "\n".join(row_html(r, i) for i, r in enumerate(results[:10], 1))
    trade_cards = "\n".join(trade_card(r, i) for i, r in enumerate(portfolio, 1)) or '<div class="note">没有组合候选通过资金、EV、ROC 与相关性约束。</div>'
    order_rows = "\n".join(order_row(r) for r in portfolio) or '<tr><td colspan="4">没有可执行组合。</td></tr>'
    total_credit = sum(float(r.get("credit", 0) or 0) * 100 for r in portfolio)
    total_debit = sum(float(r.get("debit", 0) or 0) * 100 for r in portfolio)
    total_max_profit = sum(float(r.get("max_profit", 0) or 0) for r in portfolio)
    total_max_loss = sum(float(r.get("max_loss", 0) or 0) for r in portfolio)
    any_win = 1.0
    for r in portfolio:
        any_win *= 1.0 - float(r.get("pop", 0) or 0)
    combo_any_win = 1.0 - any_win if portfolio else 0.0
    generated = html.escape(str(output.get("timestamp", "")))
    expiry = html.escape(str(output.get("target_expiry", "")))
    universe = output.get("universe", {})
    source = html.escape(str(universe.get("source", "")))
    doc = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    @page {{ size: A4; margin: 12mm; }}
    body {{ font-family: "Noto Sans CJK SC", "Microsoft YaHei", Arial, sans-serif; color: #102033; font-size: 10.5px; line-height: 1.45; }}
    .cover {{ background: #0f172a; color: white; border-radius: 10px; padding: 18px 20px; margin-bottom: 12px; }}
    h1 {{ margin: 0 0 6px; font-size: 23px; }}
    .sub {{ color: #dbeafe; }}
    .cards {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin: 10px 0 12px; }}
    .card {{ border: 1px solid #d8e2ef; border-radius: 7px; padding: 8px; background: #f8fafc; }}
    .label {{ color: #64748b; font-size: 8px; text-transform: uppercase; }}
    .value {{ font-size: 14px; font-weight: 700; margin-top: 3px; }}
    h2 {{ color: #0f172a; font-size: 14px; margin: 14px 0 7px; border-left: 4px solid #0f766e; padding-left: 7px; }}
    table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
    th {{ background: #164e63; color: white; font-size: 8px; padding: 6px 5px; text-align: left; }}
    td {{ border-bottom: 1px solid #e2e8f0; padding: 5px; vertical-align: top; word-break: break-word; }}
    tr:nth-child(even) td {{ background: #f8fafc; }}
    .note {{ background: #fff7ed; border: 1px solid #fed7aa; color: #7c2d12; border-radius: 8px; padding: 8px 10px; margin: 8px 0 12px; }}
    .trade {{ border: 1px solid #d8e2ef; border-radius: 8px; padding: 9px 10px; margin: 8px 0; break-inside: avoid; }}
    .trade-title {{ font-size: 12px; font-weight: 700; color: #0f172a; margin-bottom: 4px; }}
    .legs {{ font-family: Consolas, "Microsoft YaHei", monospace; color: #334155; margin-bottom: 6px; }}
    .trade-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 4px 10px; }}
    .knockout {{ background: #fef2f2; border: 1px solid #fecaca; border-radius: 7px; color: #7f1d1d; margin-top: 7px; padding: 7px 8px; }}
    .order-line {{ margin-top: 6px; color: #0f766e; font-weight: 700; }}
    .rules {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; }}
    .rule {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 7px; padding: 8px; }}
  </style>
</head>
<body>
  <section class="cover">
    <h1>期权组合下单胜率排名与执行方案</h1>
    <div class="sub">到期日: {expiry} | 资金: ${CONFIG['total_budget']:,.0f} | 生成时间: {generated}</div>
  </section>
  <section class="cards">
    <div class="card"><div class="label">标的数</div><div class="value">{output.get('ticker_count', 0)}</div></div>
    <div class="card"><div class="label">有效策略</div><div class="value">{len(results)}</div></div>
    <div class="card"><div class="label">可构建组合</div><div class="value">{len(tradable)}</div></div>
    <div class="card"><div class="label">推荐下单</div><div class="value">{len(portfolio)}</div></div>
  </section>
  <div class="note">核心口径：POP 为模型胜率/历史障碍胜率融合值；权利金若来自 yfinance 估算，实盘必须在券商端用实时 bid/ask 复核后再挂限价组合单。</div>
  <h2>策略评分排名 Top 10</h2>
  <table>
    <thead><tr><th>排名</th><th>标的</th><th>最佳策略</th><th>胜率</th><th>ROC</th><th>期望值</th><th>最大亏损</th><th>GEX</th><th>评分</th></tr></thead>
    <tbody>{top_rows}</tbody>
  </table>
  <h2>具体交易构建</h2>
  {trade_cards}
  <h2>组合推荐</h2>
  <section class="cards">
    <div class="card"><div class="label">净收/付</div><div class="value">${total_credit - total_debit:,.0f}</div></div>
    <div class="card"><div class="label">最大盈利</div><div class="value">${total_max_profit:,.0f}</div></div>
    <div class="card"><div class="label">最大亏损</div><div class="value">${total_max_loss:,.0f}</div></div>
    <div class="card"><div class="label">至少一单盈利</div><div class="value">{combo_any_win * 100:.1f}%</div></div>
  </section>
  <h2>一键下单速查</h2>
  <table><thead><tr><th>标的</th><th>具体组合</th><th>下单指令</th><th>管理规则</th></tr></thead><tbody>{order_rows}</tbody></table>
  <h2>日频交易执行守则</h2>
  <div class="rules">
    <div class="rule"><b>每日检查</b><br>价格 vs short strike/盈亏平衡点；IV 是否快速扩张；5日趋势是否反向。</div>
    <div class="rule"><b>止盈</b><br>卖方价差赚到权利金 70-80% 平仓；买方价差达到最大盈利 70-80% 分批止盈。</div>
    <div class="rule"><b>止损</b><br>亏损达到最大亏损 50-60% 或价格触及 short strike 附近，立即评估减仓/平仓。</div>
    <div class="rule"><b>到期管理</b><br>到期前 5-7 天处理，避免最后一周 Gamma 风险；不要裸扛到期。</div>
  </div>
  <h2>核心建议</h2>
  <div class="note">优先执行推荐组合中的前 3-5 单；全部使用组合限价单。若实时价格低于报告估算的 Credit 或高于报告估算的 Debit，取消等待，不追价。</div>
</body>
</html>"""
    HTML(string=doc, base_url=os.getcwd()).write_pdf(path)
    return True


@lru_cache(maxsize=1)
def _screen_result_cache_namespace() -> str:
    script_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
    settings = json.dumps({
        "expiry": CONFIG["target_expiry"],
        "family": CONFIG["strategy_family"],
        "budget": CONFIG["total_budget"],
        "estimated_quotes": CONFIG["allow_estimated_quotes"],
    }, sort_keys=True)
    config_hash = hashlib.sha256(settings.encode("utf-8")).hexdigest()[:8]
    return f"{script_hash}_{config_hash}"


def _screen_result_cache_path(ticker: str) -> Path:
    root = Path(ensure_cache_dir("screening_results", _screen_result_cache_namespace()))
    return root / f"{cache_symbol(ticker)}.json"


def _current_cached_close(ticker: str) -> tuple[str, float] | None:
    from market_calendar import most_recent_session

    frame, _ = get_daily_history(ticker, period=CONFIG["history_period"], allow_yfinance_fallback=False)
    if frame.empty:
        return None
    price_as_of = pd.Timestamp(frame.index.max()).date().isoformat()
    if price_as_of < most_recent_session().isoformat():
        return None
    return price_as_of, float(frame["Close"].iloc[-1])


def _read_session_screen_result(ticker: str) -> dict | None:
    try:
        current = _current_cached_close(ticker)
        if current is None:
            return None
        payload = json.loads(_screen_result_cache_path(ticker).read_text(encoding="utf-8"))
        result = payload["result"]
        if (payload.get("price_as_of") == current[0]
                and abs(float(result.get("spot") or 0) - current[1]) <= max(0.02, current[1] * 0.0005)):
            return dict(result)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        pass
    return None


def _screen_cache_json_default(value: object) -> object:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    return str(value)


def _write_session_screen_result(ticker: str, result: dict) -> None:
    try:
        current = _current_cached_close(ticker)
        if current is None or result.get("price_as_of") != current[0]:
            return
        path = _screen_result_cache_path(ticker)
        temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temp.write_text(json.dumps({"price_as_of": current[0], "result": result}, ensure_ascii=False, default=_screen_cache_json_default), encoding="utf-8")
        temp.replace(path)
    except (OSError, ValueError, TypeError):
        return


def main(
    tickers: list[str],
    output_json: str = "options_screening_results_v2.json",
    universe_source: str = "manual",
    memberships: Optional[dict[str, list[str]]] = None,
) -> dict:
    results = []
    errors = []
    memberships = memberships or universe_memberships(tickers, "手动输入")
    for i, ticker in enumerate(tickers, 1):
        ticker = ticker.strip().upper()
        print(f"[{i}/{len(tickers)}] {ticker}...", flush=True)
        try:
            result = _read_session_screen_result(ticker) if CONFIG["reuse_session_results"] else None
            if result is None:
                data = fetch_ticker(ticker, CONFIG["target_expiry"])
                result = evaluate_ticker(ticker, data)
                if CONFIG["reuse_session_results"]:
                    _write_session_screen_result(ticker, result)
            result["universe_memberships"] = memberships.get(ticker, ["未标注"])
            results.append(result)
        except Exception as exc:
            errors.append({"ticker": ticker, "error": str(exc)})
            print(f"  error: {exc}")
    results.sort(key=lambda r: (-r.get("research_score", r.get("final_score", 0)), -r.get("final_score", 0), -r.get("expected_value", -999999), r["ticker"]))
    portfolio = build_portfolio(results)
    summary = research_summary(results)
    output = {
        "framework_version": "4.0-liquidity-graded-actionable-trades",
        "timestamp": datetime.now().isoformat(),
        "target_expiry": CONFIG["target_expiry"],
        "config": CONFIG,
        "cache": {
            "enabled": bool(CONFIG.get("cache_enabled", True)),
            "root": cache_root(),
            "history_refresh_overlap_days": CONFIG["history_refresh_overlap_days"],
            "options_expiry_cache_ttl_hours": CONFIG["options_expiry_cache_ttl_hours"],
            "option_chain_cache_ttl_minutes": CONFIG["option_chain_cache_ttl_minutes"],
        },
        "supported_strategy_families": SUPPORTED_STRATEGY_FAMILIES,
        "universe": {
            "name": CONFIG["universe"],
            "source": universe_source,
            "tickers": tickers,
            "memberships": memberships,
        },
        "ticker_count": len(tickers),
        "valid_tickers": len(results),
        "errors": errors,
        "research_summary": summary,
        "results": results,
        "portfolio": portfolio,
    }
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    pdf_path = os.path.splitext(output_json)[0] + ".pdf"
    pdf_ok = write_pdf(output, pdf_path)
    brief_path = os.path.splitext(output_json)[0] + "_research_brief.md"
    brief_ok = write_research_brief(output, brief_path)
    print(f"JSON: {output_json}")
    print(f"PDF: {pdf_path}" if pdf_ok else "PDF: unavailable")
    print(f"Research brief: {brief_path}" if brief_ok else "Research brief: unavailable")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run optimized options screening.")
    parser.add_argument("--universe", default=CONFIG["universe"], help="Universe name: spx, ixic, djia, ndx, rut, rua, mid, sml, w5000, or sox.")
    parser.add_argument("--tickers", default="", help="Optional comma-separated custom tickers")
    parser.add_argument("--output", default=None, help="Output JSON path (auto: options_screening_results_{universe}_{YYMMDD}.json)")
    parser.add_argument("--no-cache", action="store_true", help="Disable yfinance history/options cache for this run")
    parser.add_argument("--reuse-session-results", action="store_true", help="Reuse same-session ticker results across overlapping research universes")
    parser.add_argument("--daily-price-cache-only", action="store_true", help="Use the preflight-synced daily cache; never fetch per-symbol equity prices")
    parser.add_argument("--no-cboe-options", action="store_true", help="Disable CBOE delayed option-chain fallback for this run")
    parser.add_argument("--cache-dir", default=CONFIG["cache_dir"], help="Override yfinance cache directory")
    parser.add_argument("--snapshot-dir", default=CONFIG["universe_snapshot_dir"], help="Override ETF constituent snapshot archive directory")
    parser.add_argument("--snapshot-date", default="", help="Use the latest archived constituent snapshot on or before YYYY-MM-DD")
    parser.add_argument("--archive-only", action="store_true", help="Fail when --snapshot-date has no archived constituents; never substitute today's holdings")
    parser.add_argument("--snapshot-only", action="store_true", help="Archive the selected live universe and exit without running the options model")
    parser.add_argument("--list-universes", action="store_true", help="Print registered research universes and exit")
    parser.add_argument("--option-chain-cache-minutes", type=float, default=CONFIG["option_chain_cache_ttl_minutes"], help="Option chain cache TTL in minutes")
    parser.add_argument(
        "--strategy-family",
        default=CONFIG["strategy_family"],
        choices=["auto", "Single", "Spread", "Straddle", "Roll Position", "Butterfly", "Condor", "Iron Condor"],
        help="Force one strategy family; Roll Position only adds roll guidance and does not create a new opening trade.",
    )
    args = parser.parse_args()
    if args.list_universes:
        print(json.dumps(UNIVERSE_REGISTRY, ensure_ascii=False, indent=2, default=list))
        raise SystemExit(0)
    CONFIG["universe"] = args.universe
    CONFIG["strategy_family"] = args.strategy_family
    CONFIG["cache_enabled"] = not args.no_cache
    CONFIG["reuse_session_results"] = args.reuse_session_results
    CONFIG["daily_price_cache_only"] = args.daily_price_cache_only
    CONFIG["cboe_option_chain_enabled"] = not args.no_cboe_options
    CONFIG["cache_dir"] = args.cache_dir
    CONFIG["universe_snapshot_dir"] = args.snapshot_dir
    CONFIG["option_chain_cache_ttl_minutes"] = args.option_chain_cache_minutes
    if args.output is None:
        today = date.today().strftime("%y%m%d")
        args.output = f"options_screening_results_{args.universe}_{today}.json"
        print(f"Output → {args.output}")
    TICKERS, SOURCE, MEMBERSHIPS = resolve_universe(
        args.universe,
        args.tickers or None,
        args.snapshot_date or None,
        args.archive_only,
    )
    print(f"Universe: {args.universe} | source={SOURCE} | tickers={len(TICKERS)} | strategy_family={args.strategy_family}")
    if args.snapshot_only:
        print(json.dumps({"universe": args.universe, "source": SOURCE, "ticker_count": len(TICKERS)}, ensure_ascii=False))
        raise SystemExit(0)
    main(TICKERS, args.output, SOURCE, MEMBERSHIPS)
