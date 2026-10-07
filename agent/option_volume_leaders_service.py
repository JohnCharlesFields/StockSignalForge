"""Cboe four-exchange option-volume leaders, for manual research screens only."""

from __future__ import annotations

import csv
import io
import time
from collections import defaultdict
from datetime import date
from typing import Any

import requests

from market_data_service import _CACHE_ROOT
from scripts.backtest_leader_long_options import MAGNIFICENT_SEVEN

CBOE_REPORT = "https://www.cboe.com/us/options/market_statistics/historical_data/download/all_symbols/"
VOLUME_ROOT = _CACHE_ROOT / "long_option_screen" / "cboe_volume"
EXCHANGES = ("CBOE", "BATS", "C2", "EDGX")
MAX_REFERENCE_CHECKS = 45
NASDAQ_LISTINGS = (
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
)


def _months(as_of: date) -> list[tuple[int, int]]:
    year, month = as_of.year, as_of.month
    result = []
    for _ in range(12):
        month -= 1
        if month == 0:
            year -= 1
            month = 12
        result.append((year, month))
    return list(reversed(result))


def _monthly_report(year: int, month: int, session: requests.Session) -> str:
    path = VOLUME_ROOT / f"{year}-{month:02d}.csv"
    if path.exists():
        return path.read_text(encoding="utf-8-sig")
    response = session.get(
        CBOE_REPORT,
        params={"reportType": "volume", "month": str(month), "year": str(year),
                "volumeType": "sum", "volumeAggType": "monthly", "exchanges": list(EXCHANGES)},
        timeout=35,
    )
    response.raise_for_status()
    if len(response.content) > 10_000_000:
        raise ValueError("Cboe report exceeds size limit")
    raw = response.content.decode("utf-8-sig")
    header = ("Trade Month", "Options Class", "Underlying", "Product Type", "Exchange", "Volume")
    if tuple(next(csv.reader(io.StringIO(raw)), ())) != header:
        raise ValueError("Cboe report schema changed")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(raw, encoding="utf-8")
    temp.replace(path)
    return raw


def _listing_directory(session: requests.Session) -> dict[str, dict[str, str]]:
    """Official Nasdaq directory covers Nasdaq and other US-listed securities."""
    listings = {}
    for url in NASDAQ_LISTINGS:
        name = url.rsplit("/", 1)[-1]
        path = VOLUME_ROOT / name
        if path.exists() and time.time() - path.stat().st_mtime < 24 * 3600:
            raw = path.read_text(encoding="utf-8-sig")
        else:
            response = session.get(url, timeout=20)
            response.raise_for_status()
            raw = response.content.decode("utf-8-sig")
            if len(raw) > 3_000_000 or "|ETF|" not in raw.splitlines()[0]:
                raise ValueError("Nasdaq symbol directory schema changed")
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(raw, encoding="utf-8")
            temp.replace(path)
        for row in csv.DictReader(io.StringIO(raw), delimiter="|"):
            symbol = (row.get("Symbol") or row.get("ACT Symbol") or "").strip().upper()
            if symbol and row.get("Test Issue") == "N":
                listings[symbol] = {"etf": row.get("ETF", ""), "name": row.get("Security Name", "")}
    return listings


def rank_non_seven_stocks(as_of: date, *, limit: int = 10, session: requests.Session | None = None) -> dict[str, Any]:
    """Rank common stocks by 12 *complete* calendar months of Cboe option volume.

    An incomplete report fails the whole ranking; never label a partial year as a year.
    Unverified security types are skipped, not presumed to be common stock.
    """
    session = session or requests.Session()
    months = _months(as_of)
    totals: dict[str, int] = defaultdict(int)
    try:
        for year, month in months:
            rows = csv.DictReader(io.StringIO(_monthly_report(year, month, session)))
            for row in rows:
                if row["Product Type"] != "S" or row["Exchange"] not in EXCHANGES:
                    continue
                symbol = row["Underlying"].strip().upper()
                if symbol:
                    totals[symbol] += int(row["Volume"])
    except (requests.RequestException, OSError, ValueError, KeyError, TypeError) as exc:
        return {"available": False, "reason": f"Cboe 12个月报表未完整取得：{type(exc).__name__}", "leaders": []}

    try:
        listings = _listing_directory(session)
    except (requests.RequestException, OSError, ValueError, TypeError) as exc:
        return {"available": False, "reason": f"Nasdaq证券类型目录未取得：{type(exc).__name__}", "leaders": []}

    leaders = []
    unchecked = 0
    reference_checks = 0
    for symbol, volume in sorted(totals.items(), key=lambda item: (-item[1], item[0])):
        if symbol in MAGNIFICENT_SEVEN:
            continue
        if reference_checks >= MAX_REFERENCE_CHECKS:
            break
        reference_checks += 1
        listing = listings.get(symbol)
        if listing is None:
            unchecked += 1
            continue
        if listing["etf"] != "N" or "common stock" not in listing["name"].lower():
            continue
        leaders.append({"symbol": symbol, "contracts_12m": volume, "security_type": "common_stock",
                        "security_name": listing["name"],
                        "source": "cboe_4_exchanges:monthly_volume"})
        if len(leaders) >= limit:
            break
    return {
        "available": len(leaders) >= 5, "leaders": leaders,
        "window_start": f"{months[0][0]}-{months[0][1]:02d}",
        "window_end": f"{months[-1][0]}-{months[-1][1]:02d}",
        "exchanges": list(EXCHANGES), "security_type_unverified": unchecked,
        "reference_checks": reference_checks, "ranking_complete": len(leaders) >= limit and unchecked == 0,
        "coverage": "Cboe旗下四个期权交易所，非全美市场；12个完整自然月。普通股与ETF按Nasdaq Trader当日目录核验。",
        "reason": None if len(leaders) >= 5 else "不足5只核验普通股；未以 ETF 或正股成交量补位。",
    }
