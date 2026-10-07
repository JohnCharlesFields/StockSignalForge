"""Explicit, small-sample GilData/FMP/Massive/Cboe shadow comparison."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv


def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    from gildata_shadow_service import _root, compare_with_existing, probe

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True, help="US market session YYYY-MM-DD")
    parser.add_argument("--symbols", nargs="+", default=["AAPL", "NVDA"], help="At most five US tickers")
    parser.add_argument("--compare-live", action="store_true", help="Also call existing FMP/Massive/Cboe sources")
    args = parser.parse_args()

    report = probe(args.symbols, args.as_of)
    if args.compare_live:
        report = compare_with_existing(report)
    summary = {
        "report": str(_root() / "latest_report.json"),
        "as_of": report["as_of"],
        "symbols": {symbol: sample.get("available_fields", []) for symbol, sample in report["symbols"].items()},
        "vix": (report.get("vix") or {}).get("value"),
        "requests": report["requests"],
        "comparisons": report.get("comparisons", {}),
        "provider_cost": "unknown; consult GilData billing",
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
