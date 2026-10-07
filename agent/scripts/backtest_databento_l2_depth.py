"""REAL L2 order-book test (Databento XNAS.ITCH mbp-10): do depth imbalance and
depth erosion at deep-oversold entries add edge over plain deep-oversold?

This finally tests, with actual order-book data, the two ideas that were rejected
only for "no data": (1) the user's order-book jargon -- price drifts toward the
heavier resting-order side (depth imbalance); (2) the arXiv-2604.20949 paper's
depth-erosion channel. Cost-controlled: for each deep-oversold event we pull only
a ~70-min window around the close (not the whole day), compute the closing book
imbalance + intraday depth-erosion slope, and test the orthogonal increment over
deep-oversold under our discipline (net excess vs own baseline, cluster CI,
entry close[T] -> exit open[T+H]).

Usage: dry-run prints event count + get_cost estimate and STOPS unless --execute
is passed and the estimate is within --cost-cap.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import random
import sys
import time
from pathlib import Path
from statistics import mean, median

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
for _p in (AGENT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import cost_model  # noqa: E402
import exit_model  # noqa: E402
from market_data_service import get_daily_history  # noqa: E402

DATASET = "XNAS.ITCH"
SCHEMA = "mbp-10"
CACHE_ROOT = Path(AGENT) / "data_cache" / "databento_l2_depth"
RUNS_DIR = Path(AGENT) / "runs"
# Nasdaq-listed, liquid, with enough volatility to actually go oversold.
DEFAULT_SYMS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD",
                "NFLX", "INTC", "MU", "QCOM", "ADBE", "TXN", "AMAT", "MRVL", "ASML",
                "ROKU", "MRNA", "MDB", "DDOG", "PANW", "SMCI", "ON", "ARM", "PYPL",
                "CSCO", "COST", "PEP"]


def _osc(close):
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(2).mean()
    loss = (-delta.clip(upper=0)).rolling(2).mean()
    rsi2 = 100.0 - 100.0 / (1.0 + gain / loss.replace(0.0, np.nan))
    ma = close.rolling(20).mean()
    sd = close.rolling(20).std()
    z = (close - ma) / sd.replace(0.0, np.nan)
    return rsi2, z


def _us_dst(d: datetime.date) -> bool:
    """US DST: 2nd Sunday March .. 1st Sunday November."""
    mar = datetime.date(d.year, 3, 1)
    start = mar + datetime.timedelta(days=(6 - mar.weekday()) % 7 + 7)
    nov = datetime.date(d.year, 11, 1)
    end = nov + datetime.timedelta(days=(6 - nov.weekday()) % 7)
    return start <= d < end


def _close_window_utc(date_str: str):
    """Short ~13-min window right at the US cash close (16:00 ET), in UTC, DST-aware.
    Tiny vs a full day -> fast download + cheap, while capturing the closing book
    (last record) + into-the-close depth erosion."""
    d = datetime.date.fromisoformat(date_str)
    close_h = 20 if _us_dst(d) else 21  # 16:00 ET = 20:00 UTC (EDT) / 21:00 UTC (EST)
    base = datetime.datetime(d.year, d.month, d.day, close_h, 0, 0)
    st = (base - datetime.timedelta(minutes=12)).strftime("%Y-%m-%dT%H:%M:%S")
    en = (base + datetime.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    return st, en


def _l2_features(df: pd.DataFrame):
    """From an mbp-10 window DataFrame near the close, compute closing-book
    imbalance + intraday depth-erosion slope. Returns dict or None."""
    if df is None or df.empty:
        return None
    bsz = [c for c in df.columns if c.startswith("bid_sz_")]
    asz = [c for c in df.columns if c.startswith("ask_sz_")]
    if not bsz or not asz or "bid_px_00" not in df or "ask_px_00" not in df:
        return None
    df = df.sort_index()  # ts_event index
    tot_bid = df[bsz].sum(axis=1).astype(float)
    tot_ask = df[asz].sum(axis=1).astype(float)
    tot = (tot_bid + tot_ask).replace(0.0, np.nan)
    last = df.iloc[-1]
    lb, la = float(tot_bid.iloc[-1]), float(tot_ask.iloc[-1])
    if lb + la <= 0:
        return None
    depth_imb = (lb - la) / (lb + la)
    b0, a0 = float(last["bid_sz_00"]), float(last["ask_sz_00"])
    touch_imb = (b0 - a0) / (b0 + a0) if (b0 + a0) > 0 else 0.0
    px_b, px_a = float(last["bid_px_00"]), float(last["ask_px_00"])
    # Databento prices are 1e-9 fixed point unless pretty; normalise defensively.
    if px_a > 1e6:
        px_b, px_a = px_b / 1e9, px_a / 1e9
    mid = (px_b + px_a) / 2.0
    spread = (px_a - px_b) / mid if mid > 0 else None
    # depth erosion: slope of total depth over the window, normalised by mean depth
    series = tot.dropna()
    erosion = None
    if len(series) >= 10:
        x = np.arange(len(series), dtype=float)
        y = series.to_numpy(dtype=float)
        slope = np.polyfit(x, y, 1)[0]
        erosion = float(slope * len(series) / (y.mean() + 1e-9))  # total drift / mean, signed
    return {"depth_imb": float(depth_imb), "touch_imb": float(touch_imb),
            "spread": spread, "erosion": erosion, "rows": int(len(df))}


def _cache_key(symbol: str, date_str: str, start: str, end: str) -> Path:
    raw = f"{DATASET}|{SCHEMA}|{symbol}|{date_str}|{start}|{end}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return CACHE_ROOT / "features" / f"{symbol}_{date_str}_{digest}.json"


def _read_feature_cache(symbol: str, date_str: str, start: str, end: str):
    path = _cache_key(symbol, date_str, start, end)
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return None


def _write_feature_cache(symbol: str, date_str: str, start: str, end: str, feature: dict):
    path = _cache_key(symbol, date_str, start, end)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"dataset": DATASET, "schema": SCHEMA, "symbol": symbol, "date": date_str,
                   "start": start, "end": end, "feature": feature}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _fetch_l2_features(client, symbol: str, date_str: str, start: str, end: str, *, retries: int, sleep_sec: float):
    cached = _read_feature_cache(symbol, date_str, start, end)
    if cached and isinstance(cached.get("feature"), dict):
        return cached["feature"], "cache"
    last_err = None
    for attempt in range(max(1, retries + 1)):
        try:
            store = client.timeseries.get_range(dataset=DATASET, symbols=[symbol], schema=SCHEMA,
                                                 start=start, end=end, stype_in="raw_symbol")
            feature = _l2_features(store.to_df())
            if feature:
                _write_feature_cache(symbol, date_str, start, end, feature)
            if sleep_sec > 0:
                time.sleep(sleep_sec)
            return feature, "databento"
        except Exception as exc:
            last_err = str(exc)[:240]
            wait = max(sleep_sec, 0.25) * (2 ** attempt)
            if "too many" in last_err.lower() or "429" in last_err or "rate" in last_err.lower():
                wait = max(wait, 3.0 * (attempt + 1))
            time.sleep(min(wait, 30.0))
    return None, f"failed:{last_err or 'unknown'}"


def _cluster_ci(rows, key="net", n_boot=3000, seed=42):
    by = {}
    for e in rows:
        by.setdefault(e["ticker"], []).append(float(e[key]))
    cl = [k for k in by if by[k]]
    if len(cl) < 2:
        return (None, None)
    rng = random.Random(seed)
    ests = []
    for _ in range(n_boot):
        s = [rng.choice(cl) for _ in cl]
        v = [x for c in s for x in by[c]]
        if v:
            ests.append(sum(v) / len(v))
    ests.sort()
    return (round(ests[int(0.025 * (len(ests) - 1))], 6), round(ests[int(0.975 * (len(ests) - 1))], 6))


def _diff_ci(a, b, n_boot=3000, seed=7):
    ba, bb = {}, {}
    for e in a:
        ba.setdefault(e["ticker"], []).append(float(e["net"]))
    for e in b:
        bb.setdefault(e["ticker"], []).append(float(e["net"]))
    cl = sorted(set(ba) | set(bb))
    if len(cl) < 2:
        return (None, None, None)
    rng = random.Random(seed)
    ests = []
    for _ in range(n_boot):
        s = [rng.choice(cl) for _ in cl]
        xa = [x for c in s for x in ba.get(c, [])]
        xb = [x for c in s for x in bb.get(c, [])]
        if xa and xb:
            ests.append(sum(xa) / len(xa) - sum(xb) / len(xb))
    if not ests:
        return (None, None, None)
    ests.sort()
    return (round(mean(ests), 6), round(ests[int(0.025 * (len(ests) - 1))], 6), round(ests[int(0.975 * (len(ests) - 1))], 6))


def _summ(label, rows):
    if not rows:
        print(f"  {label:34} n=0", flush=True)
        return
    nets = [e["net"] for e in rows]
    lo, hi = _cluster_ci(rows)
    sig = "SIG(>0)" if (lo is not None and lo > 0) else "  -   "
    ci = f"[{lo}, {hi}]" if lo is not None else "[n/a]"
    print(f"  {label:34} n={len(rows):<4} tk={len({e['ticker'] for e in rows}):<3} net={mean(nets)*100:+.3f}%  CI95={ci}  hit={sum(1 for v in nets if v>0)/len(nets):.0%}  {sig}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMS))
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--hold", type=int, default=8)
    ap.add_argument("--period", default="3y")
    ap.add_argument("--max-events-per-symbol", type=int, default=15)
    ap.add_argument("--cost-cap", type=float, default=40.0)
    ap.add_argument("--sleep-sec", type=float, default=0.4, help="Throttle between Databento calls.")
    ap.add_argument("--retries", type=int, default=4, help="Retries per event window.")
    ap.add_argument("--output-prefix", default="", help="Optional basename under agent/runs.")
    ap.add_argument("--cache-only", action="store_true", help="Use cached L2 feature JSON only; never call Databento.")
    ap.add_argument("--execute", action="store_true", help="actually pull L2 (costs money)")
    args = ap.parse_args()

    import databento as db
    key = os.environ.get("DATABENTO_API_KEY", "").strip()
    if not key:
        print("no DATABENTO_API_KEY", flush=True)
        return
    client = db.Historical(key)
    rt = cost_model.equity_round_trip_cost()
    H = args.hold
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    cutoff_date = (pd.Timestamp.utcnow().tz_localize(None) - pd.Timedelta(days=int(args.years * 365)))

    # ---- Stage A: select deep-oversold events (free) ----
    events = []  # (sym, date_str, entry_idx, net)
    daily = {}
    for sym in syms:
        try:
            frame, _s = get_daily_history(sym, period=args.period)
        except Exception:
            continue
        if frame is None or frame.empty or "Close" not in frame or "Open" not in frame or len(frame) < 60 + H:
            continue
        close = pd.to_numeric(frame["Close"], errors="coerce").reset_index(drop=True)
        open_ = pd.to_numeric(frame["Open"], errors="coerce").reset_index(drop=True)
        base = exit_model.baseline_forward_returns(close, open_, H)
        bmean = mean(base) if base else 0.0
        rsi2, z = _osc(close)
        dates = [ts for ts in frame.index]
        daily[sym] = (close, open_, bmean)
        picks = []
        for i in range(25, len(close) - H):
            di = dates[i].tz_localize(None) if getattr(dates[i], "tzinfo", None) else dates[i]
            if di < cutoff_date:
                continue
            dos = (pd.notna(z.iloc[i]) and z.iloc[i] < -1.5) or (pd.notna(rsi2.iloc[i]) and rsi2.iloc[i] < 10)
            if not dos:
                continue
            r = exit_model.forward_return(close, open_, i, H)
            if r is None:
                continue
            picks.append((sym, dates[i].strftime("%Y-%m-%d"), i, r - bmean - rt))
        picks = picks[-args.max_events_per_symbol:]  # most recent N
        events.extend(picks)

    print(f"symbols={len(daily)} deep-oversold events={len(events)} (cap {args.max_events_per_symbol}/sym, last {args.years}y)", flush=True)
    if not events:
        print("no events", flush=True)
        return

    # ---- Stage B: cost estimate (free) ----
    s0, d0, _i0, _n0 = events[0]
    st, en = _close_window_utc(d0)
    per = None
    try:
        per = float(client.metadata.get_cost(dataset=DATASET, symbols=[s0], schema=SCHEMA,
                                              start=st, end=en, stype_in="raw_symbol"))
    except Exception as e:
        print("get_cost ERR", str(e)[:160], flush=True)
    est = (per or 0.0) * len(events)
    print(f"cost estimate: ${est:.2f}  (~${per:.4f}/event-window x {len(events)} events)" if per is not None else "cost estimate: unknown", flush=True)
    if not args.execute:
        print("DRY-RUN. Re-run with --execute to pull (within cost cap).", flush=True)
        return
    if per is not None and est > args.cost_cap and not args.cache_only:
        print(f"ABORT: estimate ${est:.2f} exceeds cap ${args.cost_cap:.2f}. Lower --max-events-per-symbol or raise --cost-cap.", flush=True)
        return

    # ---- Stage C: pull L2 per event (COSTS MONEY) ----
    feat_rows = []
    pulled = failed = cached_hits = 0
    for k, (sym, dstr, i, net) in enumerate(events):
        st, en = _close_window_utc(dstr)
        if args.cache_only:
            cached = _read_feature_cache(sym, dstr, st, en)
            f = cached.get("feature") if isinstance(cached, dict) else None
            source = "cache" if f else "cache_miss"
        else:
            f, source = _fetch_l2_features(client, sym, dstr, st, en, retries=args.retries, sleep_sec=args.sleep_sec)
        if not f:
            failed += 1
            if (k + 1) % 25 == 0:
                print(f"  ...processed {k+1}/{len(events)} pulled={pulled} cache={cached_hits} failed={failed} last={source}", flush=True)
            continue
        if source == "cache":
            cached_hits += 1
        else:
            pulled += 1
        feat_rows.append({"ticker": sym, "date": dstr, "net": net, "l2_source": source, **f})
        if (k + 1) % 50 == 0:
            print(f"  ...processed {k+1}/{len(events)} pulled={pulled} cache={cached_hits} failed={failed}", flush=True)
    print(f"\nL2 features for {len(feat_rows)} events: pulled={pulled}, cache={cached_hits}, failed={failed}. actual billed ~ check Databento dashboard (est max ${est:.2f}).", flush=True)
    if len(feat_rows) < 30:
        print("too few L2 events for a CI; stopping.", flush=True)
        return

    # ---- Stage D: orthogonality within deep-oversold (free) ----
    report = {
        "dataset": DATASET,
        "schema": SCHEMA,
        "events_requested": len(events),
        "events_with_l2": len(feat_rows),
        "pulled": pulled,
        "cache_hits": cached_hits,
        "failed": failed,
        "estimated_cost_usd": round(est, 4) if per is not None else None,
        "hold_days": H,
        "rows": feat_rows,
        "splits": {},
    }

    print("\n=== DOS events with L2 (net-of-cost excess vs own baseline; entry close[T] -> exit open[T+%d]) ===" % H, flush=True)
    _summ("ALL DOS (with L2 data)", feat_rows)

    def split(key, hi_pred, lo_pred, hi_lbl, lo_lbl):
        hi = [e for e in feat_rows if e.get(key) is not None and hi_pred(e[key])]
        lo = [e for e in feat_rows if e.get(key) is not None and lo_pred(e[key])]
        _summ(hi_lbl, hi)
        _summ(lo_lbl, lo)
        d, clo, chi = _diff_ci(hi, lo)
        report["splits"][key] = {
            "hi_label": hi_lbl.strip(),
            "lo_label": lo_lbl.strip(),
            "hi_n": len(hi),
            "lo_n": len(lo),
            "hi_net_mean": mean([e["net"] for e in hi]) if hi else None,
            "lo_net_mean": mean([e["net"] for e in lo]) if lo else None,
            "diff_mean": d,
            "diff_ci_low": clo,
            "diff_ci_high": chi,
            "increment": bool(clo is not None and clo > 0),
        }
        if d is not None:
            verdict = "INCREMENT (CI>0)" if clo > 0 else ("INCREMENT<0 (CI<0)" if chi < 0 else "no increment (CI incl 0)")
            print(f"    diff [{hi_lbl.strip()}]-[{lo_lbl.strip()}] = {d*100:+.3f}%  CI95=[{clo}, {chi}]  -> {verdict}", flush=True)
        return d, clo, chi

    print("\n-- (1) DEPTH IMBALANCE (jargon: heavier bids -> price up?) --", flush=True)
    split("depth_imb", lambda v: v > 0.1, lambda v: v < -0.1, "  bid-heavy (buy press)", "  ask-heavy (sell press)")
    print("\n-- (2) TOP-OF-BOOK IMBALANCE --", flush=True)
    split("touch_imb", lambda v: v > 0.1, lambda v: v < -0.1, "  touch bid-heavy", "  touch ask-heavy")
    print("\n-- (3) DEPTH EROSION (paper: depth depleting into close) --", flush=True)
    split("erosion", lambda v: v < -0.15, lambda v: v > -0.05, "  eroding", "  stable/building")

    print("\n=== VERDICT ===", flush=True)
    print("  See per-feature diff CIs above. An L2 feature 'adds over deep-oversold' only if its", flush=True)
    print("  hi-vs-lo difference CI excludes 0. Caveats: in-sample; ~%d events; XNAS.ITCH single-venue;" % len(feat_rows), flush=True)
    print("  close-window snapshot proxy; daily-horizon outcome on an intraday feature.", flush=True)

    basename = args.output_prefix.strip() or f"databento_l2_depth_{datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RUNS_DIR / f"{basename}.json"
    md_path = RUNS_DIR / f"{basename}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md = [
        "# Databento L2 深度正交性回测",
        "",
        f"- 数据集：{DATASET} / {SCHEMA}",
        f"- 事件：请求 {len(events)}，有效 L2 {len(feat_rows)}，新拉 {pulled}，缓存 {cached_hits}，失败 {failed}",
        f"- 费用估算上限：${est:.2f}" if per is not None else "- 费用估算：unknown",
        f"- 持有口径：entry close[T] -> exit open[T+{H}]，扣成本后相对个股自身基线的净超额",
        "",
        "## 分组结果",
        "",
        "| 特征 | 高组 | 低组 | 高组n | 低组n | 差值 | CI95 | 结论 |",
        "|---|---|---:|---:|---:|---:|---|---|",
    ]
    for key, val in report["splits"].items():
        d = val.get("diff_mean")
        lo = val.get("diff_ci_low")
        hi = val.get("diff_ci_high")
        md.append(
            f"| {key} | {val['hi_label']} | {val['lo_label']} | {val['hi_n']} | {val['lo_n']} | "
            f"{(d or 0)*100:+.3f}% | [{lo}, {hi}] | {'可作为增量候选' if val.get('increment') else '未通过正交增量'} |"
        )
    md += [
        "",
        "## 判定纪律",
        "",
        "- 只有高低组差值的聚类 bootstrap CI 完全大于 0，才认为 L2 特征对 deep-oversold 有增量。",
        "- 单场所 XNAS.ITCH、收盘窗口代理、日线持有周期，均属于限制条件。",
    ]
    md_path.write_text("\n".join(md), encoding="utf-8")
    print(f"\nreport_json={json_path}", flush=True)
    print(f"report_md={md_path}", flush=True)


if __name__ == "__main__":
    main()
