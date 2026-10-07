"""Databento intraday microstructure orthogonality test for deep-oversold.

Tests whether NBBO/quote drift, signed trade pressure, or auction imbalance
add incremental edge over the existing deep-oversold / pullback_hv effect.

The user's practical entry window is Beijing 02:30-03:45, roughly US/Eastern
14:30-15:45 during daylight-saving months.  We therefore compute point-in-time
features at multiple ET decision cutoffs and test each inside the DOS subset.

This script is research-only. It never activates calibration curves.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import random
import sys
import time
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
AGENT = HERE.parent
for _p in (AGENT, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import cost_model  # noqa: E402
import exit_model  # noqa: E402
from market_data_service import get_daily_history  # noqa: E402

DATASET = "XNAS.ITCH"
DEFAULT_SCHEMAS = ("mbp-1", "trades", "imbalance")
DEFAULT_CUTOFFS = ("14:30", "15:00", "15:30", "15:45")
CACHE_ROOT = AGENT / "data_cache" / "databento_microstructure"
RUNS_DIR = AGENT / "runs"
NY = ZoneInfo("America/New_York")

DEFAULT_SYMS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD",
    "NFLX", "INTC", "MU", "QCOM", "ADBE", "TXN", "AMAT", "MRVL", "ASML",
    "ROKU", "MRNA", "MDB", "DDOG", "PANW", "SMCI", "ON", "ARM", "PYPL",
    "CSCO", "COST", "PEP",
]


def _osc(close: pd.Series) -> tuple[pd.Series, pd.Series]:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(2).mean()
    loss = (-delta.clip(upper=0)).rolling(2).mean()
    rsi2 = 100.0 - 100.0 / (1.0 + gain / loss.replace(0.0, np.nan))
    ma = close.rolling(20).mean()
    sd = close.rolling(20).std()
    z = (close - ma) / sd.replace(0.0, np.nan)
    return rsi2, z


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
    raw = f"{DATASET}|{schema}|{symbol}|{date_str}|{start}|{end}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return CACHE_ROOT / schema / f"{symbol}_{date_str}_{digest}.parquet"


def _load_schema_df(client, schema: str, symbol: str, date_str: str, start: str, end: str, *, cache_only: bool, retries: int, sleep_sec: float) -> tuple[pd.DataFrame, str]:
    path = _cache_path(schema, symbol, date_str, start, end)
    if path.exists():
        try:
            return pd.read_parquet(path), "cache"
        except Exception:
            pass
    if cache_only:
        return pd.DataFrame(), "cache_miss"
    last_err = ""
    for attempt in range(max(1, retries + 1)):
        try:
            store = client.timeseries.get_range(
                dataset=DATASET,
                schema=schema,
                symbols=[symbol],
                stype_in="raw_symbol",
                start=start,
                end=end,
            )
            df = store.to_df()
            if df is not None and not df.empty:
                path.parent.mkdir(parents=True, exist_ok=True)
                df.to_parquet(path)
            if sleep_sec > 0:
                time.sleep(sleep_sec)
            return df, "databento"
        except Exception as exc:
            last_err = str(exc)[:220]
            wait = max(0.25, sleep_sec) * (2 ** attempt)
            if "too many" in last_err.lower() or "429" in last_err or "rate" in last_err.lower():
                wait = max(wait, 3.0 * (attempt + 1))
            time.sleep(min(wait, 30.0))
    return pd.DataFrame(), f"failed:{last_err or 'unknown'}"


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


def _before_cutoff(df: pd.DataFrame, cutoff: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    h, m = [int(x) for x in cutoff.split(":", 1)]
    cutoff_time = dt.time(h, m)
    return df[[x.time() <= cutoff_time for x in df.index]]


def _num_col(df: pd.DataFrame, names: tuple[str, ...]) -> str | None:
    lowered = {str(c).lower(): c for c in df.columns}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _quote_features(df: pd.DataFrame, cutoff: str) -> dict[str, float | None]:
    frame = _before_cutoff(_as_local_index(df), cutoff)
    if frame.empty:
        return {"quote_available": 0}
    bid_col = _num_col(frame, ("bid_px_00", "bid_px", "bid"))
    ask_col = _num_col(frame, ("ask_px_00", "ask_px", "ask"))
    bsz_col = _num_col(frame, ("bid_sz_00", "bid_sz", "bid_size"))
    asz_col = _num_col(frame, ("ask_sz_00", "ask_sz", "ask_size"))
    if not bid_col or not ask_col:
        return {"quote_available": 0}
    bid = pd.to_numeric(frame[bid_col], errors="coerce")
    ask = pd.to_numeric(frame[ask_col], errors="coerce")
    if ask.dropna().median() > 1e6:
        bid = bid / 1e9
        ask = ask / 1e9
    mid = ((bid + ask) / 2.0).dropna()
    if len(mid) < 2:
        return {"quote_available": 0}
    spread = ((ask - bid) / mid.replace(0.0, np.nan)).replace([np.inf, -np.inf], np.nan).dropna()
    out: dict[str, float | None] = {
        "quote_available": 1,
        "mid_drift": float(mid.iloc[-1] / mid.iloc[0] - 1.0) if mid.iloc[0] > 0 else None,
        "mid_last": float(mid.iloc[-1]),
        "spread_last": float(spread.iloc[-1]) if len(spread) else None,
        "spread_change": float(spread.iloc[-1] - spread.iloc[0]) if len(spread) >= 2 else None,
    }
    if bsz_col and asz_col:
        bsz = pd.to_numeric(frame[bsz_col], errors="coerce").astype(float)
        asz = pd.to_numeric(frame[asz_col], errors="coerce").astype(float)
        den = (bsz + asz).replace(0.0, np.nan)
        imb = ((bsz - asz) / den).replace([np.inf, -np.inf], np.nan).dropna()
        out["quote_imb_last"] = float(imb.iloc[-1]) if len(imb) else None
        out["quote_imb_mean"] = float(imb.mean()) if len(imb) else None
    else:
        out["quote_imb_last"] = None
        out["quote_imb_mean"] = None
    return out


def _trade_features(df: pd.DataFrame, cutoff: str) -> dict[str, float | int | None]:
    frame = _before_cutoff(_as_local_index(df), cutoff)
    if frame.empty:
        return {"trades_available": 0}
    px_col = _num_col(frame, ("price", "px"))
    sz_col = _num_col(frame, ("size", "qty", "volume"))
    if not px_col or not sz_col:
        return {"trades_available": 0}
    px = pd.to_numeric(frame[px_col], errors="coerce").astype(float)
    if px.dropna().median() > 1e6:
        px = px / 1e9
    sz = pd.to_numeric(frame[sz_col], errors="coerce").fillna(0.0).clip(lower=0.0).astype(float)
    valid = px.notna() & (sz > 0)
    px = px[valid]
    sz = sz[valid]
    if len(px) < 3 or float(sz.sum()) <= 0:
        return {"trades_available": 0}
    diff = px.diff()
    signs = np.sign(diff.to_numpy(dtype=float))
    last = 0.0
    filled = []
    for s in signs:
        if np.isnan(s) or s == 0:
            filled.append(last)
        else:
            last = float(s)
            filled.append(last)
    signed = np.array(filled, dtype=float) * sz.to_numpy(dtype=float)
    signed_ratio = float(signed.sum() / (float(sz.sum()) + 1e-9))
    block_cut = float(sz.quantile(0.9))
    block_signed = signed[sz.to_numpy(dtype=float) >= block_cut]
    block_vol = sz[sz >= block_cut]
    return {
        "trades_available": 1,
        "trade_count": int(len(px)),
        "trade_volume": float(sz.sum()),
        "signed_trade_ratio": signed_ratio,
        "block_signed_ratio": float(block_signed.sum() / (float(block_vol.sum()) + 1e-9)) if len(block_vol) else None,
        "trade_price_drift": float(px.iloc[-1] / px.iloc[0] - 1.0) if px.iloc[0] > 0 else None,
    }


def _imbalance_features(df: pd.DataFrame, cutoff: str) -> dict[str, float | int | None]:
    frame = _before_cutoff(_as_local_index(df), cutoff)
    if frame.empty:
        return {"auction_available": 0}
    numeric = frame.select_dtypes(include=["number"])
    if numeric.empty:
        return {"auction_available": 0}
    cols = [c for c in numeric.columns if "imb" in str(c).lower()]
    qty_col = cols[0] if cols else numeric.columns[0]
    series = pd.to_numeric(numeric[qty_col], errors="coerce").dropna()
    if series.empty:
        return {"auction_available": 0}
    return {
        "auction_available": 1,
        "auction_metric_last": float(series.iloc[-1]),
        "auction_metric_mean": float(series.mean()),
        "auction_records": int(len(series)),
    }


def _cluster_ci(rows: list[dict], key: str = "net", n_boot: int = 3000, seed: int = 42) -> tuple[float | None, float | None]:
    by: dict[str, list[float]] = {}
    for row in rows:
        by.setdefault(row["ticker"], []).append(float(row[key]))
    clusters = [k for k in by if by[k]]
    if len(clusters) < 2:
        return None, None
    rng = random.Random(seed)
    estimates = []
    for _ in range(n_boot):
        sampled = [rng.choice(clusters) for _ in clusters]
        vals = [x for c in sampled for x in by[c]]
        if vals:
            estimates.append(sum(vals) / len(vals))
    estimates.sort()
    return round(estimates[int(0.025 * (len(estimates) - 1))], 6), round(estimates[int(0.975 * (len(estimates) - 1))], 6)


def _diff_ci(a: list[dict], b: list[dict], n_boot: int = 3000, seed: int = 7) -> tuple[float | None, float | None, float | None]:
    ba: dict[str, list[float]] = {}
    bb: dict[str, list[float]] = {}
    for row in a:
        ba.setdefault(row["ticker"], []).append(float(row["net"]))
    for row in b:
        bb.setdefault(row["ticker"], []).append(float(row["net"]))
    clusters = sorted(set(ba) | set(bb))
    if len(clusters) < 2:
        return None, None, None
    rng = random.Random(seed)
    estimates = []
    for _ in range(n_boot):
        sampled = [rng.choice(clusters) for _ in clusters]
        xa = [x for c in sampled for x in ba.get(c, [])]
        xb = [x for c in sampled for x in bb.get(c, [])]
        if xa and xb:
            estimates.append(sum(xa) / len(xa) - sum(xb) / len(xb))
    if not estimates:
        return None, None, None
    estimates.sort()
    return round(mean(estimates), 6), round(estimates[int(0.025 * (len(estimates) - 1))], 6), round(estimates[int(0.975 * (len(estimates) - 1))], 6)


def _collect_events(symbols: list[str], *, years: float, period: str, hold: int, max_events_per_symbol: int, event_selection: str) -> list[tuple[str, str, float]]:
    rt = cost_model.equity_round_trip_cost()
    cutoff_date = pd.Timestamp.now(tz="UTC").tz_localize(None) - pd.Timedelta(days=int(years * 365))
    events: list[tuple[str, str, float]] = []
    for sym in symbols:
        try:
            frame, _source = get_daily_history(sym, period=period)
        except Exception:
            continue
        if frame is None or frame.empty or "Close" not in frame or "Open" not in frame or len(frame) < 60 + hold:
            continue
        close = pd.to_numeric(frame["Close"], errors="coerce").reset_index(drop=True)
        open_ = pd.to_numeric(frame["Open"], errors="coerce").reset_index(drop=True)
        base = exit_model.baseline_forward_returns(close, open_, hold)
        bmean = mean(base) if base else 0.0
        rsi2, z = _osc(close)
        dates = list(frame.index)
        picks = []
        for i in range(25, len(close) - hold):
            di = dates[i].tz_localize(None) if getattr(dates[i], "tzinfo", None) else dates[i]
            if di < cutoff_date:
                continue
            dos = (pd.notna(z.iloc[i]) and z.iloc[i] < -1.5) or (pd.notna(rsi2.iloc[i]) and rsi2.iloc[i] < 10)
            if not dos:
                continue
            fwd = exit_model.forward_return(close, open_, i, hold)
            if fwd is None:
                continue
            picks.append((sym, dates[i].strftime("%Y-%m-%d"), fwd - bmean - rt))
        if event_selection == "oldest":
            selected = picks[:max_events_per_symbol]
        elif event_selection == "middle":
            start = max(0, (len(picks) - max_events_per_symbol) // 2)
            selected = picks[start:start + max_events_per_symbol]
        else:
            selected = picks[-max_events_per_symbol:]
        events.extend(selected)
    return events


def _split(rows: list[dict], key: str, hi_pred, lo_pred, hi_label: str, lo_label: str) -> dict:
    hi = [r for r in rows if r.get(key) is not None and hi_pred(float(r[key]))]
    lo = [r for r in rows if r.get(key) is not None and lo_pred(float(r[key]))]
    d, clo, chi = _diff_ci(hi, lo)
    return {
        "key": key,
        "hi_label": hi_label,
        "lo_label": lo_label,
        "hi_n": len(hi),
        "lo_n": len(lo),
        "hi_tickers": len({r["ticker"] for r in hi}),
        "lo_tickers": len({r["ticker"] for r in lo}),
        "hi_net_mean": mean([r["net"] for r in hi]) if hi else None,
        "lo_net_mean": mean([r["net"] for r in lo]) if lo else None,
        "diff_mean": d,
        "diff_ci_low": clo,
        "diff_ci_high": chi,
        "increment": bool(clo is not None and clo > 0),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Databento microstructure orthogonality test.")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMS))
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--period", default="3y")
    ap.add_argument("--hold", type=int, default=8)
    ap.add_argument("--max-events-per-symbol", type=int, default=5)
    ap.add_argument("--event-selection", choices=["recent", "oldest", "middle"], default="recent")
    ap.add_argument("--schemas", default=",".join(DEFAULT_SCHEMAS))
    ap.add_argument("--cutoffs", default=",".join(DEFAULT_CUTOFFS))
    ap.add_argument("--window-start-et", default="14:00")
    ap.add_argument("--window-end-et", default="16:00")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--cache-only", action="store_true")
    ap.add_argument("--cost-cap", type=float, default=20.0)
    ap.add_argument("--sleep-sec", type=float, default=0.5)
    ap.add_argument("--retries", type=int, default=4)
    ap.add_argument("--output-prefix", default="")
    args = ap.parse_args()

    import databento as db

    if not os.environ.get("DATABENTO_API_KEY", "").strip():
        print("no DATABENTO_API_KEY", flush=True)
        return
    client = db.Historical()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    schemas = [s.strip() for s in args.schemas.split(",") if s.strip()]
    cutoffs = [c.strip() for c in args.cutoffs.split(",") if c.strip()]
    events = _collect_events(
        symbols,
        years=args.years,
        period=args.period,
        hold=args.hold,
        max_events_per_symbol=args.max_events_per_symbol,
        event_selection=args.event_selection,
    )
    print(f"symbols={len(symbols)} DOS events={len(events)} cap={args.max_events_per_symbol}/symbol years={args.years} selection={args.event_selection}", flush=True)
    if not events:
        return

    per_schema_cost: dict[str, float | None] = {}
    s0, d0, _n0 = events[0]
    st0, en0 = _local_window_utc(d0, args.window_start_et, args.window_end_et)
    est = 0.0
    for schema in schemas:
        try:
            c = float(client.metadata.get_cost(dataset=DATASET, symbols=[s0], schema=schema, start=st0, end=en0, stype_in="raw_symbol"))
        except Exception as exc:
            print(f"cost {schema} ERR {str(exc)[:160]}", flush=True)
            c = None
        per_schema_cost[schema] = c
        if c is not None:
            est += c * len(events)
    print(f"cost estimate max=${est:.2f} schemas={per_schema_cost}", flush=True)
    if not args.execute:
        print("DRY-RUN. Re-run with --execute to pull.", flush=True)
        return
    if est > args.cost_cap and not args.cache_only:
        print(f"ABORT: estimate ${est:.2f} > cap ${args.cost_cap:.2f}", flush=True)
        return

    rows: list[dict] = []
    pulls = caches = fails = 0
    for idx, (sym, dstr, net) in enumerate(events, start=1):
        st, en = _local_window_utc(dstr, args.window_start_et, args.window_end_et)
        dfs: dict[str, pd.DataFrame] = {}
        for schema in schemas:
            df, source = _load_schema_df(client, schema, sym, dstr, st, en, cache_only=args.cache_only, retries=args.retries, sleep_sec=args.sleep_sec)
            if source == "cache":
                caches += 1
            elif source == "databento":
                pulls += 1
            else:
                fails += 1
            dfs[schema] = df
        for cutoff in cutoffs:
            feat: dict = {"ticker": sym, "date": dstr, "cutoff_et": cutoff, "net": net}
            if "mbp-1" in dfs:
                feat.update(_quote_features(dfs["mbp-1"], cutoff))
            if "trades" in dfs:
                feat.update(_trade_features(dfs["trades"], cutoff))
            if "imbalance" in dfs:
                feat.update(_imbalance_features(dfs["imbalance"], cutoff))
            if feat.get("quote_available") or feat.get("trades_available") or feat.get("auction_available"):
                rows.append(feat)
        if idx % 25 == 0:
            print(f"  ...events {idx}/{len(events)} rows={len(rows)} pulls={pulls} cache={caches} fail={fails}", flush=True)

    if not rows:
        print("no microstructure rows", flush=True)
        return
    report = {
        "dataset": DATASET,
        "schemas": schemas,
        "events": len(events),
        "event_selection": args.event_selection,
        "rows": rows,
        "pulls": pulls,
        "cache_hits": caches,
        "failures": fails,
        "estimated_cost_usd": round(est, 4),
        "cutoffs": {},
    }
    for cutoff in cutoffs:
        cr = [r for r in rows if r.get("cutoff_et") == cutoff]
        lo, hi = _cluster_ci(cr)
        splits = [
            _split(cr, "mid_drift", lambda v: v > 0.001, lambda v: v < -0.001, "NBBO mid up", "NBBO mid down"),
            _split(cr, "signed_trade_ratio", lambda v: v > 0.10, lambda v: v < -0.10, "signed buy pressure", "signed sell pressure"),
            _split(cr, "quote_imb_last", lambda v: v > 0.10, lambda v: v < -0.10, "bid-side quote imbalance", "ask-side quote imbalance"),
            _split(cr, "trade_price_drift", lambda v: v > 0.001, lambda v: v < -0.001, "trade price up", "trade price down"),
        ]
        combo_hi = [r for r in cr if (r.get("mid_drift") or 0) > 0 and (r.get("signed_trade_ratio") or 0) > 0 and (r.get("quote_imb_last") or 0) > 0]
        combo_lo = [r for r in cr if (r.get("mid_drift") or 0) < 0 and (r.get("signed_trade_ratio") or 0) < 0 and (r.get("quote_imb_last") or 0) < 0]
        d, clo, chi = _diff_ci(combo_hi, combo_lo)
        splits.append({
            "key": "constructive_combo",
            "hi_label": "mid+trade+quote constructive",
            "lo_label": "mid+trade+quote deteriorating",
            "hi_n": len(combo_hi),
            "lo_n": len(combo_lo),
            "hi_tickers": len({r["ticker"] for r in combo_hi}),
            "lo_tickers": len({r["ticker"] for r in combo_lo}),
            "hi_net_mean": mean([r["net"] for r in combo_hi]) if combo_hi else None,
            "lo_net_mean": mean([r["net"] for r in combo_lo]) if combo_lo else None,
            "diff_mean": d,
            "diff_ci_low": clo,
            "diff_ci_high": chi,
            "increment": bool(clo is not None and clo > 0),
        })
        report["cutoffs"][cutoff] = {
            "n": len(cr),
            "tickers": len({r["ticker"] for r in cr}),
            "net_mean": mean([r["net"] for r in cr]) if cr else None,
            "ci_low": lo,
            "ci_high": hi,
            "splits": splits,
        }
        print(f"\n=== cutoff {cutoff} ET n={len(cr)} tk={len({r['ticker'] for r in cr})} net={mean([r['net'] for r in cr])*100:+.3f}% CI=[{lo},{hi}] ===", flush=True)
        for s in splits:
            d = s["diff_mean"]
            print(
                f"  {s['key']}: hi_n={s['hi_n']} lo_n={s['lo_n']} diff={(d or 0)*100:+.3f}% "
                f"CI=[{s['diff_ci_low']},{s['diff_ci_high']}] {'INCREMENT' if s['increment'] else 'no'}",
                flush=True,
            )

    basename = args.output_prefix.strip() or f"databento_microstructure_{dt.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RUNS_DIR / f"{basename}.json"
    md_path = RUNS_DIR / f"{basename}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md = [
        "# Databento 微观结构正交性回测",
        "",
        f"- 数据集：{DATASET}",
        f"- schemas：{', '.join(schemas)}",
        f"- 事件：{len(events)}；特征行：{len(rows)}；新拉：{pulls}；缓存：{caches}；失败：{fails}",
        f"- 事件选择：{args.event_selection}",
        f"- 费用估算上限：${est:.2f}",
        f"- 持有口径：entry close[T] -> exit open[T+{args.hold}]，扣成本后相对个股自身基线净超额",
        "",
    ]
    for cutoff, data in report["cutoffs"].items():
        md += [
            f"## {cutoff} ET 决策截点",
            "",
            f"- n={data['n']}，tickers={data['tickers']}，全体净超额={(data['net_mean'] or 0)*100:+.3f}%，CI95=[{data['ci_low']}, {data['ci_high']}]",
            "",
            "| 特征 | 高组 | 低组 | 高n | 低n | 差值 | CI95 | 结论 |",
            "|---|---|---|---:|---:|---:|---|---|",
        ]
        for s in data["splits"]:
            d = s.get("diff_mean")
            md.append(
                f"| {s['key']} | {s['hi_label']} | {s['lo_label']} | {s['hi_n']} | {s['lo_n']} | "
                f"{(d or 0)*100:+.3f}% | [{s['diff_ci_low']}, {s['diff_ci_high']}] | "
                f"{'可作为增量候选' if s.get('increment') else '未通过正交增量'} |"
            )
        md.append("")
    md += [
        "## 判定纪律",
        "",
        "- 只有高低组差值的 cluster bootstrap CI 完全大于 0，才认为微观结构特征对 deep-oversold 有增量。",
        "- 本实验不改线上模型、不激活曲线。",
    ]
    md_path.write_text("\n".join(md), encoding="utf-8")
    print(f"\nreport_json={json_path}", flush=True)
    print(f"report_md={md_path}", flush=True)


if __name__ == "__main__":
    main()
