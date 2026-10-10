"""Manual, cost-capped long-call comparisons; scenarios are not probabilities."""
from __future__ import annotations

import math
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from statistics import NormalDist

import pandas as pd

from app_database import cache_get, cache_set
from consensus_signal_service import consensus_feature_frame
from market_calendar import is_trading_day, most_recent_session, session_close_et
from market_data_service import (_CACHE_ROOT, _bs_option_price, _implied_vol_from_mid,
                                 external_data_scope, get_daily_history, get_next_earnings)
from scripts.backtest_leader_long_options import CostCappedHistorical, EASTERN, MAGNIFICENT_SEVEN

VERSION = 1
_LOCK = threading.Lock()
_ACTIVE: set[str] = set()


@dataclass(frozen=True)
class PlanConfig:
    capital: float = 2000.0
    risk_fraction: float = 0.015
    hold_days: int = 10
    fee: float = 0.65
    max_spread: float = 0.10
    slippage: float = 0.02
    min_delta: float = 0.45
    max_delta: float = 0.75
    earnings_buffer_days: int = 2


def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def forward_sessions(start, count):
    days, current = [], start
    while len(days) < count:
        if is_trading_day(current):
            days.append(current)
        current += timedelta(days=1)
    return days


def quote_window(session):
    close = session_close_et(session).astimezone(timezone.utc)
    return (close - timedelta(minutes=5)).isoformat(), close.isoformat()


def event_window(earnings, start, config):
    days = forward_sessions(start, config.hold_days + config.earnings_buffer_days + 1)
    last_exit = days[config.hold_days - 1]
    raw = (earnings or {}).get("next_date")
    try:
        release = date.fromisoformat(str(raw)[:10])
        if release < start:
            raise ValueError("expired calendar")
    except (ValueError, TypeError):
        return {"status": "unknown", "next_date": None, "latest_exit_date": last_exit.isoformat(),
                "risk_flags": ["财报日期未知，须先人工核验；突发事件无法预先排除"]}
    safe = [d for d in days[:config.hold_days] if
            len([s for s in forward_sessions(d, config.hold_days + 10) if s < release]) > config.earnings_buffer_days]
    blocked = not safe
    return {"status": "blocked" if blocked else "shortened" if len(safe) < config.hold_days else "clear_known_earnings",
            "next_date": release.isoformat(), "source": (earnings or {}).get("source"),
            "latest_exit_date": safe[-1].isoformat() if safe else None,
            "hold_days_allowed": len(safe),
            "risk_flags": ["财报临近，不生成入场计划"] if blocked else ["须在财报前缓冲窗口退出"] if len(safe) < config.hold_days else [],
            "note": "仅检查缓存中已知财报日期；不是完整重大事件日历"}


def select_definitions(definitions, spot, quote_day, plan_day):
    """Bounded real OCC contracts, three expiries and four strikes each."""
    needed = {"expiration", "strike_price", "instrument_class"}
    if definitions is None or definitions.empty or not needed.issubset(definitions.columns):
        return []
    rows = []
    for _, record in definitions.iterrows():
        if str(record.get("instrument_class", "")).upper() != "C":
            continue
        try:
            expiry = pd.Timestamp(record["expiration"]).date()
            strike = number(record["strike_price"])
            raw = str(record.get("raw_symbol") or record.get("symbol") or "")
            match = re.search(r"(\d{6})C(\d{8})$", raw)
            if (not match or expiry.strftime("%y%m%d") != match[1] or not strike or strike <= 0
                    or abs(int(match[2]) / 1000 - strike) > 0.001
                    or not 60 <= (expiry - plan_day).days <= 90 or not 0.85 <= strike / spot <= 1.08):
                continue
            multiplier = number(record.get("contract_multiplier"))
            if multiplier in (None, 2147483647):
                multiplier = number(record.get("unit_of_measure_qty"))
            if multiplier is not None and multiplier != 100:
                continue
            # Adjusted option roots ending in a digit are excluded even if K matches.
            if re.search(r"\d$", raw[:match.start()].strip()):
                continue
            rows.append({"contract": raw, "expiry": expiry.isoformat(), "strike": strike,
                         "dte": (expiry - plan_day).days, "quote_dte": (expiry - quote_day).days,
                         "multiplier": 100, "multiplier_verified": multiplier == 100})
        except (TypeError, ValueError, OverflowError):
            continue
    expiry_order = sorted({r["expiry"] for r in rows}, key=lambda e: (abs((date.fromisoformat(e) - plan_day).days - 75), e))[:3]
    out = []
    for expiry in expiry_order:
        available = [r for r in rows if r["expiry"] == expiry]
        candidates = [min(available, key=lambda r: (abs(r["strike"] / spot - target), r["contract"]))
                      for target in (0.95, 0.98, 1.0, 1.04)]
        seen = set()
        for row in candidates:
            if row["contract"] not in seen:
                out.append(row)
                seen.add(row["contract"])
    return out


