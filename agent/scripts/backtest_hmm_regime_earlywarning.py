"""Extract the paper "Early Detection of Latent Microstructure Regimes" (arXiv
2604.20949) META-METHOD onto DAILY data and test it under our discipline.

The paper's specific LOB detector is unusable for us (3/4 channels need L2 depth /
NBBO / order flow we cannot get; seconds-scale; target = liquidity stress, not
return). The TRANSFERABLE kernel is: a lead-time-oriented regime-change detector =
regime-uncertainty channel (their HMM entropy) + drift channels, MAX-aggregated,
fired by a rising-edge trigger with an adaptive percentile threshold + suppression.

We rebuild that on daily OHLCV-derived features (no new data source):
  - regime model: 2-component Gaussian mixture on [rv5, range, vol_z] -> the
    "stress" component (higher short realized vol); entropy of its responsibility
    = regime-ambiguity channel (analog of HMM entropy).
  - drift channels: HV-rise (vol expanding), volume drift, stress-prob drift.
  - St = MAX(channels, each z-scored on a trailing window).
  - rising-edge trigger: St >= trailing 85th pct AND St rising AND >=5d since last.

Then we ask, under net-of-cost excess vs own baseline + cluster-bootstrap CI
(entry close[T] -> exit open[T+H], the user's mechanics):
  (a) ENTRY: is the trigger itself a long-entry edge?  (likely NO -- it warns of
      stress, which is not a buy)
  (b) RISK FILTER: within the validated pullback_hv top bucket, does sitting out
      days with an active stress-warning IMPROVE the edge?  (the promising angle)
  + lead-time descriptive: do triggers precede pullback_hv setups?

Honesty: GMM fit per symbol uses the full series (mild lookahead in the centroids;
forward returns are still genuinely forward). In-sample, single period, archive
universe (survivorship). A pass here is a CANDIDATE, not a ship.
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import warnings
from statistics import mean, median

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
for _p in (AGENT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import cost_model  # noqa: E402
import exit_model  # noqa: E402
from market_data_service import get_daily_history  # noqa: E402
from research_signal_framework_backtest import _collect_symbol_events  # noqa: E402


def _universe(name, n):
    from screening_framework_v2_optimized import resolve_universe
    try:
        syms, _s, _m = resolve_universe(name, archive_only=True)
    except Exception:
        syms = []
    return [str(s).upper() for s in syms][:n]


def _zroll(s: pd.Series, w: int = 60) -> pd.Series:
    m = s.rolling(w, min_periods=max(20, w // 2)).mean()
    sd = s.rolling(w, min_periods=max(20, w // 2)).std()
    return (s - m) / sd.replace(0.0, np.nan)


def _osc(close: pd.Series):
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(2).mean()
    loss = (-delta.clip(upper=0)).rolling(2).mean()
    rsi2 = 100.0 - 100.0 / (1.0 + gain / loss.replace(0.0, np.nan))
    ma = close.rolling(20).mean()
    sd = close.rolling(20).std()
    z = (close - ma) / sd.replace(0.0, np.nan)
    return rsi2, z


def _detector(frame: pd.DataFrame, walk_forward: bool = True, refit: int = 21, min_train: int = 90):
    """Return (St, trigger_bool) Series indexed 0..n-1 for one symbol.

    walk_forward=True: the Gaussian mixture is refit every `refit` days on PAST
    data only (standardised by past stats), so p_stress[t] is strictly causal --
    no lookahead. walk_forward=False = the original full-series fit (lookahead).
    """
    from sklearn.mixture import GaussianMixture
    close = pd.to_numeric(frame["Close"], errors="coerce").reset_index(drop=True)
    high = pd.to_numeric(frame["High"], errors="coerce").reset_index(drop=True)
    low = pd.to_numeric(frame["Low"], errors="coerce").reset_index(drop=True)
    vol = pd.to_numeric(frame.get("Volume"), errors="coerce").reset_index(drop=True)
    ret = np.log(close / close.shift(1))
    rv5 = ret.rolling(5).std()
    rng = (high - low) / close.replace(0.0, np.nan)
    volz = (vol - vol.rolling(20).mean()) / vol.rolling(20).std().replace(0.0, np.nan)
    feat = pd.DataFrame({"rv5": rv5, "rng": rng, "volz": volz})
    valid = feat.notna().all(axis=1).to_numpy()
    Xall = feat.to_numpy()
    nrow = len(feat)
    p_stress = pd.Series(np.nan, index=feat.index)

    def _fit(Xtr):
        mu = Xtr.mean(axis=0); sd = Xtr.std(axis=0) + 1e-9
        gm = GaussianMixture(n_components=2, covariance_type="full", random_state=0, n_init=2, reg_covar=1e-4)
        gm.fit((Xtr - mu) / sd)
        return gm, mu, sd, int(np.argmax(gm.means_[:, 0]))  # higher rv5 mean = stress

    if walk_forward:
        model = mu = sd = None; sc = 0
        for i in range(nrow):
            if not (valid[i] and i >= min_train):
                continue
            if model is None or (i % refit == 0):
                tr = Xall[:i][valid[:i]]
                if len(tr) >= 60:
                    try:
                        model, mu, sd, sc = _fit(tr)
                    except Exception:
                        model = None
            if model is not None and mu is not None:
                try:
                    p_stress.iloc[i] = float(model.predict_proba(((Xall[i] - mu) / sd).reshape(1, -1))[0, sc])
                except Exception:
                    pass
    elif valid.sum() >= 60:
        try:
            gm, mu, sd, sc = _fit(Xall[valid])
            p_stress.loc[valid] = gm.predict_proba((Xall[valid] - mu) / sd)[:, sc]
        except Exception:
            pass
    ent = -(p_stress * np.log(p_stress.clip(1e-6, 1)) + (1 - p_stress) * np.log((1 - p_stress).clip(1e-6, 1)))
    hvr = (rv5 / rv5.shift(10) - 1.0).clip(lower=0.0)
    pdrift = (p_stress - p_stress.shift(5)).clip(lower=0.0)
    ch = pd.DataFrame({
        "ent": _zroll(ent), "hv": _zroll(hvr),
        "vol": _zroll(volz.clip(lower=0.0)), "pdrift": _zroll(pdrift),
    })
    St = ch.max(axis=1)
    thr = St.rolling(60, min_periods=30).quantile(0.85)
    rising = St > St.shift(1)
    raw_trig = (St >= thr) & rising
    # suppression: >=5 days since last trigger
    trig = pd.Series(False, index=St.index)
    last = -99
    for i in range(len(St)):
        if bool(raw_trig.iloc[i]) and (i - last) >= 5 and pd.notna(St.iloc[i]):
            trig.iloc[i] = True
            last = i
    return St, trig


def _cluster_ci(rows, n_boot=2500, seed=42):
    by = {}
    for e in rows:
        by.setdefault(e["ticker"], []).append(float(e["net"]))
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


def _diff_ci(a, b, n_boot=2500, seed=7):
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
        print(f"  {label:36} n=0", flush=True)
        return
    nets = [e["net"] for e in rows]
    lo, hi = _cluster_ci(rows)
    sig = "SIG(>0)" if (lo is not None and lo > 0) else "  -   "
    ci = f"[{lo}, {hi}]" if lo is not None else "[n/a]"
    print(f"  {label:36} n={len(rows):<5} tk={len({e['ticker'] for e in rows}):<3} net={mean(nets)*100:+.3f}%  CI95={ci}  hit={sum(1 for v in nets if v>0)/len(nets):.0%}  {sig}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="spx")
    ap.add_argument("--max-symbols", type=int, default=150)
    ap.add_argument("--hold", type=int, default=8)
    ap.add_argument("--period", default="2y")
    ap.add_argument("--top-quantile", type=float, default=0.70)
    ap.add_argument("--warn-window", type=int, default=5)
    ap.add_argument("--full-fit", action="store_true", help="use lookahead full-series GMM (default = walk-forward)")
    args = ap.parse_args()
    print(f"GMM mode: {'FULL-FIT (lookahead)' if args.full_fit else 'WALK-FORWARD (causal)'}", flush=True)

    rt = cost_model.equity_round_trip_cost()
    H = args.hold
    syms = _universe(args.universe, args.max_symbols)
    print(f"universe={args.universe} symbols={len(syms)} hold=open[T+{H}] cost(rt)={rt:.4f}", flush=True)

    trig_entry, all_entry = [], []        # angle a: trigger as entry vs baseline
    pb_all, pb_nowarn, pb_warn = [], [], []   # angle b: pullback_hv +/- active stress-warning
    dos_nowarn, dos_warn = [], []         # orthogonality: within deep-oversold subset
    lead_gaps = []                        # trigger -> next pullback_hv event gap (days)
    used = 0
    for si, sym in enumerate(syms):
        try:
            frame, _s = get_daily_history(sym, period=args.period)
        except Exception:
            continue
        if frame is None or frame.empty or "Close" not in frame or "Open" not in frame or len(frame) < 90 + H:
            continue
        close = pd.to_numeric(frame["Close"], errors="coerce").reset_index(drop=True)
        open_ = pd.to_numeric(frame["Open"], errors="coerce").reset_index(drop=True)
        baseline = exit_model.baseline_forward_returns(close, open_, H)
        base_mean = mean(baseline) if baseline else 0.0

        def net_at(i):
            r = exit_model.forward_return(close, open_, i, H)
            return None if r is None else (r - base_mean - rt)

        try:
            St, trig = _detector(frame, walk_forward=not args.full_fit)
        except Exception:
            continue
        rsi2, z = _osc(close)
        n = len(close)
        trig_idx = [i for i in range(n) if bool(trig.iloc[i])]
        trig_set = set(trig_idx)

        # angle a: trigger-as-entry + a matched baseline (all valid days)
        for i in trig_idx:
            if 25 <= i < n - H:
                v = net_at(i)
                if v is not None:
                    trig_entry.append({"ticker": sym, "net": v})
        for i in range(25, n - H, 3):  # subsample baseline (every 3d) to bound size
            v = net_at(i)
            if v is not None:
                all_entry.append({"ticker": sym, "net": v})

        # pullback_hv events + stress-warning flag
        evs = _collect_symbol_events(sym, frame, H, min_launch_score=0.0, min_tunnel_score=0.0, cooldown_days=5)
        dates = [ts.strftime("%Y-%m-%d") for ts in frame.index]
        pos = {d: k for k, d in enumerate(dates)}
        scored = [(e, pos.get(e["date"])) for e in evs if e.get("pullback_hv_score") is not None]
        if scored:
            cut = sorted(e["pullback_hv_score"] for e, _i in scored)
            cutoff = cut[int(args.top_quantile * (len(cut) - 1))]
            for e, i in scored:
                if i is None or i < 25 or i >= n - H:
                    continue
                if e["pullback_hv_score"] < cutoff:
                    continue
                v = net_at(i)
                if v is None:
                    continue
                warn = any((i - w) in trig_set for w in range(0, args.warn_window))
                row = {"ticker": sym, "net": v}
                pb_all.append(row)
                (pb_warn if warn else pb_nowarn).append(row)
                # orthogonality: same split, but only inside the deep-oversold subset
                dos = (pd.notna(z.iloc[i]) and z.iloc[i] < -1.5) or (pd.notna(rsi2.iloc[i]) and rsi2.iloc[i] < 10)
                if dos:
                    (dos_warn if warn else dos_nowarn).append(row)

        # lead-time: nearest pullback_hv event AFTER each trigger (within 15d)
        ev_idx = sorted(i for _e, i in scored if i is not None)
        for ti in trig_idx:
            nxt = [j for j in ev_idx if 0 < (j - ti) <= 15]
            if nxt:
                lead_gaps.append(nxt[0] - ti)
        used += 1
        if (si + 1) % 30 == 0:
            print(f"  ...{si+1}/{len(syms)} done, triggers so far={len(trig_entry)}", flush=True)

    print(f"\nsymbols_used={used}  triggers={len(trig_entry)}  pb_top={len(pb_all)} (warn={len(pb_warn)}/nowarn={len(pb_nowarn)})", flush=True)

    print("\n=== ANGLE (a): is the stress-warning TRIGGER a long-entry edge? ===", flush=True)
    _summ("trigger-as-entry", trig_entry)
    _summ("baseline (all days, subsampled)", all_entry)
    d, lo, hi = _diff_ci(trig_entry, all_entry)
    if d is not None:
        print(f"  [trigger] - [baseline]  diff={d*100:+.3f}%  CI95=[{lo}, {hi}]  -> {'edge' if (lo and lo>0) else 'no long edge (expected)'}", flush=True)

    print("\n=== ANGLE (b): does sitting out stress-warnings IMPROVE the pullback_hv edge? ===", flush=True)
    _summ("pullback_hv TOP (all)", pb_all)
    _summ("  + NO stress-warning", pb_nowarn)
    _summ("  + WITH stress-warning", pb_warn)
    d2, lo2, hi2 = _diff_ci(pb_nowarn, pb_warn)
    if d2 is not None:
        better = "FILTER HELPS (CI>0)" if (lo2 and lo2 > 0) else "no significant filter benefit (CI incl 0)"
        print(f"  [no-warn] - [with-warn]  diff={d2*100:+.3f}%  CI95=[{lo2}, {hi2}]  -> {better}", flush=True)

    print("\n=== ORTHOGONALITY: does the filter add INSIDE deep-oversold (z<-1.5 or rsi2<10)? ===", flush=True)
    _summ("DOS & NO stress-warning", dos_nowarn)
    _summ("DOS & WITH stress-warning", dos_warn)
    d3, lo3, hi3 = _diff_ci(dos_nowarn, dos_warn)
    if d3 is not None:
        verdict3 = "ADDS over DOS (CI>0)" if (lo3 and lo3 > 0) else "no increment over DOS (CI incl 0)"
        print(f"  [DOS no-warn] - [DOS with-warn]  diff={d3*100:+.3f}%  CI95=[{lo3}, {hi3}]  -> {verdict3}", flush=True)
    else:
        print("  insufficient DOS sample.", flush=True)

    print("\n=== lead-time (descriptive) ===", flush=True)
    if lead_gaps:
        print(f"  triggers followed by a pullback_hv setup within 15d: {len(lead_gaps)}; median lead = {median(lead_gaps)}d, mean = {mean(lead_gaps):.1f}d", flush=True)
    else:
        print("  no trigger->pullback_hv pairs within 15d.", flush=True)

    print("\n=== VERDICT ===", flush=True)
    mode = "FULL-FIT(lookahead)" if args.full_fit else "WALK-FORWARD(causal)"
    b_edge = (lo2 is not None and lo2 > 0 and len(pb_warn) >= 30)
    o_edge = (lo3 is not None and lo3 > 0 and len(dos_warn) >= 30)
    if b_edge and o_edge:
        print(f"  -> [{mode}] PASSES BOTH: filter helps the pullback_hv edge ({d2*100:+.3f}%, CI>0) AND adds INSIDE deep-oversold ({d3*100:+.3f}%, CI>0). Strong candidate to ship as a risk gate.", flush=True)
    elif b_edge and not o_edge:
        print(f"  -> [{mode}] filter helps overall ({d2*100:+.3f}%, CI>0) but does NOT add over deep-oversold (redundant with DOS). Marginal.", flush=True)
    elif not b_edge:
        print(f"  -> [{mode}] filter benefit NOT significant after this check (CI incl 0). The earlier result was likely lookahead/in-sample. Do NOT ship.", flush=True)
    print(f"\nMode={mode}. Caveats: in-sample; single 2y period; archive_only (survivorship); daily adaptation of an HFT/LOB method.", flush=True)


if __name__ == "__main__":
    main()
