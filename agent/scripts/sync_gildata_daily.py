"""JSON stdin/out worker for the existing daily preflight process watchdog."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gildata_daily_service import SYNC_VERSION, sync_daily

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True)
    parser.add_argument("--max-seconds", type=float, default=150)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        symbols = json.load(sys.stdin)["symbols"]
        print(json.dumps(sync_daily(symbols, args.session, max_seconds=args.max_seconds, execute=not args.dry_run), ensure_ascii=False), flush=True)
    except Exception as exc:
        print(json.dumps({"status": "unavailable", "source": "gildata:FinQuery:daily", "error": type(exc).__name__,
                          "phase": "worker", "sync_version": SYNC_VERSION, "symbols_written": 0}), flush=True)
        sys.exit(1)