def evaluate_quote(contract, quotes, spot, quote_day, config):
    if quotes is None or quotes.empty or not {"symbol", "bid_px_00", "ask_px_00"}.issubset(quotes.columns):
        return None
    q = quotes[quotes["symbol"].astype(str) == contract["contract"]].copy()
    q.index = pd.to_datetime(q.index, utc=True, errors="coerce")
    end = pd.Timestamp(session_close_et(quote_day)).tz_convert("UTC")
    q = q[(q.index >= end - pd.Timedelta(minutes=5)) & (q.index < end)].sort_index()
    if q.empty:
        return None
    last = q.iloc[-1]
    bid, ask = number(last.get("bid_px_00")), number(last.get("ask_px_00"))
    if bid is None or ask is None or not 0 < bid <= ask:
        return None
    mid = (bid + ask) / 2
    sigma = _implied_vol_from_mid(spot, contract["strike"], contract["quote_dte"], mid, "call")
    if (sigma is None or mid <= max(0, spot - contract["strike"] * math.exp(-0.04 * contract["quote_dte"] / 365))
            or mid >= spot or abs(_bs_option_price(spot, contract["strike"], contract["quote_dte"], sigma, "call") - mid) > max(0.02, mid * 0.01)):
        return None
    t = contract["quote_dte"] / 365
    d1 = (math.log(spot / contract["strike"]) + (0.04 + sigma ** 2 / 2) * t) / (sigma * math.sqrt(t))
    delta = NormalDist().cdf(d1)
    spread = (ask - bid) / ask
    valid = (pd.to_numeric(q["bid_px_00"], errors="coerce") > 0) & (pd.to_numeric(q["ask_px_00"], errors="coerce") >= pd.to_numeric(q["bid_px_00"], errors="coerce"))
    samples = q.index[valid].floor("min").nunique()
    size_bid, size_ask = number(last.get("bid_sz_00")), number(last.get("ask_sz_00"))
    flags = []
    if spread > config.max_spread:
        flags.append("价差过宽")
    if samples < 3 or (end - q.index[-1]).total_seconds() > 120:
        flags.append("报价连续性不足")
    if size_bid is None or size_ask is None or min(size_bid, size_ask) < 1:
        flags.append("双边报价数量未核验")
    if not config.min_delta <= delta <= config.max_delta:
        flags.append("Delta不在候选区间")
    if not contract["multiplier_verified"]:
        flags.append("100股交割乘数待核验")
    premium = ask * 100 + config.fee
    planned_loss = ask * 100 * 0.25 + 2 * config.fee
    if premium > config.capital:
        flags.append("一张成本超过资金")
    if planned_loss > config.capital * config.risk_fraction:
        flags.append("计划止损风险超预算")
    return {**contract, "bid": bid, "ask": ask, "iv": sigma, "delta": round(delta, 4),
            "quote_at": q.index[-1].isoformat(), "spread_fraction": round(spread, 4),
            "bid_size": size_bid, "ask_size": size_ask, "quote_samples": int(samples),
            "cash_usd": round(premium, 2), "planned_stop_risk_usd": round(planned_loss, 2),
            "max_loss_usd": round(premium, 2), "risk_flags": flags, "comparison_eligible": not flags,
            "target_exit_bid": round(ask * 1.5, 4), "stop_exit_bid": round(ask * 0.75, 4),
            "volume": None, "open_interest": None,
            "pricing_basis": "BS欧式近似，r=4%，未计股息/美式提前行权；IV用同日正股收盘代理和该合约中间价反推，非精确逐笔同步"}


