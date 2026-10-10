"""Bounded subprocess: local training/validation/scoring, no external data calls."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.validate_v_swing import validate
from v_swing_service import run_scoring

if __name__ == "__main__":
    try:
        request = json.load(sys.stdin)
        symbols = request.get("symbols") or []
        if not isinstance(symbols, list) or len(symbols) > 5000:
            raise ValueError("invalid_v_swing_universe")
        result = run_scoring(symbols, validate(), request.get("source_pools"))
        print(json.dumps({key: value for key, value in result.items() if key not in {"scores", "charts", "data_audit"}}, ensure_ascii=False), flush=True)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}, ensure_ascii=False), flush=True)
        sys.exit(1)
