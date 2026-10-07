"""Offline study of post-news rallies and subsequent reversal.

The news archive is incomplete and its event labels are keyword based. This
script studies *association*, not whether an article caused a price move.
It never changes live rankings or estimates option returns.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd


HORIZONS = (1, 3, 5, 10)


def session_day(published_utc: str) -> str | None:
    """Daily bars cannot attribute an intraday move to intraday news."""
    try:
        stamp = pd.Timestamp(published_utc)
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
        eastern = stamp.tz_convert("America/New_York")
    except (TypeError, ValueError):
        return None
    minutes = eastern.hour * 60 + eastern.minute
    if eastern.weekday() < 5 and 9 * 60 + 30 <= minutes < 16 * 60:
        return None
    day = eastern.date() + timedelta(days=1 if minutes >= 16 * 60 else 0)
    return day.isoformat()


def read_prices(cache_dir: Path, symbol: str) -> pd.Series | None:
    path = cache_dir / f"{symbol}.csv"
    if not path.is_file():
        return None
    try:
        frame = pd.read_csv(path, usecols=["Date", "Close"])
        dates = pd.to_datetime(frame["Date"], errors="coerce")
        closes = pd.to_numeric(frame["Close"], errors="coerce")
        series = pd.Series(closes.values, index=dates).dropna()
        series = series[~series.index.duplicated(keep="last")].sort_index()
        return series if len(series) > 30 else None
    except (OSError, ValueError, KeyError):
        return None


def next_trading_session(day: str | None, spy: pd.Series) -> str | None:
    if day is None:
        return None
    index = int(spy.index.searchsorted(pd.Timestamp(day)))
    return spy.index[index].date().isoformat() if index < len(spy) else None


def prior_valuation_snapshots(conn: sqlite3.Connection) -> dict[str, list[tuple[pd.Timestamp, float, float | None, str]]]:
    """Load archived multiple/price pairs, never reconstructed from later data."""
    result: dict[str, list[tuple[pd.Timestamp, float, float | None, str]]] = defaultdict(list)
    for generated, raw in conn.execute(
        "SELECT generated_at, payload_json FROM home_dashboard_snapshots WHERE status = 'completed'"
    ):
        try:
            stamp = pd.Timestamp(generated)
            stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
            rows = json.loads(raw).get("rows", [])
        except (TypeError, ValueError, AttributeError):
            continue
        for row in rows:
            try:
                pe = float(row.get("pe"))
            except (TypeError, ValueError):
                continue
            if np.isfinite(pe) and pe > 0:
                try:
                    price = float(row.get("current_price"))
                    price = price if np.isfinite(price) and price > 0 else None
                except (TypeError, ValueError):
                    price = None
                result[str(row.get("symbol", "")).upper()].append(
                    (stamp, pe, price, str(row.get("price_as_of") or ""))
                )
    for values in result.values():
        values.sort(key=lambda item: item[0])
    return result


def prior_pe(
    snapshots: dict[str, list[tuple[pd.Timestamp, float, float | None, str]]], symbol: str, published_utc: str
) -> float | None:
    published = pd.Timestamp(published_utc)
    published = published.tz_localize("UTC") if published.tzinfo is None else published.tz_convert("UTC")
    for stamp, pe, _price, _price_date in reversed(snapshots.get(symbol, [])):
        if stamp < published:
            return pe if published - stamp <= pd.Timedelta(days=7) else None
    return None


def prior_peer_valuation_snapshots(conn: sqlite3.Connection) -> list[tuple[pd.Timestamp, dict[str, dict]]]:
    """Keep whole archived cross-sections so peers share a real snapshot date."""
    snapshots = []
    for generated, raw in conn.execute(
        "SELECT generated_at, payload_json FROM home_dashboard_snapshots WHERE status = 'completed'"
    ):
        try:
            stamp = pd.Timestamp(generated)
            stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
            rows = json.loads(raw).get("rows", [])
            by_symbol = {str(row.get("symbol", "")).upper(): row for row in rows if isinstance(row, dict)}
        except (TypeError, ValueError, AttributeError):
            continue
        snapshots.append((stamp, by_symbol))
    return sorted(snapshots, key=lambda item: item[0])


def peer_valuation_reference(
    snapshots: list[tuple[pd.Timestamp, dict[str, dict]]],
    symbol: str,
    published_utc: str,
    event_close: float,
    min_peers: int = 3,
) -> dict:
    """Static-earnings peer scenario using only one pre-publication cross-section."""
    result = {
        "peer_reference_status": "missing_pre_event_snapshot",
        "peer_reference_snapshot_at": None,
        "peer_reference_price_as_of": None,
        "peer_reference_group": None,
        "peer_reference_sublane": None,
        "peer_reference_pe_type": None,
        "peer_reference_symbols": [],
        "peer_reference_count": 0,
        "peer_reference_pe_median": None,
        "peer_reference_pe_q25": None,
        "peer_reference_pe_q75": None,
        "peer_reference_price_median": None,
        "peer_reference_price_low": None,
        "peer_reference_price_high": None,
        "peer_reference_gap_pct": None,
    }
    published = pd.Timestamp(published_utc)
    published = published.tz_localize("UTC") if published.tzinfo is None else published.tz_convert("UTC")
    for stamp, rows in reversed(snapshots):
        if stamp >= published:
            continue
        if published - stamp > pd.Timedelta(days=7):
            break
        target = rows.get(symbol.upper())
        if not target:
            continue
        result["peer_reference_snapshot_at"] = stamp.isoformat()
        mapping = target.get("peer_mapping") or {}
        lane = mapping.get("target_sublane")
        peers = mapping.get("valuation_peers") or mapping.get("direct_peers") or []
        result["peer_reference_group"] = mapping.get("group_id")
        result["peer_reference_sublane"] = lane
        price_date = str(target.get("price_as_of") or "")
        result["peer_reference_price_as_of"] = price_date or None
        if not lane or not peers:
            result["peer_reference_status"] = "missing_same_sublane_mapping"
            return result
        if not price_date or price_date > published.tz_convert("America/New_York").date().isoformat():
            result["peer_reference_status"] = "missing_or_future_price_date"
            return result
        pe_type = target.get("pe_type")
        result["peer_reference_pe_type"] = pe_type
        if pe_type not in {"trailing", "forward"}:
            result["peer_reference_status"] = "unverified_target_pe_basis"
            return result
        try:
            target_pe = float(target.get(f"{pe_type}_pe"))
            target_price = float(target.get("current_price"))
        except (TypeError, ValueError):
            result["peer_reference_status"] = "missing_target_earnings_basis"
            return result
        if not all(np.isfinite(x) and x > 0 for x in (target_pe, target_price, event_close)):
            result["peer_reference_status"] = "missing_target_earnings_basis"
            return result
        peer_lanes = mapping.get("peer_sublanes") or {}
        multiples = []
        for peer in dict.fromkeys(str(item).upper() for item in peers):
            row = rows.get(peer)
            if peer == symbol.upper() or not row or row.get("price_as_of") != price_date:
                continue
            row_mapping = row.get("peer_mapping") or {}
            peer_lane = peer_lanes.get(peer)
            if peer_lane is None and row_mapping.get("group_id") == mapping.get("group_id"):
                peer_lane = row_mapping.get("target_sublane")
            if peer_lane != lane or row.get("pe_type") != pe_type:
                continue
            try:
                pe = float(row.get(f"{pe_type}_pe"))
            except (TypeError, ValueError):
                continue
            if np.isfinite(pe) and pe > 0:
                multiples.append((peer, pe))
        result["peer_reference_symbols"] = [item[0] for item in multiples]
        result["peer_reference_count"] = len(multiples)
        if len(multiples) < min_peers:
            result["peer_reference_status"] = "insufficient_same_sublane_peers"
            return result
        values = [item[1] for item in multiples]
        earnings_basis = target_price / target_pe
        q25, median, q75 = (float(x) for x in np.quantile(values, [0.25, 0.5, 0.75]))
        result.update({
            "peer_reference_status": "static_earnings_peer_scenario",
            "peer_reference_pe_median": median,
            "peer_reference_pe_q25": q25,
            "peer_reference_pe_q75": q75,
            "peer_reference_price_median": earnings_basis * median,
            "peer_reference_price_low": earnings_basis * q25,
            "peer_reference_price_high": earnings_basis * q75,
            "peer_reference_gap_pct": earnings_basis * median / event_close - 1.0,
        })
        return result
    return result


def valuation_reference(
    snapshots: dict[str, list[tuple[pd.Timestamp, float, float | None, str]]],
    symbol: str,
    published_utc: str,
    event_close: float,
) -> dict:
    """A pre-event multiple reversion *scenario*, not an intrinsic value."""
    empty = {
        "pe_reference_status": "insufficient_pre_event_snapshots",
        "pe_reference_multiple": None,
        "pe_reference_snapshot_days": 0,
        "pe_implied_earnings_basis": None,
        "pe_reference_price_provisional": None,
        "pe_reference_gap_pct_provisional": None,
    }
    published = pd.Timestamp(published_utc)
    published = published.tz_localize("UTC") if published.tzinfo is None else published.tz_convert("UTC")
    # Deduplicate repeated dashboard refreshes on the same pricing date.
    dated: dict[str, tuple[pd.Timestamp, float, float]] = {}
    for stamp, pe, price, price_date in snapshots.get(symbol, []):
        if not published - pd.Timedelta(days=30) <= stamp < published or price is None:
            continue
        as_of = price_date or stamp.date().isoformat()
        if as_of > published.tz_convert("America/New_York").date().isoformat():
            continue
        dated[as_of] = (stamp, pe, price)
    if len(dated) < 3:
        return empty
    observations = sorted(dated.values(), key=lambda item: item[0])
    latest_stamp, latest_pe, latest_price = observations[-1]
    if published - latest_stamp > pd.Timedelta(days=7):
        return empty
    baseline_pe = float(np.median([item[1] for item in observations]))
    earnings_basis = latest_price / latest_pe
    anchor = earnings_basis * baseline_pe
    if not np.isfinite(anchor) or anchor <= 0 or event_close <= 0:
        return empty
    return {
        "pe_reference_status": "unverified_pe_basis_static_earnings_scenario",
        "pe_reference_multiple": baseline_pe,
        "pe_reference_snapshot_days": len(dated),
        "pe_implied_earnings_basis": earnings_basis,
        "pe_reference_price_provisional": anchor,
        "pe_reference_gap_pct_provisional": anchor / event_close - 1.0,
    }


def study_event(symbol: str, session: str, prices: pd.Series, spy: pd.Series) -> dict | None:
    date = pd.Timestamp(session)
    idx = int(prices.index.searchsorted(date))
    if idx < 21 or idx + max(HORIZONS) >= len(prices):
        return None
    event_day = prices.index[idx]
    # A non-trading-day publication must align to the same SPY session.
    if event_day not in spy.index or prices.index[idx - 1] not in spy.index:
        return None
    spy_idx = int(spy.index.get_loc(event_day))
    if spy_idx < 21 or spy_idx + max(HORIZONS) >= len(spy):
        return None
    recent = prices.iloc[idx - 21:idx].pct_change().dropna()
    if len(recent) < 19 or recent.iloc[-20:].isna().any():
        return None
    daily_vol = float(recent.std(ddof=1))
    if not np.isfinite(daily_vol) or daily_vol <= 0:
        return None
    initial_raw = float(prices.iloc[idx] / prices.iloc[idx - 1] - 1)
    initial_spy = float(spy.loc[event_day] / spy.loc[prices.index[idx - 1]] - 1)
    initial_excess = initial_raw - initial_spy
    row = {
        "symbol": symbol,
        "event_session": event_day.date().isoformat(),
        "event_close": float(prices.iloc[idx]),
        "pre_event_close": float(prices.iloc[idx - 1]),
        "initial_return": initial_raw,
        "initial_excess_spy": initial_excess,
        "prior_20d_daily_vol": daily_vol,
        "rally": initial_excess > 0 and initial_raw >= daily_vol,
    }
    for horizon in HORIZONS:
        future = prices.index[idx + horizon]
        if future not in spy.index:
            return None
        raw = float(prices.iloc[idx + horizon] / prices.iloc[idx] - 1)
        bench = float(spy.loc[future] / spy.loc[event_day] - 1)
        row[f"forward_{horizon}d"] = raw
        row[f"excess_{horizon}d"] = raw - bench
    return row


def summarize(rows: list[dict], horizon: int) -> dict:
    values = np.array([r[f"excess_{horizon}d"] for r in rows], dtype=float)
    if not len(values):
        return {"n": 0, "mean_excess": None, "reversal_rate": None, "full_giveback_rate": None}
    initial = np.array([r["initial_excess_spy"] for r in rows], dtype=float)
    symbols = {r["symbol"] for r in rows}
    return {
        "n": len(rows),
        "symbols": len(symbols),
        "mean_excess": float(values.mean()),
        "median_excess": float(np.median(values)),
        "reversal_rate": float(np.mean(values < 0)),
        "full_giveback_rate": float(np.mean(values <= -initial)),
        "insufficient_sample": len(rows) < 30 or len(symbols) < 10,
    }


def clustered_contrast(events: list[dict], controls: list[dict], horizon: int, draws: int = 1000) -> dict:
    """Resample symbols, not overlapping daily events, for an exploratory CI."""
    event_by_symbol: dict[str, list[float]] = defaultdict(list)
    control_by_symbol: dict[str, list[float]] = defaultdict(list)
    for row in events:
        event_by_symbol[row["symbol"]].append(row[f"excess_{horizon}d"])
    for row in controls:
        control_by_symbol[row["symbol"]].append(row[f"excess_{horizon}d"])
    symbols = sorted(set(event_by_symbol) & set(control_by_symbol))
    if len(symbols) < 10:
        return {"paired_symbol_count": len(symbols), "difference": None, "ci95": None}
    event = np.array([np.mean(event_by_symbol[s]) for s in symbols], dtype=float)
    control = np.array([np.mean(control_by_symbol[s]) for s in symbols], dtype=float)
    deltas = event - control
    rng = np.random.default_rng(20260924)
    indices = rng.integers(0, len(symbols), size=(draws, len(symbols)))
    samples = deltas[indices].mean(axis=1)
    return {
        "paired_symbol_count": len(symbols),
        "difference": float(deltas.mean()),
        "ci95": [float(x) for x in np.quantile(samples, [0.025, 0.975])],
        "method": "equal-weight ticker paired difference, ticker bootstrap; exploratory, not a matched causal estimate",
    }


def run_study(db_path: Path, cache_dir: Path) -> tuple[dict, pd.DataFrame]:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        news = pd.read_sql_query(
            "SELECT news_id, symbol, published_utc, publisher, article_url, title_original, "
            "event_type, sentiment, sentiment_score "
            "FROM premarket_news_items ORDER BY published_utc, news_id", conn
        )
        snapshots = prior_valuation_snapshots(conn)
        peer_snapshots = prior_peer_valuation_snapshots(conn)
    finally:
        conn.close()
    spy = read_prices(cache_dir, "SPY")
    if spy is None:
        raise RuntimeError("SPY daily cache is unavailable")
    # "Not in archive" is only a diagnostic comparison, never proof that
    # the company had no contemporaneous news elsewhere.
    known_news_sessions = set()
    for item in news.itertuples(index=False):
        aligned = session_day(item.published_utc)
        if aligned is None:
            try:
                aligned = pd.Timestamp(item.published_utc).tz_convert("America/New_York").date().isoformat()
            except (TypeError, ValueError):
                continue
        session = next_trading_session(aligned, spy)
        if session is not None:
            known_news_sessions.add((item.symbol, session))
    news = news[(news.sentiment == "positive") & (news.sentiment_score > 0)].copy()
    news["session"] = news.published_utc.map(lambda value: next_trading_session(session_day(value), spy))
    eligible = news.dropna(subset=["session"])
    price_map: dict[str, pd.Series | None] = {}
    rows = []
    for (symbol, session), group in eligible.groupby(["symbol", "session"], sort=True):
        # One ticker-session is one observation; multiple articles are not
        # independent trials. Keep the earliest article for PIT valuation.
        first = group.iloc[0]
        if symbol not in price_map:
            price_map[symbol] = read_prices(cache_dir, symbol)
        prices = price_map[symbol]
        if prices is None:
            continue
        result = study_event(symbol, session, prices, spy)
        if result is None:
            continue
        result.update({
            "published_utc": first.published_utc,
            "article_count": len(group),
            "event_type_keyword": first.event_type,
            "publisher": first.publisher,
            "title_original": first.title_original,
            "article_url": first.article_url,
            "news_id": first.news_id,
            "prior_archived_pe": prior_pe(snapshots, symbol, first.published_utc),
        })
        result.update(valuation_reference(snapshots, symbol, first.published_utc, result["event_close"]))
        result.update(peer_valuation_reference(peer_snapshots, symbol, first.published_utc, result["event_close"]))
        rows.append(result)
    events = pd.DataFrame(rows)
    rallies = [r for r in rows if r["rally"]]
    comparison = []
    if rallies:
        first_day = min(r["event_session"] for r in rallies)
        last_day = max(r["event_session"] for r in rallies)
        rally_symbols = {r["symbol"] for r in rallies}
        for symbol, prices in price_map.items():
            if prices is None or symbol not in rally_symbols:
                continue
            for day in prices.loc[first_day:last_day].index:
                session = day.date().isoformat()
                if (symbol, session) in known_news_sessions:
                    continue
                control = study_event(symbol, session, prices, spy)
                if control is not None and control["rally"]:
                    comparison.append(control)
    pe_rows = [r for r in rallies if r["prior_archived_pe"] is not None]
    groups = {}
    for label, subset in (
        ("all_positive_mentions", rows),
        ("post_news_rallies", rallies),
        ("rallies_without_archived_news_comparison", comparison),
        ("rallies_with_prior_archived_pe", pe_rows),
    ):
        groups[label] = {f"{h}d": summarize(subset, h) for h in HORIZONS}
    by_type = {}
    for category in sorted({r["event_type_keyword"] for r in rallies}):
        subset = [r for r in rallies if r["event_type_keyword"] == category]
        by_type[category] = {f"{h}d": summarize(subset, h) for h in HORIZONS}
    anchor_rows = [r for r in rallies if r["pe_reference_price_provisional"] is not None]
    peer_anchor_rows = [r for r in rallies if r["peer_reference_price_median"] is not None]
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": "positive archived news, premarket/after-hours only; ticker-session dedup; event-day raw rally >= trailing 20d daily volatility and SPY excess > 0; forward from event close",
        "news_positive_count": len(news),
        "publication_time_eligible_count": len(eligible),
        "matched_ticker_sessions": len(rows),
        "rally_count": len(rallies),
        "rally_session_range": [min((r["event_session"] for r in rallies), default=None), max((r["event_session"] for r in rallies), default=None)],
        "prior_archived_pe_count": len(pe_rows),
        "valuation_reference": {
            "preferred_basis": "same_sublane_peer_pe_when_available",
            "peer_available_count": len(peer_anchor_rows),
            "peer_reference_below_event_close_count": sum(r["peer_reference_gap_pct"] < 0 for r in peer_anchor_rows),
            "peer_status_counts": {
                str(status): int(count)
                for status, count in pd.Series([r["peer_reference_status"] for r in rallies]).value_counts().items()
            },
            "peer_interpretation": "At least three same-sublane peers, same pricing date and trailing/forward PE basis, from one pre-publication snapshot; target implied earnings held static. Valuation scenario after independently observed sentiment fade, never a short signal or fair value.",
            "available_count": len(anchor_rows),
            "reference_below_event_close_count": sum(r["pe_reference_gap_pct_provisional"] < 0 for r in anchor_rows),
            "interpretation": "Legacy own-history diagnostic only: prior 30-day median archived PE times implied earnings. PE basis is unverified; do not use as the peer reference.",
        },
        "comparison_rally_count": len(comparison),
        "groups": groups,
        "by_keyword_event_type": by_type,
        "ticker_clustered_news_minus_comparison": {
            f"{h}d": clustered_contrast(rallies, comparison, h) for h in HORIZONS
        },
        "limitations": [
            "News mentions are not verified causal catalysts; event_type is a loose keyword label.",
            "Archive coverage is intermittent; absence of later articles cannot establish sentiment fade.",
            "The comparison group lacks an archived article, not necessarily real-world news; it is not matched on sector, volatility, or capitalization.",
            "PE/price pairs are candidate-selected dashboard snapshots. PE may mix trailing and forward bases; the provisional reference assumes unchanged earnings and is not a fair value estimate.",
            "Same-sublane peer reference needs at least three peers in one archived snapshot with identical PE basis and pricing date; old snapshots lack PE type and may have no usable peer coverage.",
            "Cached Close prices are not corporate-action adjusted; split-affected rows require manual review.",
            "No trading costs, sector/beta adjustment, or historical option returns; do not use as a trade signal.",
        ],
    }
    return report, events


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("/app/agent/data_cache/easymoneysniper.sqlite3"))
    parser.add_argument("--prices", type=Path, default=Path("/app/agent/data_cache/market_data/ohlcv_daily"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report, events = run_study(args.db, args.prices)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "event_reversal_summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    events.to_csv(args.output / "event_reversal_events.csv", index=False)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