def exit_scenarios(contract, spot, support, breakout, start, event, config):
    """Crossed price/time/IV stress grid, with no invented joint probabilities."""
    sessions = forward_sessions(start, config.hold_days)
    rows = []
    targets = {"原地不涨": spot, "上行5%": spot * 1.05}
    if support and 0 < support < spot:
        targets["失效支撑"] = support
    if breakout and breakout > spot:
        targets["突破参考"] = breakout
    expiry = date.fromisoformat(contract["expiry"])
    for horizon in sorted({1, 3, 5, 8, config.hold_days}):
        if horizon > config.hold_days:
            continue
        day = sessions[horizon - 1]
        if event.get("latest_exit_date") and day.isoformat() > event["latest_exit_date"]:
            continue
        if event["status"] == "blocked":
            continue
        dte = (expiry - day).days
        for label, target in targets.items():
            outcomes = {}
            for iv_label, factor in (("iv_down", 0.8), ("iv_flat", 1.0), ("iv_up", 1.2)):
                price = _bs_option_price(target, contract["strike"], dte, contract["iv"] * factor, "call")
                # Model value is not a quote: subtract current half-spread and slippage.
                bid = max(0, price - (contract["ask"] - contract["bid"]) / 2 - config.slippage)
                net = (bid - contract["ask"]) * 100 - 2 * config.fee
                outcomes[iv_label] = {"estimated_bid": round(bid, 4), "net_usd": round(net, 2),
                                      "return_on_premium": round(net / (contract["ask"] * 100), 4)}
            rows.append({"hold_days": horizon, "exit_date": day.isoformat(), "calendar_days": (day - start).days,
                         "stock_target": round(target, 4), "target_name": label, **outcomes,
                         "hit_probability": None, "expected_net_usd": None})
    return rows


def read_plan(symbol):
    snapshot = cache_get(f"call_plan:v{VERSION}:{symbol}")
    state = cache_get(f"call_plan_job:v{VERSION}:{symbol}") or {"status": "idle"}
    if state.get("status") in {"queued", "running"} and symbol not in _ACTIVE:
        state = {**state, "status": "interrupted", "reason": "服务重启中断计算，可手动重试"}
    return {"available": isinstance(snapshot, dict), "snapshot": snapshot,
            "current_request_blocked": state.get("status") == "blocked",
            "stale": bool(snapshot and snapshot.get("quote_date") != most_recent_session().isoformat()),
            "job": state}


def priority_pool(symbol, session):
    if symbol in MAGNIFICENT_SEVEN:
        return "七姐妹优先研究"
    from long_option_screen_service import read_snapshot
    board = (read_snapshot().get("snapshot") or {})
    volume = board.get("option_volume_research") or {}
    if board.get("data_as_of") == session.isoformat() and volume.get("available"):
        leaders = volume.get("leaders") or []
        for index, row in enumerate(leaders[:20], 1):
            if row.get("symbol") == symbol:
                return f"Cboe四所12月活跃股 #{index}；非全美排名"
    return "个股研究；尚无同日期权活跃排名核验"


