"""Portfolio holdings advisor: turn manually-recorded positions into per-holding
ADD / HOLD / TRIM / CLOSE decisions with concrete price levels.

The decision logic is deliberately risk-first and grounded in the only edge that
survived validation (the pullback + vol-expansion / oversold mean-reversion read,
exposed via ``priority_board_service.single_stock_signal_read``):

  1. CLOSE  -- hard protective stop hit, or unrealized loss exceeds the per-trade
     risk budget. Capital preservation overrides everything else.
  2. TRIM   -- position over-concentrated (> max position ratio), OR a large
     unrealized gain on an over-extended (non-pullback) name -> lock partial,
     trail the stop up.
  3. ADD    -- ONLY when a *validated* edge is present today (calibrated win-rate
     >= threshold AND the name is in a pullback/oversold state) AND the market
     regime is risk-on AND there is room under the max-position cap AND cash to
     buy. Add size is risk-budget-based (same philosophy as trade_risk_rules).
  4. HOLD   -- the default: above stop, nothing to add or trim.

``decide_holding`` is a pure function (no I/O) so it can be unit-tested.
``compute_portfolio_view`` does the data fetching and aggregation.

Keep code ASCII; UI strings may be Chinese like the rest of the services.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

from app_database import (
    cache_get,
    cache_set,
    portfolio_account_get,
    portfolio_holdings_list,
)
from distribution_risk_service import detect_distribution_risk
from trade_risk_rules import MAX_POSITION_RATIO, RISK_PER_TRADE

_CACHE_KEY_PREFIX = "portfolio_view:v1"
_CACHE_TTL_SECONDS = 300

# --- decision constants (tunable in one place) -----------------------------
HARD_STOP_FROM_COST = 0.08      # protective stop: 8% below average cost
TRAIL_STOP_ATR_MULT = 2.0       # trailing stop = price - 2*ATR once in profit
BREAKEVEN_TRIGGER = 0.05        # lift stop to >= breakeven after +5%
ADD_WINRATE_MIN = 0.52          # only add when calibrated edge clears coin-flip
REGIME_RISK_ON = 0.80           # gross-exposure multiplier floor to allow adds
OVEREXTENDED_GAIN = 0.20        # +20% unrealized triggers a trim review
REWARD_RISK = 2.0               # take-profit at 2R above current
EARNINGS_BUFFER_DAYS = 7        # do not ADD within this many days of earnings

# action codes
ADD = "ADD"
HOLD = "HOLD"
TRIM = "TRIM"
CLOSE = "CLOSE"

_ACTION_CN = {
    ADD: "加仓",
    HOLD: "持有",
    TRIM: "减仓",
    CLOSE: "平仓",
}
_ACTION_TONE = {
    ADD: "buy",
    HOLD: "hold",
    TRIM: "warn",
    CLOSE: "sell",
}

# Pullback states (from priority_board_service._pullback_state) that represent a
# genuine oversold/pullback entry -- the only condition the validated edge fires on.
_PULLBACK_STATES = {"深度回调(超卖)", "回调中"}


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def decide_holding(
    *,
    shares: float,
    avg_cost: float,
    current_price: float,
    atr: Optional[float],
    calibrated_win_rate: Optional[float],
    confidence_badge: Optional[str],
    pullback_state: Optional[str],
    hv_state: Optional[str],
    relative_strength: Optional[float],
    regime_mult: float,
    available_cash: float,
    total_equity: float,
) -> Dict[str, Any]:
    """Pure decision for one holding. Returns action + reason + price levels.

    All money/level outputs are rounded for display. ``add_qty`` is a risk-budget
    based suggestion (0 when ADD does not apply).
    """
    shares = max(0.0, _finite(shares))
    cost = _finite(avg_cost)
    px = _finite(current_price)
    if px <= 0 or cost <= 0:
        return {
            "action": HOLD,
            "action_cn": "数据不足",
            "tone": "hold",
            "reason": "缺少有效现价或成本，无法给出决策。",
            "levels": {},
        }
    atr_v = atr if (atr and atr > 0) else max(px * 0.025, 0.01)

    position_value = shares * px
    unrealized_pct = px / cost - 1.0 if cost > 0 else 0.0
    win = calibrated_win_rate
    badge = (confidence_badge or "").lower()
    in_pullback = (pullback_state or "") in _PULLBACK_STATES

    # --- stop construction ---------------------------------------------------
    hard_stop = cost * (1.0 - HARD_STOP_FROM_COST)
    trail_stop = px - TRAIL_STOP_ATR_MULT * atr_v
    if unrealized_pct >= BREAKEVEN_TRIGGER:
        # In profit: trail up but never below breakeven (lock the trade green).
        stop = max(trail_stop, cost)
    else:
        stop = hard_stop
    stop = max(0.01, stop)

    per_share_risk = max(0.01, px - stop)
    take_profit = px + REWARD_RISK * per_share_risk
    # An add should happen on a dip toward / just under current price.
    add_zone_high = px
    add_zone_low = max(0.01, min(px - 0.5 * atr_v, px * 0.985))

    risk_budget = max(0.0, total_equity) * RISK_PER_TRADE
    room_value = max(0.0, MAX_POSITION_RATIO * max(0.0, total_equity) - position_value)
    qty_by_risk = int(risk_budget / per_share_risk) if per_share_risk > 0 else 0
    qty_by_cash = int(max(0.0, available_cash) / px) if px > 0 else 0
    qty_by_room = int(room_value / px) if px > 0 else 0
    add_qty = max(0, min(qty_by_risk, qty_by_cash, qty_by_room))

    levels = {
        "stop_price": round(stop, 2),
        "stop_pct": round(stop / px - 1.0, 4),
        "take_profit": round(take_profit, 2),
        "take_profit_pct": round(take_profit / px - 1.0, 4),
        "add_zone_low": round(add_zone_low, 2),
        "add_zone_high": round(add_zone_high, 2),
    }

    # --- priority-ordered decision ------------------------------------------
    # 1) CLOSE: protective stop breached.
    if px <= hard_stop:
        return _decision(CLOSE, "现价已跌破保护止损（成本下 8%），按纪律清仓止损。", levels, 0)

    # 1b) CLOSE: single-position loss already at/over the per-trade risk budget.
    unrealized_loss = max(0.0, -(px - cost) * shares)
    if risk_budget > 0 and unrealized_loss >= risk_budget and unrealized_pct < 0:
        return _decision(
            CLOSE,
            f"该仓位浮亏 ${unrealized_loss:,.0f} 已达单笔风险预算（账户 {RISK_PER_TRADE*100:.1f}%），离场控损。",
            levels, 0,
        )

    # 2) TRIM: over-concentrated relative to max position ratio.
    if total_equity > 0 and position_value > MAX_POSITION_RATIO * total_equity * 1.02:
        target_value = MAX_POSITION_RATIO * total_equity
        trim_shares = int(max(0.0, (position_value - target_value) / px))
        levels["trim_shares"] = trim_shares
        return _decision(
            TRIM,
            f"仓位占账户 {position_value/total_equity*100:.0f}%，超过 {MAX_POSITION_RATIO*100:.0f}% 上限，"
            f"建议减约 {trim_shares} 股降低集中度，止损上移至 ${stop:.2f}。",
            levels, 0,
        )

    # 2b) TRIM: big gain on an over-extended (non-pullback) name -> lock partial.
    if unrealized_pct >= OVEREXTENDED_GAIN and not in_pullback:
        return _decision(
            TRIM,
            f"浮盈 {unrealized_pct*100:.0f}% 且形态偏追高（{pullback_state or '非回调'}），"
            f"建议减半锁定利润，剩余仓位止损上移至 ${stop:.2f}（已保盈）。",
            levels, 0,
        )

    # 3) ADD: validated edge + pullback + risk-on + room + cash.
    edge_ok = (win is not None and win >= ADD_WINRATE_MIN and badge != "experimental")
    if edge_ok and in_pullback and regime_mult >= REGIME_RISK_ON and add_qty >= 1:
        levels["add_qty"] = add_qty
        rs_txt = f"，相对大盘 {relative_strength*100:+.1f}%" if relative_strength is not None else ""
        hv_txt = f"，{hv_state}" if hv_state else ""
        return _decision(
            ADD,
            f"校准胜率 {win*100:.0f}%（已验证）+ {pullback_state}{hv_txt}{rs_txt}，市况偏多。"
            f"可在 ${add_zone_low:.2f}~${add_zone_high:.2f} 加约 {add_qty} 股，止损 ${stop:.2f}。",
            levels, add_qty,
        )

    # 4) HOLD: explain why not adding (the most useful part for the user).
    reasons = []
    if win is None:
        reasons.append("暂无校准胜率")
    elif win < ADD_WINRATE_MIN:
        reasons.append(f"校准胜率 {win*100:.0f}% 未过 {ADD_WINRATE_MIN*100:.0f}% 加仓线")
    if not in_pullback:
        reasons.append(f"形态非回调（{pullback_state or '未知'}）")
    if regime_mult < REGIME_RISK_ON:
        reasons.append(f"市况择时偏谨慎(×{regime_mult:.2f})")
    if edge_ok and in_pullback and add_qty < 1:
        reasons.append("无加仓额度（现金/仓位上限/风险预算受限）")
    why = "；".join(reasons) if reasons else "条件未触发加减仓"
    return _decision(
        HOLD,
        f"持有观望：{why}。止损 ${stop:.2f}，止盈参考 ${take_profit:.2f}。",
        levels, 0,
    )


def _decision(action: str, reason: str, levels: Dict[str, Any], add_qty: int) -> Dict[str, Any]:
    return {
        "action": action,
        "action_cn": _ACTION_CN.get(action, action),
        "tone": _ACTION_TONE.get(action, "hold"),
        "reason": reason,
        "add_qty": int(add_qty),
        "levels": levels,
    }


# ---------------------------------------------------------------------------
# Aggregation: load holdings, fetch prices/signals, decide, total up.
# ---------------------------------------------------------------------------

def _atr(frame: pd.DataFrame, window: int = 14) -> Optional[float]:
    if frame is None or frame.empty or len(frame) < 5:
        return None
    high = pd.to_numeric(frame.get("High"), errors="coerce")
    low = pd.to_numeric(frame.get("Low"), errors="coerce")
    close = pd.to_numeric(frame.get("Close"), errors="coerce")
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1).dropna()
    if tr.empty:
        return None
    value = float(tr.tail(window).mean())
    return round(value, 4) if math.isfinite(value) and value > 0 else None


def _holdings_cache_key(holdings: List[Dict[str, Any]], cash: float) -> str:
    """Cache key tied to holdings + cash so any edit naturally busts the cache."""
    basis = [(h.get("symbol"), round(_finite(h.get("shares")), 4), round(_finite(h.get("avg_cost")), 4))
             for h in holdings]
    digest = hashlib.sha256(json.dumps([basis, round(cash, 2)], sort_keys=True).encode()).hexdigest()[:20]
    return f"{_CACHE_KEY_PREFIX}:{digest}"


def _system_expected_excess() -> Optional[float]:
    """Fallback expected 8-day net excess for validated deep-oversold picks: the
    active pullback_hv top-bucket mean net excess (the system's measured edge),
    used when a pick's own score bucket is too thin to give a per-name estimate."""
    try:
        from app_database import signal_calibration_active
        from priority_board_service import PULLBACK_HV_HORIZON
        active = signal_calibration_active("pullback_hv", PULLBACK_HV_HORIZON)
        tb = ((((active or {}).get("curve") or {}).get("global") or {}).get("edge") or {}).get("top_bucket") or {}
        v = tb.get("mean_excess_net")
        return float(v) if v is not None else None
    except Exception:
        return None


def _news_risk(symbol: str) -> Dict[str, Any]:
    """Recent (≤10d) news sentiment for a symbol from the Massive/Polygon news
    `insights` (already entitled). Net-negative recent coverage => 舆情 risk."""
    key = f"news_risk:v1:{symbol}"
    cached = cache_get(key)
    if isinstance(cached, dict):
        return cached
    out: Dict[str, Any] = {"pos": 0, "neg": 0, "neu": 0, "n": 0, "risk": False, "top_negative": None}
    try:
        from market_data_service import _massive_get
        cutoff = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        payload = _massive_get("/v2/reference/news", ticker=symbol, limit=25)
        for r in ((payload or {}).get("results") or []):
            if str(r.get("published_utc") or "") < cutoff:
                continue
            for ins in (r.get("insights") or []):
                if str(ins.get("ticker") or "").upper() != symbol.upper():
                    continue
                s = ins.get("sentiment")
                if s == "positive":
                    out["pos"] += 1
                elif s == "negative":
                    out["neg"] += 1
                    if not out["top_negative"]:
                        out["top_negative"] = (ins.get("sentiment_reasoning") or "")[:140]
                else:
                    out["neu"] += 1
        out["n"] = out["pos"] + out["neg"] + out["neu"]
        out["risk"] = bool(out["neg"] >= 2 and out["neg"] > out["pos"])
    except Exception:
        pass
    cache_set(key, out, ttl_seconds=2 * 3600)
    return out


def _news_label(news: Dict[str, Any]) -> str:
    if news.get("n", 0) == 0:
        return "舆情·无近况"
    if news.get("neg", 0) >= 1 and news.get("neg", 0) >= news.get("pos", 0):
        return "舆情·偏负"
    if news.get("pos", 0) > news.get("neg", 0):
        return "舆情·偏正"
    return "舆情·中性"


# Coarse GICS-ish buckets so a GICS "Technology" sector and a SIC
# "Services-Prepackaged Software" industry both canonicalize to the same 赛道
# (the two sources disagree on granularity; without this, same-sector slips through).
_SECTOR_KEYWORDS = [
    ("technology", ["software", "semiconductor", "computer", "information technology", "internet", "electronic", "tech", "data storage", "cloud"]),
    ("healthcare", ["health", "pharma", "biotech", "medical", "drug", "life science"]),
    ("financial", ["bank", "insurance", "financ", "capital market", "asset manage", "brokerage", "credit"]),
    ("energy", ["oil", "gas", "petroleum", "energy", "coal"]),
    ("industrials", ["trucking", "airline", "machinery", "industrial", "aerospace", "defense", "freight", "railroad", "logistics", "construction"]),
    ("materials", ["chemical", "mining", "metal", "material", "steel", "gold", "fertiliz"]),
    ("communication", ["telecom", "media", "advertis", "broadcast", "entertainment", "publishing", "gaming"]),
    ("consumer", ["retail", "apparel", "restaurant", "consumer", "beverage", "food", "auto", "leisure", "lodging", "footwear"]),
    ("real estate", ["reit", "real estate"]),
    ("utilities", ["utilit", "electric power", "water"]),
]


def _canon_sector(sector: Optional[str], industry: Optional[str]) -> Optional[str]:
    text = f"{sector or ''} {industry or ''}".lower()
    for bucket, kws in _SECTOR_KEYWORDS:
        if any(k in text for k in kws):
            return bucket
    return ((sector or industry or "").strip().lower()) or None


def _symbol_sector(symbol: str) -> Optional[str]:
    try:
        from priority_board_service import _symbol_track
        tr = _symbol_track(symbol) or {}
        return _canon_sector(tr.get("sector"), tr.get("industry"))
    except Exception:
        return None


def _rotation_candidates(held: set, avoid_sectors: set, limit: int = 3) -> List[Dict[str, Any]]:
    """High-liquidity + deep-oversold rotation targets from the priority board.
    Excludes: names already held; the SAME 赛道 as anything being sold (avoid
    re-concentrating); and names with recent net-negative 舆情 (news sentiment)."""
    try:
        from priority_board_service import compute_priority_board
        board = compute_priority_board(limit=60)
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for p in (board.get("picks") or []):
        sym = str(p.get("symbol") or "").upper()
        if not sym or sym in held:
            continue
        dov = (p.get("deep_oversold") or {}).get("level")
        liq_tone = (p.get("liquidity") or {}).get("tone")
        win = _finite(p.get("calibrated_probability"))
        if dov not in ("deep", "oversold"):
            continue
        if liq_tone not in ("strong", "good"):  # high liquidity only
            continue
        if win < 0.50 or p.get("confidence_badge") == "experimental":
            continue
        td = p.get("track_detail") or {}
        sector = _canon_sector(td.get("sector"), td.get("industry"))
        if sector and sector in avoid_sectors:
            continue  # same 赛道 as a name being sold -> avoid re-concentration
        news = _news_risk(sym)
        if news.get("risk"):
            continue  # recent net-negative 舆情 -> avoid
        out.append({
            "symbol": sym,
            "current_price": _finite(p.get("current_price")),
            "calibrated_win_rate": win,
            "expected_return_8d": p.get("expected_return_8d"),
            "expected_excess_8d": p.get("expected_excess_8d"),
            "liquidity_tier": (p.get("liquidity") or {}).get("tier"),
            "deep_oversold_level": dov,
            "deep_oversold_cn": "深度超卖" if dov == "deep" else "超卖",
            "sector": p.get("track_cn") or p.get("track"),
            "news_label": _news_label(news),
            "news_neg": news.get("neg", 0),
            "news_pos": news.get("pos", 0),
            "detail_url": p.get("detail_url") or f"/single-stock-overnight?symbol={sym}",
        })
        if len(out) >= limit:
            break
    # Fill missing per-name expected excess with the system edge estimate so the
    # user always gets a "预计收益" figure (flagged as a system estimate).
    sys_exc = _system_expected_excess()
    for t in out:
        if t.get("expected_excess_8d") is None and sys_exc is not None:
            t["expected_excess_8d"] = round(sys_exc, 6)
            t["excess_is_estimate"] = True
    return out


def _rotation_for(freed_capital: float, targets: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Attach per-target share/return estimates for a given freed-capital amount."""
    enriched = []
    for t in targets:
        price = _finite(t.get("current_price"))
        er = t.get("expected_return_8d")
        ex = t.get("expected_excess_8d")
        shares = int(freed_capital // price) if price > 0 else 0
        enriched.append({
            **t,
            "est_shares": shares,
            "est_gain_8d": round(freed_capital * er, 2) if er is not None else None,
            "est_excess_8d": round(freed_capital * ex, 2) if ex is not None else None,
        })
    return {"freed_capital": round(freed_capital, 2), "targets": enriched}


def compute_portfolio_view(force_refresh: bool = False) -> Dict[str, Any]:
    """Load manual holdings + cash, score each, and aggregate account totals.

    Cached for a few minutes keyed by the holdings+cash signature, and computed
    with a single batched price download + one benchmark/calibration lookup, so
    repeat loads are instant and a first load scales with one network round-trip
    instead of ~3 per holding.
    """
    from market_data_service import download_daily_history, get_daily_history, get_next_earnings
    from portfolio_timing_service import portfolio_timing_gate
    from priority_board_service import _select_signals, _spy_return, single_stock_signal_read

    account = portfolio_account_get()
    cash = _finite(account.get("available_cash"))
    holdings = portfolio_holdings_list()

    cache_key = _holdings_cache_key(holdings, cash)
    if not force_refresh:
        cached = cache_get(cache_key)
        if isinstance(cached, dict):
            return {**cached, "cache_hit": True}

    try:
        gate = portfolio_timing_gate()
        regime_mult = _finite(gate.get("gross_exposure_multiplier"), 1.0)
        regime_cn = gate.get("regime_cn") or gate.get("regime") or ""
    except Exception:
        regime_mult, regime_cn = 1.0, ""

    # Fetch everything shared ONCE: batch price download, benchmark return, and
    # the active calibration selection (instead of redoing all three per holding).
    symbols = [str(h.get("symbol") or "").upper() for h in holdings if h.get("symbol")]
    frames: Dict[str, pd.DataFrame] = {}
    if symbols:
        try:
            from launch_signal_service import _ticker_frame

            bulk, _sources = download_daily_history(symbols, period="6mo")
            for sym in symbols:
                try:
                    frames[sym] = _ticker_frame(bulk, sym)
                except Exception:
                    frames[sym] = None
        except Exception:
            frames = {}
    try:
        spy_ret = _spy_return(20)
    except Exception:
        spy_ret = 0.0
    try:
        selected = _select_signals()
    except Exception:
        selected = None

    # First pass: prices + market value, so total_equity is known before deciding.
    enriched: List[Dict[str, Any]] = []
    market_value = 0.0
    for h in holdings:
        symbol = str(h.get("symbol") or "").upper()
        shares = _finite(h.get("shares"))
        avg_cost = _finite(h.get("avg_cost"))
        price, atr, signal, distribution_risk = None, None, {}, {"available": False, "triggered": False}
        try:
            frame = frames.get(symbol)
            if frame is None or (hasattr(frame, "empty") and frame.empty):
                frame, _src = get_daily_history(symbol, period="6mo")
            if frame is not None and not frame.empty:
                price = float(pd.to_numeric(frame["Close"], errors="coerce").dropna().iloc[-1])
                atr = _atr(frame)
                signal = single_stock_signal_read(symbol, frame=frame, spy_ret=spy_ret, selected=selected) or {}
                distribution_risk = detect_distribution_risk(frame)
            else:
                signal = {"available": False, "reason": "无行情数据"}
        except Exception:
            signal = {"available": False, "reason": "行情/信号获取失败"}
        try:
            earnings = get_next_earnings(symbol) or {}
        except Exception:
            earnings = {}
        price = price if (price and price > 0) else avg_cost
        pos_value = shares * (price or 0)
        market_value += pos_value
        enriched.append({
            "symbol": symbol, "shares": shares, "avg_cost": avg_cost,
            "note": h.get("note") or "", "current_price": price, "atr": atr,
            "signal": signal, "position_value": pos_value, "earnings": earnings,
            "distribution_risk": distribution_risk,
        })

    total_equity = market_value + cash

    items: List[Dict[str, Any]] = []
    action_counts = {ADD: 0, HOLD: 0, TRIM: 0, CLOSE: 0}
    total_cost = 0.0
    for e in enriched:
        sig = e["signal"] or {}
        decision = decide_holding(
            shares=e["shares"], avg_cost=e["avg_cost"], current_price=e["current_price"],
            atr=e["atr"], calibrated_win_rate=sig.get("calibrated_win_rate"),
            confidence_badge=sig.get("confidence_badge"),
            pullback_state=sig.get("pullback_state"),
            hv_state=(sig.get("hv") or {}).get("hv_state") if isinstance(sig.get("hv"), dict) else None,
            relative_strength=sig.get("relative_strength_20d"),
            regime_mult=regime_mult, available_cash=cash, total_equity=total_equity,
        )
        # Earnings-proximity guard: never ADD within the buffer window before a
        # scheduled report (the biggest overnight tail risk). Flag holds too.
        earn = e.get("earnings") or {}
        du = earn.get("days_until")
        near_earnings = isinstance(du, int) and 0 <= du <= EARNINGS_BUFFER_DAYS
        if near_earnings and decision["action"] == ADD:
            decision = {
                **decision,
                "action": HOLD, "action_cn": "持有", "tone": "hold",
                "reason": f"临近财报（{du}天后 {earn.get('next_date')}），暂不加仓以避开财报暴雷；财报落地后再评估。",
            }
        elif near_earnings:
            decision = {**decision, "reason": f"⚠️ {du}天后财报（{earn.get('next_date')}），持仓需注意暴雷风险。" + decision["reason"]}
        dist = e.get("distribution_risk") or {}
        if dist.get("triggered"):
            prefix = f"⚠️ {dist.get('label') or '高位派发风险'}：{'；'.join((dist.get('reasons') or [])[:3])}。"
            if decision["action"] == ADD:
                decision = {
                    **decision,
                    "action": HOLD, "action_cn": "鎸佹湁", "tone": "hold",
                    "reason": prefix + "暂不加仓，先观察是否放量跌破或重新站稳高点。",
                }
            elif decision["action"] in (HOLD, TRIM):
                decision = {**decision, "reason": prefix + decision["reason"]}
        action_counts[decision["action"]] = action_counts.get(decision["action"], 0) + 1
        cost_value = e["shares"] * e["avg_cost"]
        total_cost += cost_value
        unreal = e["position_value"] - cost_value
        items.append({
            "symbol": e["symbol"],
            "shares": e["shares"],
            "avg_cost": round(e["avg_cost"], 2),
            "current_price": round(e["current_price"], 2) if e["current_price"] else None,
            "note": e["note"],
            "position_value": round(e["position_value"], 2),
            "cost_value": round(cost_value, 2),
            "unrealized_pnl": round(unreal, 2),
            "unrealized_pct": round(unreal / cost_value, 4) if cost_value > 0 else None,
            "weight_pct": round(e["position_value"] / total_equity, 4) if total_equity > 0 else None,
            "atr14": e["atr"],
            "calibrated_win_rate": sig.get("calibrated_win_rate"),
            "confidence_badge": sig.get("confidence_badge"),
            "pullback_state": sig.get("pullback_state"),
            "hv_state": (sig.get("hv") or {}).get("hv_state") if isinstance(sig.get("hv"), dict) else None,
            "relative_strength_20d": sig.get("relative_strength_20d"),
            "distribution_risk": e.get("distribution_risk") or {"available": False, "triggered": False},
            "signal_available": bool(sig.get("available")),
            "earnings": {
                "next_date": earn.get("next_date"),
                "days_until": earn.get("days_until"),
                "last_surprise_pct": earn.get("last_surprise_pct"),
            },
            "decision": decision,
            "detail_url": f"/single-stock-overnight?symbol={e['symbol']}",
        })

    # Rotation: for every TRIM/CLOSE, suggest WHERE the freed capital should go
    # (high-liquidity + deep-oversold board picks the user does not already hold)
    # with an expected 8-day net-return estimate, instead of just "sell".
    held_syms = {it["symbol"] for it in items}
    sell_syms = [it["symbol"] for it in items if it["decision"]["action"] in (TRIM, CLOSE)]
    avoid_sectors = set()
    for s in sell_syms:  # don't rotate into the same 赛道 we're reducing
        sec = _symbol_sector(s)
        if sec:
            avoid_sectors.add(sec)
    rotation_targets = _rotation_candidates(held_syms, avoid_sectors, limit=3) if sell_syms else []
    pool_capital = 0.0
    if rotation_targets:
        max_pos_value = MAX_POSITION_RATIO * total_equity if total_equity > 0 else 0.0
        for it in items:
            act = it["decision"]["action"]
            pv = _finite(it["position_value"])
            if act == CLOSE:
                freed = pv
            elif act == TRIM:
                freed = max(pv - max_pos_value, 0.0) or round(pv / 3.0, 2)  # excess over cap, else ~1/3
            else:
                continue
            pool_capital += freed
            it["decision"]["rotation"] = _rotation_for(freed, rotation_targets)

    # Most actionable first: CLOSE > TRIM > ADD > HOLD, then by position size.
    order = {CLOSE: 0, TRIM: 1, ADD: 2, HOLD: 3}
    items.sort(key=lambda it: (order.get(it["decision"]["action"], 9), -_finite(it["position_value"])))

    total_unreal = market_value - total_cost
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cache_hit": False,
        "account": {
            "available_cash": round(cash, 2),
            "currency": account.get("currency") or "USD",
            "market_value": round(market_value, 2),
            "total_equity": round(total_equity, 2),
            "cost_basis": round(total_cost, 2),
            "unrealized_pnl": round(total_unreal, 2),
            "unrealized_pct": round(total_unreal / total_cost, 4) if total_cost > 0 else None,
            "cash_pct": round(cash / total_equity, 4) if total_equity > 0 else None,
            "invested_pct": round(market_value / total_equity, 4) if total_equity > 0 else None,
            "updated_at": account.get("updated_at"),
        },
        "regime": {"gross_exposure_multiplier": round(regime_mult, 4), "regime_cn": regime_cn},
        "action_counts": action_counts,
        "rotation": ({
            "pool_capital": round(pool_capital, 2),
            "targets": _rotation_for(pool_capital, rotation_targets)["targets"],
            "note": "减仓/平仓腾出的资金可换入这些高流动性·深超卖标的（系统验证过的回调买入 edge 集中处）。预计收益=校准的 8 日净成本收益期望，非保证，edge 偏小。",
        } if rotation_targets else None),
        "holdings": items,
        "n_holdings": len(items),
        "method_note": (
            "决策优先级：平仓(止损/超风险预算) > 减仓(超集中度/追高兑现) > 加仓(已验证胜率+回调+市况偏多+有额度) > 持有。"
            "加仓额度按账户单笔风险预算 1.5% 与 35% 单仓上限取小。所有价位为研究参考，非下单指令。"
        ),
    }
    cache_set(cache_key, result, ttl_seconds=_CACHE_TTL_SECONDS)
    return result
