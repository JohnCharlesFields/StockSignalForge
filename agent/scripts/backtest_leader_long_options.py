#!/usr/bin/env python3
"""Auditable, cost-capped pilot for long options on large-cap sector leaders.

This is a research replay, not a live order generator. Historical sector leaders
require point-in-time market-cap snapshots; today's cached profiles are never
presented as proof of historical leadership.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

AGENT = Path(__file__).resolve().parents[1]
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

from consensus_signal_service import consensus_feature_frame  # noqa: E402
from market_calendar import is_trading_day  # noqa: E402
from market_data_service import _CACHE_ROOT, get_daily_history  # noqa: E402
from peer_earnings_signal_service import PEER_GROUPS  # noqa: E402


MAGNIFICENT_SEVEN = ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA")
EASTERN = ZoneInfo("America/New_York")
DATASET = "OPRA.PILLAR"


def _finite_positive(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError):
        return None


def _cached_cap(symbol: str, root: Path) -> dict[str, Any]:
    """Read existing provider caches without triggering hundreds of API calls."""
    sources = (
        (root / "fmp" / f"PROFILE_{symbol}_.json", "fmp_cached_profile"),
        (root / "ticker_reference" / f"{symbol}.json", "massive_cached_reference"),
    )
    candidates = []
    for path, source in sources:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                payload = payload[0] if payload else {}
            cap = _finite_positive(payload.get("marketCap") or payload.get("market_cap"))
            if cap is None:
                continue
            captured_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            candidates.append({"market_cap": cap, "source": source, "captured_at": captured_at.isoformat()})
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return max(candidates, key=lambda row: row["captured_at"]) if candidates else {}


def build_leader_snapshot(root: Path, *, as_of: datetime | None = None, max_age_days: int = 7) -> dict[str, Any]:
    as_of = as_of or datetime.now(timezone.utc)
    symbols = sorted({str(s).upper() for group in PEER_GROUPS for s in group["symbols"]})
    profiles = {symbol: _cached_cap(symbol, root) for symbol in symbols}
    groups = []
    for group in PEER_GROUPS:
        members = [str(s).upper() for s in group["symbols"]]
        ranked = sorted((s for s in members if profiles[s]), key=lambda s: (-profiles[s]["market_cap"], s))
        missing = [s for s in members if not profiles[s]]
        stale = [
            s for s in members if profiles[s]
            and not timedelta(0) <= as_of - datetime.fromisoformat(profiles[s]["captured_at"]) <= timedelta(days=max_age_days)
        ]
        complete_recent = bool(ranked) and not missing and not stale
        groups.append({
            "group_id": group["id"],
            "group_label": group["label"],
            "member_count": len(members),
            "leader": ranked[0] if ranked else None,
            "leader_market_cap": profiles[ranked[0]]["market_cap"] if ranked else None,
            "ranking_status": "complete_recent_cache" if complete_recent else "provisional_incomplete_or_stale",
            "missing_cap": missing,
            "stale_cap": stale,
        })
    eligible = sorted(set(MAGNIFICENT_SEVEN) | {
        group["leader"] for group in groups
        if group["ranking_status"] == "complete_recent_cache" and group["leader"]
    })
    return {
        "generated_at": as_of.isoformat(),
        "selection_policy": "Magnificent Seven + largest cached market cap in each existing peer group",
        "historical_pit_status": "unavailable_before_this_snapshot",
        "market_cap_profiles": profiles,
        "groups": groups,
        "eligible_symbols": eligible,
        "group_count": len(groups),
        "complete_recent_groups": sum(g["ranking_status"] == "complete_recent_cache" for g in groups),
        "warning": "Current cached caps are not point-in-time historical caps; retrospective group backtests are exploratory only.",
    }


def refresh_market_caps(root: Path, limit: int) -> dict[str, Any]:
    """Use the project's guarded FMP profile endpoint, never bypassing quotas."""
    from market_data_service import _fmp_get

    before = build_leader_snapshot(root)
    groups = sorted(
        before["groups"],
        key=lambda row: (len(row["missing_cap"]) + len(row["stale_cap"]), row["group_id"]),
    )
    pending = []
    for group in groups:
        for symbol in group["missing_cap"] + group["stale_cap"]:
            if symbol not in pending:
                pending.append(symbol)
    refreshed = []
    for symbol in pending[:max(0, limit)]:
        path = root / "fmp" / f"PROFILE_{symbol}_.json"
        prior_mtime = path.stat().st_mtime_ns if path.exists() else None
        try:
            _fmp_get("profile", symbol)
        except Exception:
            continue
        if path.exists() and path.stat().st_mtime_ns != prior_mtime:
            refreshed.append(symbol)
    return {"attempted": min(max(0, limit), len(pending)), "refreshed": refreshed, "remaining_before": len(pending)}