def build_plan(symbol, config, max_cost, *, feed=None, now=None):
    now = now or datetime.now(timezone.utc)
    session = most_recent_session(now)
    start = forward_sessions(now.astimezone(EASTERN).date(), 1)[0]
    if now >= session_close_et(start).astimezone(timezone.utc):
        start = forward_sessions(start + timedelta(days=1), 1)[0]
    with external_data_scope(False):
        daily, source = get_daily_history(symbol, period="2y")
        benchmark, _ = get_daily_history("SPY", period="2y")
        earnings = get_next_earnings(symbol)
    base = {"symbol": symbol, "version": VERSION, "generated_at": now.isoformat(),
            "quote_date": session.isoformat(), "plan_start_date": start.isoformat(), "config": asdict(config),
            "candidates": [], "expected_net_usd": None, "hit_probability": None,
            "validation_status": "scenario_only_not_oos_validated", "estimated_cost_usd": 0,
            "source": "databento:opra_cbbo_1m_historical",
            "model_note": "价格×时间×IV情景，无概率权重，不是期望收益或最可能卖点；+50%/-25%只为退出报价参考"}
    if daily is None or daily.empty or not {"Open", "High", "Low", "Close", "Volume"}.issubset(daily.columns):
        return {**base, "status": "blocked", "reason": "暂无缓存日线，请先更新个股行情"}
    daily = daily[pd.to_datetime(daily.index).date <= session].sort_index()
    if daily.empty or pd.Timestamp(daily.index[-1]).date() != session:
        return {**base, "status": "blocked", "reason": "正股与期权参考日不一致，未请求期权数据"}
    if len(daily) < 220 or daily.tail(25).isna().any().any():
        return {**base, "status": "blocked", "reason": "趋势日线不足或缺字段，未请求期权数据"}
    if daily["Close"].tail(60).pct_change().abs().max() > 0.35:
        return {**base, "status": "blocked", "reason": "历史价格存在异常跳变，请先核验复权/公司行动"}
    spot = number(daily["Close"].iloc[-1])
    if not spot or spot <= 0:
        return {**base, "status": "blocked", "reason": "正股价格不可用"}
    benchmark = benchmark[pd.to_datetime(benchmark.index).date <= session] if benchmark is not None else None
    if benchmark is None or benchmark.empty or pd.Timestamp(benchmark.index[-1]).date() != session:
        return {**base, "status": "blocked", "reason": "SPY同日基准未齐备，未请求期权数据"}
    signal = consensus_feature_frame(daily, benchmark).iloc[-1]
    net = number(signal.get("net_consensus"))
    event = event_window(earnings, start, config)
    screen = cache_get(f"single_stock_page:v1:{symbol}:auto:2y") or {}
    context = screen.get("row") or {}
    extra_risks = []
    if context.get("open_risk_status") == "BLOCK_OPEN":
        extra_risks.append("原三层风控禁止开仓")
    base.update(spot=spot, stock_source=source, net_consensus=net, events=event, context_risks=extra_risks,
                priority_pool=priority_pool(symbol, session))
    if net is None or net <= 0.12 or event["status"] == "blocked" or extra_risks:
        return {**base, "status": "blocked", "reason": "当前偏多条件/事件风控未通过，未请求期权数据"}
    if feed is None:
        if not os.getenv("DATABENTO_API_KEY", "").strip():
            return {**base, "status": "unavailable", "reason": "Databento未配置；不以近月IV冒充60–90天合约"}
        import databento as db
        feed = CostCappedHistorical(db.Historical(), _CACHE_ROOT, max_cost)
    try:
        definitions = feed.fetch(schema="definition", symbols=[f"{symbol}.OPT"], stype="parent",
                                 start=session.isoformat() + "T00:00:00Z", end=(session + timedelta(days=1)).isoformat() + "T00:00:00Z")
        if "underlying" in definitions:
            definitions = definitions[definitions["underlying"].astype(str).str.upper() == symbol]
        selected = select_definitions(definitions, spot, session, start)
        selected = [r for r in selected if re.sub(r"\d{6}C\d{8}$", "", r["contract"]).strip() == symbol]
        if not selected:
            return {**base, "status": "partial", "reason": "未找到可核验的60–90天标准Call合约",
                    "estimated_cost_usd": feed.estimated_cost, "requests": feed.requests}
        qstart, qend = quote_window(session)
        quotes = feed.fetch(schema="cbbo-1m", symbols=[r["contract"] for r in selected], stype="raw_symbol", start=qstart, end=qend)
        candidates = []
        support = number(daily["Low"].tail(10).min())
        breakout = number(daily["High"].tail(10).max())
        for item in selected:
            row = evaluate_quote(item, quotes, spot, session, config)
            if row is None:
                continue
            row["risk_flags"] += event["risk_flags"]
            row["comparison_eligible"] &= event["status"] != "unknown"
            row["scenarios"] = exit_scenarios(row, spot, support, breakout, start, event, config)
            candidates.append(row)
        candidates.sort(key=lambda r: (not r["comparison_eligible"], r["spread_fraction"], abs(r["delta"] - 0.60), r["cash_usd"]))
        return {**base, "status": "completed" if any(r["comparison_eligible"] for r in candidates) else "partial",
                "reason": "按报价质量/Delta/成本比较，未按未经验证的EV排序" if candidates else "报价/IV无法核验，暂无合适合约",
                "candidates": candidates[:3], "candidate_count": len(candidates),
                "support_price": support, "breakout_price": breakout,
                "estimated_cost_usd": feed.estimated_cost, "requests": feed.requests,
                "quote_window_utc": f"{qstart}/{qend}"}
    except Exception as exc:
        # No raw provider exception: URLs and SDK errors can contain credentials.
        reason = "费用上限拦截；提高上限后手动重试，已成功缓存的数据会复用" if isinstance(exc, ValueError) and "cost_cap" in str(exc) else f"期权源暂不可用（{type(exc).__name__}），原快照保留"
        return {**base, "status": "unavailable", "reason": reason, "estimated_cost_usd": feed.estimated_cost, "requests": feed.requests}


