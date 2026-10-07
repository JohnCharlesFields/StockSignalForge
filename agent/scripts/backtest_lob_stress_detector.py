"""Backtest arXiv 2604.20949-style LOB stress early-warning detector.

This is NOT a return/alpha backtest.  It tests whether the paper's method can
act as a liquidity-stress warning module for this project.

Paper kernel implemented here:
  - limit-order-book feature stream
  - channels: HMM/GMM entropy, depth erosion, spread drift, order-flow momentum
  - MAX aggregation
  - rising-edge trigger
  - adaptive rolling percentile threshold
  - lead-time / precision / coverage evaluation

Practical adaptation:
  - Uses cached Databento XNAS.ITCH `mbp-1` + `trades` 14:00-16:00 ET windows.
  - `mbp-1` is top-of-book, not full LOB depth. Therefore "depth erosion" is
    touch-depth erosion, a conservative proxy for the paper's visible-depth erosion.
  - Stress labels are rule-based proxies: spread shock or touch-depth collapse
    within the same window. They are not human labels.

The script is research-only and never activates live ranking.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import random
import sys
from pathlib import Path
from statistics import mean, median
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
AGENT = HERE.parent
for _p in (AGENT, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from market_data_service import get_daily_history  # noqa: E402

DATASET = "XNAS.ITCH"
RUNS_DIR = AGENT / "runs"
CACHE_ROOT = AGENT / "data_cache" / "databento_microstructure"
NY = ZoneInfo("America/New_York")

DEFAULT_SYMS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD",
    "NFLX", "INTC", "MU", "QCOM", "ADBE", "TXN", "AMAT", "MRVL", "ASML",
    "ROKU", "MRNA", "MDB", "DDOG", "PANW", "SMCI", "ON", "ARM", "PYPL",
    "CSCO", "COST", "PEP",
]


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _local_window_utc(date_str: str, start_et: str, end_et: str) -> tuple[str, str]:
    d = dt.date.fromisoformat(date_str)
    sh, sm = [int(x) for x in start_et.split(":", 1)]
    eh, em = [int(x) for x in end_et.split(":", 1)]
    start_local = dt.datetime(d.year, d.month, d.day, sh, sm, tzinfo=NY)
    end_local = dt.datetime(d.year, d.month, d.day, eh, em, tzinfo=NY)
    return (
        start_local.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        end_local.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
    )


def _cache_path(schema: str, symbol: str, date_str: str, start: str, end: str) -> Path:
    import hashlib

    raw = f"{DATASET}|{schema}|{symbol}|{date_str}|{start}|{end}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return CACHE_ROOT / schema / f"{symbol}_{date_str}_{digest}.parquet"


def _as_local_index(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    idx = pd.DatetimeIndex(pd.to_datetime(out.index))
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    else:
        idx = idx.tz_convert("UTC")
    out.index = idx.tz_convert(NY)
    return out.sort_index()


def _num_col(df: pd.DataFrame, names: tuple[str, ...]) -> str | None:
    lowered = {str(c).lower(): c for c in df.columns}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _load_cached(schema: str, symbol: str, date_str: str, start_et: str, end_et: str) -> pd.DataFrame:
    st, en = _local_window_utc(date_str, start_et, end_et)
    path = _cache_path(schema, symbol, date_str, st, en)
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_parquet(path)
    except Exception:
        return pd.DataFrame()


def _osc(close: pd.Series) -> tuple[pd.Series, pd.Series]:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(2).mean()
    loss = (-delta.clip(upper=0)).rolling(2).mean()
    rsi2 = 100.0 - 100.0 / (1.0 + gain / loss.replace(0.0, np.nan))
    ma = close.rolling(20).mean()
    sd = close.rolling(20).std()
    z = (close - ma) / sd.replace(0.0, np.nan)
    return rsi2, z


def _collect_dos_events(symbols: list[str], *, years: float, period: str, max_events_per_symbol: int) -> list[dict[str, str]]:
    cutoff_date = pd.Timestamp.now(tz="UTC").tz_localize(None) - pd.Timedelta(days=int(years * 365))
    out: list[dict[str, str]] = []
    for sym in symbols:
        try:
            frame, _source = get_daily_history(sym, period=period)
        except Exception:
            continue
        if frame is None or frame.empty or "Close" not in frame or len(frame) < 80:
            continue
        close = pd.to_numeric(frame["Close"], errors="coerce").reset_index(drop=True)
        rsi2, z = _osc(close)
        dates = list(frame.index)
        picks: list[dict[str, str]] = []
        for i in range(25, len(close) - 1):
            raw_date = dates[i]
            di = raw_date.tz_localize(None) if getattr(raw_date, "tzinfo", None) else raw_date
            if di < cutoff_date:
                continue
            dos = (pd.notna(z.iloc[i]) and z.iloc[i] < -1.5) or (pd.notna(rsi2.iloc[i]) and rsi2.iloc[i] < 10)
            if dos:
                picks.append({"symbol": sym, "date": raw_date.strftime("%Y-%m-%d")})
        out.extend(picks[-max_events_per_symbol:] if max_events_per_symbol > 0 else picks)
    return out


def _build_feature_stream(mbp: pd.DataFrame, trades: pd.DataFrame, freq: str = "1s") -> pd.DataFrame:
    q = _as_local_index(mbp)
    if q.empty:
        return pd.DataFrame()
    bid_col = _num_col(q, ("bid_px_00", "bid_px", "bid"))
    ask_col = _num_col(q, ("ask_px_00", "ask_px", "ask"))
    bsz_col = _num_col(q, ("bid_sz_00", "bid_sz", "bid_size"))
    asz_col = _num_col(q, ("ask_sz_00", "ask_sz", "ask_size"))
    if not bid_col or not ask_col or not bsz_col or not asz_col:
        return pd.DataFrame()
    bid = pd.to_numeric(q[bid_col], errors="coerce")
    ask = pd.to_numeric(q[ask_col], errors="coerce")
    if ask.dropna().median() > 1e6:
        bid = bid / 1e9
        ask = ask / 1e9
    bsz = pd.to_numeric(q[bsz_col], errors="coerce").clip(lower=0.0)
    asz = pd.to_numeric(q[asz_col], errors="coerce").clip(lower=0.0)
    quote = pd.DataFrame({
        "mid": (bid + ask) / 2.0,
        "spread": (ask - bid) / ((bid + ask) / 2.0).replace(0.0, np.nan),
        "depth": bsz + asz,
        "touch_imb": (bsz - asz) / (bsz + asz).replace(0.0, np.nan),
    }, index=q.index).replace([np.inf, -np.inf], np.nan).dropna(subset=["mid", "spread", "depth"])
    quote = quote.resample(freq).last().ffill()

    tdf = _as_local_index(trades)
    if tdf.empty:
        quote["ofi"] = 0.0
        quote["trade_count"] = 0.0
        return quote.dropna()
    px_col = _num_col(tdf, ("price", "px"))
    sz_col = _num_col(tdf, ("size", "qty", "volume"))
    if not px_col or not sz_col:
        quote["ofi"] = 0.0
        quote["trade_count"] = 0.0
        return quote.dropna()
    px = pd.to_numeric(tdf[px_col], errors="coerce")
    if px.dropna().median() > 1e6:
        px = px / 1e9
    sz = pd.to_numeric(tdf[sz_col], errors="coerce").fillna(0.0).clip(lower=0.0)
    diff = px.diff()
    sign = np.sign(diff.to_numpy(dtype=float))
    filled = []
    last = 0.0
    for s in sign:
        if np.isnan(s) or s == 0:
            filled.append(last)
        else:
            last = float(s)
            filled.append(last)
    trades1 = pd.DataFrame({"signed_vol": np.array(filled) * sz.to_numpy(dtype=float), "vol": sz}, index=tdf.index)
    agg = trades1.resample(freq).sum()
    agg["ofi"] = agg["signed_vol"] / agg["vol"].replace(0.0, np.nan)
    quote = quote.join(agg[["ofi", "vol"]], how="left")
    quote["ofi"] = quote["ofi"].fillna(0.0)
    quote["trade_count"] = quote["vol"].fillna(0.0)
    return quote.dropna(subset=["mid", "spread", "depth"])


def _rolling_z(s: pd.Series, w: int, minp: int) -> pd.Series:
    m = s.rolling(w, min_periods=minp).mean()
    sd = s.rolling(w, min_periods=minp).std()
    return (s - m) / sd.replace(0.0, np.nan)


def _detector(stream: pd.DataFrame, *, lookback: int, threshold_pct: float, suppression: int) -> pd.DataFrame:
    from sklearn.mixture import GaussianMixture

    if len(stream) < max(lookback * 2, 300):
        return pd.DataFrame()
    f = stream.copy()
    ret = np.log(f["mid"] / f["mid"].shift(1)).replace([np.inf, -np.inf], np.nan)
    f["rv"] = ret.rolling(60, min_periods=20).std()
    f["depth_erosion_raw"] = (f["depth"].shift(lookback) - f["depth"]) / f["depth"].shift(lookback).replace(0.0, np.nan)
    f["spread_drift_raw"] = (f["spread"] - f["spread"].shift(lookback)) / f["spread"].shift(lookback).replace(0.0, np.nan)
    f["ofi_momentum_raw"] = f["ofi"].rolling(lookback, min_periods=max(5, lookback // 4)).mean().abs()

    train = f[["spread", "depth", "touch_imb", "rv"]].replace([np.inf, -np.inf], np.nan).dropna()
    entropy = pd.Series(np.nan, index=f.index)
    if len(train) >= 300:
        try:
            mu = train.iloc[: min(len(train), lookback * 2)].mean(axis=0)
            sd = train.iloc[: min(len(train), lookback * 2)].std(axis=0).replace(0.0, 1.0)
            X = ((train - mu) / sd).to_numpy(dtype=float)
            gm = GaussianMixture(n_components=2, covariance_type="full", random_state=0, n_init=2, reg_covar=1e-4)
            gm.fit(X[: min(len(X), lookback * 2)])
            prob = gm.predict_proba(X)
            ent = -(prob * np.log(np.clip(prob, 1e-9, 1.0))).sum(axis=1)
            entropy.loc[train.index] = ent
        except Exception:
            entropy = pd.Series(np.nan, index=f.index)
    f["entropy_raw"] = entropy

    minp = max(30, lookback // 2)
    channels = pd.DataFrame(index=f.index)
    channels["entropy"] = _rolling_z(f["entropy_raw"], lookback, minp).clip(lower=0.0)
    channels["depth_erosion"] = _rolling_z(f["depth_erosion_raw"], lookback, minp).clip(lower=0.0)
    channels["spread_drift"] = _rolling_z(f["spread_drift_raw"], lookback, minp).clip(lower=0.0)
    channels["ofi_momentum"] = _rolling_z(f["ofi_momentum_raw"], lookback, minp).clip(lower=0.0)
    f["max_score"] = channels.max(axis=1, skipna=True)
    f["first_channel"] = channels.fillna(-np.inf).idxmax(axis=1)
    f.loc[~np.isfinite(channels.fillna(-np.inf).max(axis=1)), "first_channel"] = None
    f["threshold"] = f["max_score"].rolling(lookback, min_periods=minp).quantile(threshold_pct)
    raw = (f["max_score"] >= f["threshold"]) & (f["max_score"] > f["max_score"].shift(1))
    trig = pd.Series(False, index=f.index)
    last = -10**9
    for i, flag in enumerate(raw.fillna(False).tolist()):
        if flag and i - last >= suppression:
            trig.iloc[i] = True
            last = i
    f["trigger"] = trig
    return f


def _stress_labels(
    det: pd.DataFrame,
    *,
    lookback: int,
    spread_z: float,
    depth_drop: float,
    min_duration: int,
    merge_gap: int,
) -> list[pd.Timestamp]:
    if det.empty:
        return []
    spz = _rolling_z(det["spread"], lookback, max(30, lookback // 2))
    depth_ref = det["depth"].rolling(lookback, min_periods=max(30, lookback // 2)).median()
    depth_collapse = det["depth"] / depth_ref.replace(0.0, np.nan) - 1.0
    raw = ((spz >= spread_z) | (depth_collapse <= -abs(depth_drop))).fillna(False)
    # Stress onset is an episode, not a per-second tick.  Require persistence
    # and merge nearby ticks into one event; otherwise noisy labels inflate
    # coverage/precision artificially.
    stress = (raw.rolling(min_duration, min_periods=min_duration).sum() >= min_duration).fillna(False)
    onsets: list[pd.Timestamp] = []
    last_onset: pd.Timestamp | None = None
    active = False
    for ts, flag in stress.items():
        if bool(flag) and not active:
            if last_onset is None or (ts - last_onset).total_seconds() >= merge_gap:
                onsets.append(ts)
                last_onset = ts
            active = True
        elif not bool(flag):
            active = False
    return onsets


def _evaluate(det: pd.DataFrame, labels: list[pd.Timestamp], *, match_window_sec: int) -> dict[str, Any]:
    triggers = list(det.index[det.get("trigger", pd.Series(False, index=det.index)).fillna(False)])
    matched_triggers: set[pd.Timestamp] = set()
    lead_times: list[float] = []
    label_matches = 0
    for label in labels:
        candidates = [t for t in triggers if t < label and (label - t).total_seconds() <= match_window_sec]
        if candidates:
            trig = max(candidates)
            matched_triggers.add(trig)
            lead_times.append((label - trig).total_seconds())
            label_matches += 1
    false_triggers = [t for t in triggers if t not in matched_triggers]
    return {
        "labels": len(labels),
        "triggers": len(triggers),
        "matched_labels": label_matches,
        "false_triggers": len(false_triggers),
        "precision": round(len(matched_triggers) / len(triggers), 4) if triggers else None,
        "coverage": round(label_matches / len(labels), 4) if labels else None,
        "mean_lead_sec": round(mean(lead_times), 2) if lead_times else None,
        "median_lead_sec": round(median(lead_times), 2) if lead_times else None,
        "lead_times": lead_times,
        "channel_counts": det.loc[triggers, "first_channel"].value_counts().to_dict() if triggers else {},
    }


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    labels = sum(int(r["metrics"]["labels"]) for r in rows)
    triggers = sum(int(r["metrics"]["triggers"]) for r in rows)
    matched = sum(int(r["metrics"]["matched_labels"]) for r in rows)
    false_trig = sum(int(r["metrics"]["false_triggers"]) for r in rows)
    leads = [float(x) for r in rows for x in (r["metrics"].get("lead_times") or [])]
    channels: dict[str, int] = {}
    for r in rows:
        for k, v in dict(r["metrics"].get("channel_counts") or {}).items():
            channels[k] = channels.get(k, 0) + int(v)
    return {
        "windows": len(rows),
        "labels": labels,
        "triggers": triggers,
        "matched_labels": matched,
        "false_triggers": false_trig,
        "precision": round((triggers - false_trig) / triggers, 4) if triggers else None,
        "coverage": round(matched / labels, 4) if labels else None,
        "mean_lead_sec": round(mean(leads), 2) if leads else None,
        "median_lead_sec": round(median(leads), 2) if leads else None,
        "channel_counts": channels,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="LOB stress detector backtest based on arXiv 2604.20949.")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMS))
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--period", default="3y")
    ap.add_argument("--max-events-per-symbol", type=int, default=20)
    ap.add_argument("--window-start-et", default="14:00")
    ap.add_argument("--window-end-et", default="16:00")
    ap.add_argument("--lookback-sec", type=int, default=300)
    ap.add_argument("--threshold-pct", type=float, default=0.85)
    ap.add_argument("--suppression-sec", type=int, default=60)
    ap.add_argument("--match-window-sec", type=int, default=300)
    ap.add_argument("--stress-spread-z", type=float, default=2.5)
    ap.add_argument("--stress-depth-drop", type=float, default=0.35)
    ap.add_argument("--stress-min-duration-sec", type=int, default=10)
    ap.add_argument("--stress-merge-gap-sec", type=int, default=300)
    ap.add_argument("--output-prefix", default="")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    events = _collect_dos_events(symbols, years=args.years, period=args.period, max_events_per_symbol=args.max_events_per_symbol)
    rows: list[dict[str, Any]] = []
    cache_miss = 0
    too_short = 0
    for idx, ev in enumerate(events, start=1):
        sym, dstr = ev["symbol"], ev["date"]
        mbp = _load_cached("mbp-1", sym, dstr, args.window_start_et, args.window_end_et)
        trades = _load_cached("trades", sym, dstr, args.window_start_et, args.window_end_et)
        if mbp.empty:
            cache_miss += 1
            continue
        stream = _build_feature_stream(mbp, trades)
        if len(stream) < max(args.lookback_sec * 2, 300):
            too_short += 1
            continue
        det = _detector(stream, lookback=args.lookback_sec, threshold_pct=args.threshold_pct, suppression=args.suppression_sec)
        if det.empty:
            too_short += 1
            continue
        labels = _stress_labels(
            det,
            lookback=args.lookback_sec,
            spread_z=args.stress_spread_z,
            depth_drop=args.stress_depth_drop,
            min_duration=args.stress_min_duration_sec,
            merge_gap=args.stress_merge_gap_sec,
        )
        metrics = _evaluate(det, labels, match_window_sec=args.match_window_sec)
        rows.append({
            "symbol": sym,
            "date": dstr,
            "metrics": metrics,
            "stream_rows": len(stream),
            "first_ts": str(stream.index[0]),
            "last_ts": str(stream.index[-1]),
        })
        if idx % 50 == 0:
            print(f"...events {idx}/{len(events)} usable={len(rows)} miss={cache_miss}", flush=True)

    aggregate = _aggregate(rows)
    payload = {
        "paper": "arXiv:2604.20949v1 Early Detection of Latent Microstructure Regimes in Limit Order Books",
        "method": "MAX(entropy, touch-depth erosion, spread drift, OFI momentum) + rising edge + adaptive threshold",
        "adaptation": "Databento XNAS.ITCH mbp-1/trades cached 14:00-16:00 ET windows; top-of-book depth proxy, not full LOB",
        "config": vars(args),
        "events_requested": len(events),
        "usable_windows": len(rows),
        "cache_miss": cache_miss,
        "too_short": too_short,
        "aggregate": aggregate,
        "rows": rows,
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    basename = args.output_prefix.strip() or f"lob_stress_detector_{dt.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    json_path = RUNS_DIR / f"{basename}.json"
    md_path = RUNS_DIR / f"{basename}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md = [
        "# LOB 流动性压力早期检测回测",
        "",
        f"- 论文：{payload['paper']}",
        f"- 方法：{payload['method']}",
        f"- 适配：{payload['adaptation']}",
        f"- 请求事件：{len(events)}；可用窗口：{len(rows)}；缓存缺失：{cache_miss}；窗口过短/不可用：{too_short}",
        "",
        "## 聚合结果",
        "",
        f"- stress labels: {aggregate['labels']}",
        f"- triggers: {aggregate['triggers']}",
        f"- matched labels: {aggregate['matched_labels']}",
        f"- false triggers: {aggregate['false_triggers']}",
        f"- precision: {aggregate['precision']}",
        f"- coverage: {aggregate['coverage']}",
        f"- mean lead sec: {aggregate['mean_lead_sec']}",
        f"- median lead sec: {aggregate['median_lead_sec']}",
        f"- channel counts: {aggregate['channel_counts']}",
        "",
        "## 判定",
        "",
    ]
    precision = aggregate.get("precision") or 0
    coverage = aggregate.get("coverage") or 0
    lead = aggregate.get("mean_lead_sec")
    if precision >= 0.6 and coverage >= 0.3 and lead and lead > 0:
        md.append("- 结论：具备候选价值，可继续扩展到更完整的 L2/全天数据做二次验证。")
    else:
        md.append("- 结论：当前缓存窗口/代理标签下未达到可上线标准，不建议接入线上模型。")
    md += [
        "- 注意：本回测验证的是风险预警能力，不是买入 alpha。",
        "- 注意：mbp-1 只是 top-of-book，无法完全代表论文的 full LOB depth erosion。",
    ]
    md_path.write_text("\n".join(md), encoding="utf-8")
    print(f"events={len(events)} usable={len(rows)} report_json={json_path} report_md={md_path}", flush=True)
    print(json.dumps(aggregate, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
