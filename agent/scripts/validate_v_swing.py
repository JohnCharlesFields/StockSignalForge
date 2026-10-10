"""Offline validation using existing caches only; never downloads price data."""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_calendar import most_recent_session
from market_data_service import _read_daily_cache
from v_swing_model import annotations, clean_bars, config, fit_models, training_panel, FEATURES, score_day
from v_swing_validation import group_audit, forward_audit


def validate():
    started = time.monotonic()
    cfg = config()
    session = most_recent_session().isoformat()
    histories = {symbol: clean_bars(_read_daily_cache(symbol), session) for symbol in annotations()}
    panel, data_audit = training_panel(histories, session, cfg)
    print(json.dumps({"phase": "data", "rows": len(panel), "labels": data_audit["used_buy"] + data_audit["used_sell"]}), flush=True)
    model = fit_models(panel, cfg)
    group = group_audit(panel, cfg)
    print(json.dumps({"phase": "stock_group", "supported": group["pattern_supported"]}), flush=True)
    forward = forward_audit(histories, cfg)
    importance = {side: dict(zip(FEATURES, classifier.feature_importances_.tolist())) for side, classifier in model["models"].items()}
    ema_share = {side: sum(value for name, value in weights.items() if "ema" in name or name in ("bull", "bear")) for side, weights in importance.items()}
    supported = bool(group["pattern_supported"] and min(ema_share.values()) >= .5)
    scores = {symbol: score_day(model, symbol, frame, session, cfg) for symbol, frame in histories.items()}
    return {"session": session, "data_audit": data_audit, "group_audit": group, "forward_audit": forward,
            "config_hash": hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest(),
            "training_hash": hashlib.sha256(pd.util.hash_pandas_object(panel, index=False).values.tobytes()).hexdigest(),
            "thresholds": model["thresholds"], "feature_importance": importance, "ema_importance_share": ema_share,
            "pattern_supported": supported, "experimental": True, "scores": scores,
            "elapsed_seconds": round(time.monotonic()-started, 2),
            "data_boundary": {"source": "existing_ohlcv_cache", "external_calls": 0, "point_in_time_universe": False,
                              "hindsight_free_annotations": False, "corporate_actions_verified": False}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = validate()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "pattern_supported": report["pattern_supported"],
                      "data_audit": {k: v for k, v in report["data_audit"].items() if k != "omitted"},
                      "ema_share": report["ema_importance_share"], "resolved_buys": report["forward_audit"].get("resolved_buys"),
                      "net_win_rate": report["forward_audit"].get("net_win_rate"),
                      "calibration": report["forward_audit"].get("profit_calibration"), "seconds": report["elapsed_seconds"]}, ensure_ascii=False), flush=True)
