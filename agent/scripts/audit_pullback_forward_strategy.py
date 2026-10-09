"""Read-only ledger/cache audit. Exploratory comparisons, not new calibration.

No provider calls, DB updates, or active-model changes. All scenarios share the
same available rows; missing bars are excluded instead of moving exit dates.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, time, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app_database import DB_PATH
import cost_model
import market_data_service as market
from market_calendar import is_trading_day, most_recent_session, session_close_et

ET = ZoneInfo("America/New_York")


def summary(rows, value="net", block_length=1):
    valid = [r for r in rows if r.get(value) is not None and np.isfinite(r[value])]
    if not valid:
        return {"n": 0}
    days = sorted({r["day"] for r in valid})
    grouped = [[r[value] for r in valid if r["day"] == d] for d in days]
    sums = np.array([sum(g) for g in grouped])
    counts = np.array([len(g) for g in grouped])
    ci = None
    # Adjacent signal sessions have overlapping returns. Blocks preserve part
    # of this dependence; too few date blocks do not support inference.
    if len(days) >= max(4, 2 * block_length):
        rng = np.random.default_rng(20261008)
        starts = rng.integers(0, len(days), size=(1500, int(np.ceil(len(days) / block_length))))
        indices = ((starts[:, :, None] + np.arange(block_length)) % len(days)).reshape(1500, -1)[:, :len(days)]
        estimates = sums[indices].sum(axis=1) / counts[indices].sum(axis=1)
        ci = np.quantile(estimates, [.025, .975]).tolist()
    vals = np.array([r[value] for r in valid])
    return {"n": len(valid), "symbols": len({r['symbol'] for r in valid}), "signal_dates": len(days),
            "mean": float(vals.mean()), "median": float(np.median(vals)), "hit": float((vals > 0).mean()),
            "equal_day_mean": float(np.mean([np.mean(g) for g in grouped])),
            "ci95": ci, "block_length_dates": block_length,
            "inference": "insufficient_date_blocks" if ci is None else "exploratory_only"}


def next_open(recorded, signal_day):
    day = max(recorded.astimezone(ET).date(), signal_day)
    while not is_trading_day(day) or datetime.combine(day, time(9, 30), ET) <= recorded:
        day += timedelta(days=1)
    return day


def shift_session(day, n):
    for _ in range(n):
        day += timedelta(days=1)
        while not is_trading_day(day):
            day += timedelta(days=1)
    return day


def audit():
    conn = sqlite3.connect(DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    ledger = [dict(r) for r in conn.execute("SELECT * FROM predictions WHERE mode='live' ORDER BY as_of_date,symbol")]
    slices = {(r["as_of_date"], r["symbol"]): dict(r) for r in conn.execute("SELECT * FROM priority_candidate_slices")}
    conn.close()
    frames = {}
    for symbol in sorted({r['symbol'] for r in ledger} | {"SPY"}):
        frame = market._read_daily_cache(symbol)
        if not frame.empty:
            frame = frame[~frame.index.duplicated(keep="last")].sort_index()
            frames[symbol] = frame
    spy = frames.get("SPY", pd.DataFrame())
    raw, eligible, flags = [], [], Counter()
    features_missing = Counter()
    for row in ledger:
        if not row["resolved"]:
            continue
        day = row["as_of_date"]
        base = {"day": day, "symbol": row["symbol"], "rank": row["rank"], "prob": row["calibrated_prob"],
                "horizon": row["horizon_days"], "tag_market": bool(row["market_liquid_rs_top40"]),
                "tag_sector": bool(row["stock_stronger_than_industry"]),
                "legacy_excess": row["net_excess"], "legacy_alpha": row["beta_adjusted_alpha"]}
        base["net"] = row["forward_return"] - (row["excess_return"] - row["net_excess"])
        raw.append(base)
        try:
            recorded = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            if recorded.tzinfo is None:
                raise ValueError("unknown timezone")
            signal_day = datetime.fromisoformat(day).date()
            last_known = most_recent_session(recorded)
        except (ValueError, TypeError):
            flags["bad_timestamp"] += 1
            continue
        if not is_trading_day(signal_day):
            flags["signal_date_not_session"] += 1
        if recorded > session_close_et(signal_day):
            flags["recorded_after_assumed_entry_close"] += 1
        if signal_day > last_known:
            flags["signal_date_after_latest_completed_session"] += 1
        elif signal_day < last_known:
            flags["signal_date_older_than_latest_completed_session"] += 1
        frame = frames.get(row["symbol"])
        if frame is None or spy.empty:
            flags["missing_cached_frame"] += 1
            continue
        stamp = pd.Timestamp(signal_day)
        if stamp in frame.index:
            ref = row.get("entry_ref_price")
            if ref and abs(ref / float(frame.loc[stamp, "Close"]) - 1) > .01:
                flags["reference_price_differs_over_1pct"] += 1
        sl = slices.get((day, row["symbol"]), {})
        payload = json.loads(sl.get("payload_json") or "{}")
        raw_score = payload.get("pullback_hv_score")
        deep = (payload.get("deep_oversold") or {}).get("level")
        adv = (payload.get("liquidity") or {}).get("adv")
        for key, val in (("pullback_hv_score", raw_score), ("deep_oversold", deep), ("adv", adv)):
            if val is None:
                features_missing[key] += 1
        try:
            slice_created = datetime.fromisoformat(sl["created_at"].replace("Z", "+00:00"))
            if slice_created > recorded + timedelta(minutes=1):
                flags["slice_updated_after_first_record"] += 1
        except (KeyError, ValueError, TypeError):
            flags["slice_timestamp_unknown"] += 1
        # Reject non-session/stale signal dates; price availability must match
        # the information available when the forecast was recorded.
        if signal_day != last_known:
            continue
        entry_day = next_open(recorded, signal_day)
        entry_stamp = pd.Timestamp(entry_day)
        if entry_stamp not in frame.index or entry_stamp not in spy.index:
            flags["missing_next_open"] += 1
            continue
        entry = float(frame.loc[entry_stamp, "Open"])
        spy_entry = float(spy.loc[entry_stamp, "Open"])
        if not (np.isfinite(entry) and entry > 0 and spy_entry > 0):
            flags["invalid_next_open"] += 1
            continue
        hist = frame.loc[frame.index <= stamp].tail(253)
        returns = pd.concat([hist.Close.pct_change(), spy.Close.pct_change().loc[:stamp]], axis=1, sort=True).dropna().tail(60)
        beta = None
        if len(returns) >= 40 and returns.iloc[:, 1].var() > 0:
            beta = float(returns.iloc[:, 0].cov(returns.iloc[:, 1]) / returns.iloc[:, 1].var())
        for horizon in (1, 3, 5, 8, 10):
            exit_stamp = pd.Timestamp(shift_session(entry_day, horizon))
            sessions = pd.DatetimeIndex([pd.Timestamp(shift_session(entry_day, n)) for n in range(horizon + 1)])
            if not sessions.isin(frame.index).all() or not sessions.isin(spy.index).all():
                flags[f"h{horizon}_missing_calendar_bars"] += 1
                continue
            exit_price = float(frame.loc[exit_stamp, "Open"])
            spy_exit = float(spy.loc[exit_stamp, "Open"])
            if not (np.isfinite(exit_price) and exit_price > 0 and spy_exit > 0):
                continue
            gross = exit_price / entry - 1
            benchmark = spy_exit / spy_entry - 1
            # Same open-to-open baseline, using outcomes already known at signal.
            past = hist.Open.shift(-horizon) / hist.Open - 1
            expected_dates = [pd.Timestamp(shift_session(ts.date(), horizon)) for ts in hist.index]
            actual_dates = pd.Series(hist.index, index=hist.index).shift(-horizon)
            past = past[actual_dates == expected_dates].replace([np.inf, -np.inf], np.nan).dropna()
            baseline = float(past.mean()) if len(past) >= 60 else None
            cost = cost_model.equity_round_trip_cost(price=entry, avg_dollar_volume=adv)
            eligible.append({**base, "horizon": horizon, "entry_day": str(entry_day), "exit_day": str(exit_stamp.date()),
                             "net": gross - cost, "spy_excess": gross - benchmark - cost,
                             "alpha": gross - beta * benchmark - cost if beta is not None else None,
                             "own_excess": gross - baseline - cost if baseline is not None else None,
                             "score": raw_score, "deep": deep, "adv": adv, "cost": cost})
    scenarios = {}
    for h in (1, 3, 5, 8, 10):
        cohort = [r for r in eligible if r["horizon"] == h]
        groups = {"all_valid": cohort, "original_top20": [r for r in cohort if (r["rank"] or 9999) <= 20],
                  "untagged": [r for r in cohort if not r["tag_market"] and not r["tag_sector"]],
                  "market_tag": [r for r in cohort if r["tag_market"]],
                  "sector_tag": [r for r in cohort if r["tag_sector"]],
                  "prob_gt50": [r for r in cohort if (r["prob"] or 0) > .5],
                  "deep_oversold": [r for r in cohort if r["deep"] in {"deep", "extreme"}]}
        for name, key in (("probability_top20", "prob"), ("raw_score_top20", "score")):
            groups[name] = []
            for day in sorted({r["day"] for r in cohort}):
                candidates = [r for r in cohort if r["day"] == day and r[key] is not None]
                groups[name].extend(sorted(candidates, key=lambda r: (-r[key], r["symbol"]))[:20])
        scenarios[str(h)] = {name: {metric: summary(rows, metric, block_length=h)
                                   for metric in ("net", "spy_excess", "alpha", "own_excess")}
                             for name, rows in groups.items()}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "read_only": True,
            "primary_horizon": 5, "sensitivity_horizons": [1, 3, 8, 10],
            "comparison_status": "exploratory; inspected cohort, not independent OOS selection",
            "execution": "first regular session open strictly after recorded_at; exit open H exchange sessions later",
            "limitations": ["stored ranks/probabilities may have been overwritten; immutable model snapshots absent",
                            "date checks establish necessary timing consistency only, not a certified clean OOS cohort",
                            "comparisons use cached complete cases; missing outcomes can create selection bias",
                            "current cache may contain corporate action differences; no point-in-time historical vendor version",
                            "few signal dates and overlapping holdings; no independent validation claimed",
                            "scenario means are event returns, not an account equity curve",
                            "feature filters have different sample sizes; no causal treatment effect claimed"],
            "ledger": {"live_total": len(ledger), "resolved": len(raw), "horizons": dict(Counter(r['horizon'] for r in raw)),
                       "raw_net": summary(raw), "legacy_excess": summary(raw, "legacy_excess"),
                       "legacy_alpha": summary(raw, "legacy_alpha")},
            "audit_flags": dict(flags), "feature_missing": dict(features_missing), "scenarios": scenarios}


def markdown(report):
    lines = ["# 回调策略前向账本审计", "", f"生成时间：{report['generated_at']}", "",
             "本报告只读取现有数据库和行情缓存，不更新历史账本、排名或校准曲线。",
             "日期核验仅是必要条件，通过检查的记录仍可能缺少不可变模型版本或被更新过；不能称为已认证的干净前向样本。",
             "主观察窗口5个交易日；1/3/8/10日是敏感性分析。该批数据已经被查看，方案比较属于探索，不能当作独立样本外验证。",
             "", "## 数据审计", "", "```json", json.dumps({k: report[k] for k in ('ledger', 'audit_flags', 'feature_missing')}, ensure_ascii=False, indent=2), "```", "",
             "## 固定方案对照", "", "信号记录后下一可交易开盘入场，H个交易日后开盘退出。基线只使用信号时已知收益；成本沿用项目成本模型。",
             "分块区间按相邻信号日期重采样，日期块数不足时不报告CI。每笔收益均值不等于账户收益。", "",
             "|持有日|方案|样本|信号日|净收益均值|净盈利比例|相对SPY|Beta后Alpha|自身基线超额|", "|---|---|---|---|---|---|---|---|---|"]
    def pct(v):
        return f"{v:.2%}" if v is not None else "--"
    for h, groups in report['scenarios'].items():
        for name, values in groups.items():
            net = values['net']
            lines.append(f"|{h}|{name}|{net['n']}|{net.get('signal_dates', 0)}|{pct(net.get('mean'))}|{pct(net.get('hit'))}|{pct(values['spy_excess'].get('mean'))}|{pct(values['alpha'].get('mean'))}|{pct(values['own_excess'].get('mean'))}|")
    lines += ["", "## 优化顺序", "",
              "1. 先修验证口径：冻结首次预测和模型版本，记录实际行情日期，统一可执行入场时间、持有期和事前基线。旧账本保留但标记待审计。",
              "2. 将正超额、负超额和证据不足分开展示；概率超过50%不等于期望收益为正。剥离SPY beta也不等于已经消除行业/风格风险。",
              "3. 主验证窗口固定5日，1/3/8/10日只做敏感性分析。用滚动时间外验证，训练事件必须在验证起点前已经结算，按最大持有窗口设置隔离。",
              "4. 先做消融：主信号排序 vs 当前RS门控排序 vs 软标签只展示不加分；不要根据这批已看过的数据选最优权重或倒置做空。",
              "5. 后续分开检验均值回归与趋势回调两条策略，及路径触发入场/结构退出；账户层同时报告净收益、回撤、成本和资金占用。",
              "", "## 限制", ""] + [f"- {note}" for note in report['limitations']]
    return "\n".join(lines) + "\n"


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = audit()
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix('.json').write_text(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    out.with_suffix('.md').write_text(markdown(result), encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('ledger', 'audit_flags', 'feature_missing')}, ensure_ascii=False))