def _daily(symbol: str) -> pd.DataFrame:
    frame, _ = get_daily_history(symbol, period="5y", allow_yfinance_fallback=False)
    return frame if frame is not None else pd.DataFrame()


def select_consensus_events(
    symbols: list[str], start: date, end: date, *, max_events: int = 4,
    histories: dict[str, pd.DataFrame] | None = None,
) -> list[dict[str, Any]]:
    """Use the existing bull/bear consensus crossing; no forward price enters selection."""
    histories = histories if histories is not None else {}
    benchmark = histories.setdefault("SPY", _daily("SPY"))
    events: list[dict[str, Any]] = []
    for symbol in symbols:
        stock = histories.setdefault(symbol, _daily(symbol))
        if stock.empty or len(stock) < 220:
            continue
        feature = consensus_feature_frame(stock, benchmark)
        if feature.empty:
            continue
        net = pd.to_numeric(feature["net_consensus"], errors="coerce")
        for position, stamp in enumerate(feature.index):
            day = pd.Timestamp(stamp).date()
            if day < start or day > end or position < 1:
                continue
            value, previous = net.iloc[position], net.iloc[position - 1]
            if pd.isna(value) or pd.isna(previous):
                continue
            side = "C" if value > 0.12 and previous <= 0.12 else (
                "P" if value < -0.12 and previous >= -0.12 else None
            )
            if side is None:
                continue
            if position + 10 >= len(stock):
                continue
            spot = _finite_positive(stock["Close"].iloc[position])
            if spot is None:
                continue
            events.append({
                "ticker": symbol, "signal_date": day.isoformat(),
                "entry_date": pd.Timestamp(stock.index[position + 1]).date().isoformat(),
                "side": side, "signal_close": spot, "net_consensus": round(float(value), 4),
                "signal_source": "consensus_signal_service:threshold_crossing",
            })
    # Reserve room for both directions; within each direction the earliest
    # preselected crossing is used, never an outcome-ranked event.
    events.sort(key=lambda x: (x["signal_date"], x["ticker"], x["side"]))
    selected: list[dict[str, Any]] = []
    used: set[str] = set()
    for side, quota in (("C", (max_events + 1) // 2), ("P", max_events // 2)):
        for event in [x for x in events if x["side"] == side]:
            if sum(x["side"] == side for x in selected) >= quota:
                break
            if event["ticker"] not in used:
                selected.append(event)
                used.add(event["ticker"])
    for event in events:
        if len(selected) >= max_events:
            break
        if event["ticker"] not in used:
            selected.append(event)
            used.add(event["ticker"])
    return sorted(selected, key=lambda x: (x["signal_date"], x["ticker"]))


def _cache_file(root: Path, schema: str, symbols: list[str], start: str, end: str, stype: str) -> Path:
    key = json.dumps([DATASET, schema, symbols, start, end, stype], separators=(",", ":"))
    return root / "opra_long_option_replay" / f"{hashlib.sha256(key.encode()).hexdigest()}.csv.gz"


class CostCappedHistorical:
    def __init__(self, client: Any, root: Path, max_cost: float):
        self.client, self.root, self.max_cost = client, root, max_cost
        self.estimated_cost = 0.0
        self.requests = []

    def fetch(self, *, schema: str, symbols: list[str], start: str, end: str, stype: str) -> pd.DataFrame:
        path = _cache_file(self.root, schema, symbols, start, end, stype)
        if path.exists():
            self.requests.append({"schema": schema, "symbols": symbols, "cache_hit": True, "cost": 0.0})
            return pd.read_csv(path, index_col=0, parse_dates=True)
        estimate = float(self.client.metadata.get_cost(
            dataset=DATASET, symbols=symbols, schema=schema, start=start, end=end, stype_in=stype,
        ))
        if not math.isfinite(estimate) or estimate < 0 or self.estimated_cost + estimate > self.max_cost:
            raise ValueError(f"cost_cap: next=${estimate:.4f}, cumulative=${self.estimated_cost:.4f}, cap=${self.max_cost:.4f}")
        frame = self.client.timeseries.get_range(
            dataset=DATASET, symbols=symbols, schema=schema, start=start, end=end, stype_in=stype,
        ).to_df()
        self.estimated_cost += estimate
        self.requests.append({"schema": schema, "symbols": symbols, "cache_hit": False, "cost": estimate})
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        frame.to_csv(temp, compression="gzip")
        temp.replace(path)
        return frame


def choose_contract(defs: pd.DataFrame, side: str, spot: float, entry_date: date) -> dict[str, Any] | None:
    if defs is None or defs.empty:
        return None
    frame = defs.copy()
    needed = {"expiration", "strike_price", "instrument_class"}
    if not needed.issubset(frame.columns):
        return None
    frame["expiry"] = pd.to_datetime(frame["expiration"], errors="coerce", utc=True).dt.date
    frame["dte"] = frame["expiry"].apply(lambda day: (day - entry_date).days if pd.notna(day) else -1)
    frame["strike_price"] = pd.to_numeric(frame["strike_price"], errors="coerce")
    frame = frame[
        frame["dte"].between(45, 75)
        & (frame["instrument_class"].astype(str).str.upper() == side)
        & frame["strike_price"].notna()
    ]
    if frame.empty:
        return None
    frame = frame.assign(dte_distance=(frame["dte"] - 60).abs(), strike_distance=(frame["strike_price"] - spot).abs())
    row = frame.sort_values(["dte_distance", "strike_distance", "strike_price"]).iloc[0]
    raw = str(row.get("raw_symbol") or row.get("symbol") or "")
    if not raw:
        return None
    return {"contract": raw, "expiry": row["expiry"].isoformat(), "strike": float(row["strike_price"]), "dte": int(row["dte"])}


def _valid_quotes(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty or not {"bid_px_00", "ask_px_00"}.issubset(frame.columns):
        return pd.DataFrame()
    out = frame.copy()
    out.index = pd.to_datetime(out.index, utc=True, errors="coerce")
    out = out[out.index.notna()].sort_index()
    out["bid"] = pd.to_numeric(out["bid_px_00"], errors="coerce")
    out["ask"] = pd.to_numeric(out["ask_px_00"], errors="coerce")
    out = out[(out["bid"] > 0) & (out["ask"] >= out["bid"])]
    local = out.index.tz_convert(EASTERN)
    return out[(local.time >= time(9, 30)) & (local.time <= time(16, 0))]


def _trading_window(start: date, count: int) -> list[date]:
    days = []
    current = start
    while len(days) < count:
        if is_trading_day(current):
            days.append(current)
        current += timedelta(days=1)
    return days


def settle_path(
    quotes: pd.DataFrame, entry_day: date, *, hold_days: int = 10,
    target: float = 0.50, stop: float = 0.25, fee_per_contract: float = 0.65,
    max_spread_fraction: float = 0.10,
) -> dict[str, Any]:
    valid = _valid_quotes(quotes)
    if valid.empty:
        return {"status": "missing_quotes"}
    local = valid.index.tz_convert(EASTERN)
    entry_window = valid[(local.date == entry_day) & (local.time >= time(9, 40)) & (local.time <= time(10, 0))]
    if entry_window.empty:
        return {"status": "missing_entry_quote"}
    entry = entry_window.iloc[0]
    ask, bid = float(entry["ask"]), float(entry["bid"])
    spread_fraction = (ask - bid) / ask
    if spread_fraction > max_spread_fraction:
        return {
            "status": "wide_entry_spread", "entry_at": entry_window.index[0].isoformat(),
            "entry_ask": ask, "entry_bid": bid, "spread_fraction": round(spread_fraction, 4),
        }
    expected = _trading_window(entry_day, hold_days)
    last_day = expected[-1]
    path = valid[(valid.index >= entry_window.index[0]) & (local.date <= last_day)]
    if path.empty:
        return {"status": "missing_exit_quote"}
    available = {stamp.date() for stamp in path.index.tz_convert(EASTERN)}
    reason, exit_stamp, exit_bid = "time_exit", path.index[-1], float(path.iloc[-1]["bid"])
    for stamp, row in path.iterrows():
        price = float(row["bid"])
        if price >= ask * (1.0 + target):
            reason, exit_stamp, exit_bid = "target_first", stamp, price
            break
        if price <= ask * (1.0 - stop):
            reason, exit_stamp, exit_bid = "stop_first", stamp, price
            break
    exit_day = exit_stamp.tz_convert(EASTERN).date()
    required = [day for day in expected if day <= exit_day]
    missing = [day.isoformat() for day in required if day not in available]
    if missing:
        return {"status": "insufficient_quote_sessions", "missing_sessions": missing, "quote_sessions": len(available)}
    if reason == "time_exit" and exit_day < last_day:
        return {"status": "insufficient_quote_sessions", "quote_sessions": len(available)}
    if reason == "time_exit" and exit_stamp.tz_convert(EASTERN).time() < time(15, 45):
        return {"status": "missing_close_quote", "last_quote_at": exit_stamp.isoformat()}
    gross = (exit_bid - ask) * 100.0
    net = gross - 2.0 * fee_per_contract
    return {
        "status": "resolved", "exit_reason": reason,
        "entry_at": entry_window.index[0].isoformat(), "exit_at": exit_stamp.isoformat(),
        "entry_ask": ask, "entry_bid": bid, "exit_bid": exit_bid,
        "spread_fraction": round(spread_fraction, 4), "gross_dollars": round(gross, 2),
        "net_dollars": round(net, 2), "net_return_on_premium": round(net / (ask * 100.0), 4),
        "quote_sessions": len(required),
    }


def risk_audit(result: dict[str, Any], capital: float, risk_fraction: float, fee: float) -> dict[str, Any]:
    ask = _finite_positive(result.get("entry_ask"))
    if ask is None:
        return {"capital_usd": capital, "planned_risk_limit_usd": round(capital * risk_fraction, 2), "risk_status": "unknown_no_entry_quote"}
    premium = ask * 100.0
    planned_stop_risk = premium * 0.25 + 2.0 * fee
    max_loss = premium + fee
    eligible = premium + fee <= capital and planned_stop_risk <= capital * risk_fraction
    return {
        "capital_usd": capital, "planned_risk_limit_usd": round(capital * risk_fraction, 2),
        "premium_cash_usd": round(premium + fee, 2),
        "planned_stop_risk_usd": round(planned_stop_risk, 2),
        "max_premium_loss_usd": round(max_loss, 2),
        "within_cash_and_planned_stop_budget": eligible,
        "risk_status": "within_planned_budget" if eligible else "exceeds_cash_or_planned_stop_budget",
        "risk_note": "A stop order cannot guarantee a 25% loss cap; a gap or illiquidity can lose the full premium.",
    }


def replay_event(
    event: dict[str, Any], feed: CostCappedHistorical, *, fee: float = 0.65,
    capital: float = 2000.0, risk_fraction: float = 0.015,
) -> dict[str, Any]:
    item = dict(event)
    entry_day = date.fromisoformat(event["entry_date"])
    definition_start = entry_day.isoformat() + "T00:00:00Z"
    definition_end = (entry_day + timedelta(days=1)).isoformat() + "T00:00:00Z"
    try:
        defs = feed.fetch(
            schema="definition", symbols=[f"{event['ticker']}.OPT"],
            start=definition_start, end=definition_end, stype="parent",
        )
        contract = choose_contract(defs, event["side"], float(event["signal_close"]), entry_day)
        if contract is None:
            return {**item, "status": "no_listed_45_75_dte_contract"}
        # Query ten trading sessions plus a weekend/holiday margin. The path
        # evaluator still requires ten distinct sessions with valid quotes.
        quote_start = datetime.combine(entry_day, time(9, 40), EASTERN).isoformat()
        quote_end = datetime.combine(entry_day + timedelta(days=19), time(16, 1), EASTERN).isoformat()
        quotes = feed.fetch(
            schema="cbbo-1m", symbols=[contract["contract"]],
            start=quote_start, end=quote_end, stype="raw_symbol",
        )
        result = {**item, **contract, **settle_path(quotes, entry_day, fee_per_contract=fee)}
        return {**result, **risk_audit(result, capital, risk_fraction, fee)}
    except Exception as exc:
        return {**item, "status": "data_unavailable", "reason": f"{type(exc).__name__}: {str(exc)[:180]}"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("universe", "pilot"), default="universe")
    parser.add_argument("--start", default="2026-08-04")
    parser.add_argument("--end", default="2026-08-31")
    parser.add_argument("--max-events", type=int, default=4)
    parser.add_argument("--max-cost-usd", type=float, default=0.50)
    parser.add_argument("--refresh-cap-limit", type=int, default=0, help="Optional FMP profile refresh, guarded by existing quotas")
    parser.add_argument("--fee-per-contract", type=float, default=0.65)
    parser.add_argument("--account-capital", type=float, default=2000.0)
    parser.add_argument("--risk-fraction", type=float, default=0.015)
    parser.add_argument("--execute", action="store_true", help="Required before any billable OPRA download")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    if args.account_capital <= 0 or not 0 < args.risk_fraction < 1:
        parser.error("account-capital must be positive and risk-fraction must be between 0 and 1")
    now = datetime.now(timezone.utc)
    refresh = refresh_market_caps(_CACHE_ROOT, args.refresh_cap_limit) if args.refresh_cap_limit else None
    snapshot = build_leader_snapshot(_CACHE_ROOT, as_of=now)
    default_path = _CACHE_ROOT / "leader_universe" / f"leader_options_{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    output = Path(args.output) if args.output else default_path
    report: dict[str, Any] = {"universe": snapshot, "market_cap_refresh": refresh, "pilot": None}
    if args.mode == "pilot":
        symbols = snapshot["eligible_symbols"]
        events = select_consensus_events(
            symbols, date.fromisoformat(args.start), date.fromisoformat(args.end),
            max_events=max(0, args.max_events),
        )
        pilot: dict[str, Any] = {
            "events": events, "results": [], "execution_enabled": args.execute,
            "max_cost_usd": args.max_cost_usd,
            "fee_per_contract_side_usd": args.fee_per_contract,
            "account_capital_usd": args.account_capital,
            "planned_risk_fraction": args.risk_fraction,
            "cohort_warning": "Retrospective current-leader selection is not PIT; do not report these samples as calibrated win rate.",
            "signal_method": "Existing OHLCV bull/bear consensus crossing, signal at close, next-session option entry",
            "contract_method": "Nearest listed 60 DTE (45-75 window), nearest strike to signal close; not a delta-optimized strategy",
            "exit_method": "10 quote sessions; first +50% or -25% of entry ask at executable bid; otherwise last bid",
        }
        if args.execute and events:
            if args.max_cost_usd <= 0:
                raise ValueError("max-cost-usd must be positive")
            import databento as db

            feed = CostCappedHistorical(db.Historical(), _CACHE_ROOT, args.max_cost_usd)
            pilot["results"] = [
                replay_event(event, feed, fee=args.fee_per_contract, capital=args.account_capital, risk_fraction=args.risk_fraction)
                for event in events
            ]
            pilot["estimated_data_cost_usd"] = round(feed.estimated_cost, 6)
            pilot["requests"] = feed.requests
            pilot["resolved_count"] = sum(row.get("status") == "resolved" for row in pilot["results"])
            pilot["target_first_count"] = sum(row.get("exit_reason") == "target_first" for row in pilot["results"])
        report["pilot"] = pilot
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({
        "output": str(output), "group_count": snapshot["group_count"],
        "complete_recent_groups": snapshot["complete_recent_groups"],
        "eligible_symbols": len(snapshot["eligible_symbols"]),
        "events": len((report.get("pilot") or {}).get("events") or []),
        "resolved": (report.get("pilot") or {}).get("resolved_count"),
        "estimated_data_cost_usd": (report.get("pilot") or {}).get("estimated_data_cost_usd"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