def start_plan(symbol, slots, config=None, max_cost=0.0):
    config = config or PlanConfig()
    key = f"call_plan_job:v{VERSION}:{symbol}"
    with _LOCK:
        previous = cache_get(key) or {}
        if symbol in _ACTIVE:
            return {"started": False, "status": "running"}
        signature = {**asdict(config), "max_cost": max_cost}
        if previous.get("parameters") == signature and time.time() - float(previous.get("finished_timestamp") or 0) < 120:
            return {"started": False, **previous}
        if not slots.acquire(blocking=False):
            return {"started": False, "status": "busy"}
        _ACTIVE.add(symbol)
        try:
            cache_set(key, {"status": "queued", "parameters": signature})
        except Exception:
            _ACTIVE.discard(symbol)
            slots.release()
            raise
    def worker():
        try:
            cache_set(key, {"status": "running", "parameters": signature})
            result = build_plan(symbol, config, max_cost)
            # Provider/fee failures never overwrite a readable successful snapshot.
            existing = cache_get(f"call_plan:v{VERSION}:{symbol}")
            if result["status"] != "unavailable" and not (result["status"] == "blocked" and isinstance(existing, dict) and existing.get("candidates")):
                cache_set(f"call_plan:v{VERSION}:{symbol}", result)
            cache_set(key, {"status": result["status"], "reason": result.get("reason"),
                            "estimated_cost_usd": result["estimated_cost_usd"], "parameters": signature,
                            "next_estimated_cost_usd": next((r.get("next_estimate") for r in reversed(result.get("requests") or []) if r.get("status") == "cost_blocked"), None),
                            "finished_timestamp": time.time(), "finished_at": datetime.now(timezone.utc).isoformat()})
        except Exception as exc:
            cache_set(key, {"status": "failed", "reason": type(exc).__name__, "parameters": signature, "finished_timestamp": time.time()})
        finally:
            with _LOCK:
                _ACTIVE.discard(symbol)
            slots.release()
    try:
        threading.Thread(target=worker, name="call-plan", daemon=True).start()
    except Exception:
        with _LOCK:
            _ACTIVE.discard(symbol)
        slots.release()
        cache_set(key, {"status": "interrupted", "finished_timestamp": time.time()})
        raise
    return {"started": True, "status": "queued"}
