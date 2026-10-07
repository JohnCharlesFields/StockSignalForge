#!/usr/bin/env python3
"""Archive daily ETF constituent snapshots for reproducible research."""

from __future__ import annotations

import argparse
import json

from screening_framework_v2_optimized import CONFIG, UNIVERSE_REGISTRY, resolve_universe


def main() -> None:
    parser = argparse.ArgumentParser(description="Archive ETF universe snapshots without running the options model.")
    parser.add_argument("--include-experimental", action="store_true", help="Also archive AIPO and QTUM experimental pools")
    parser.add_argument("--snapshot-dir", default=CONFIG["universe_snapshot_dir"], help="Override snapshot output directory")
    parser.add_argument("--universes", default="", help="Optional comma-separated registry universe keys")
    args = parser.parse_args()
    CONFIG["universe_snapshot_dir"] = args.snapshot_dir

    requested = [x.strip().lower() for x in args.universes.split(",") if x.strip()]
    keys = requested or [
        key for key, spec in sorted(UNIVERSE_REGISTRY.items(), key=lambda item: int(item[1]["priority"]))
        if args.include_experimental or spec["tier"] != "experimental"
    ]
    summary = []
    for key in keys:
        if key not in UNIVERSE_REGISTRY:
            raise ValueError(f"unsupported registry universe: {key}")
        tickers, source, _ = resolve_universe(key)
        summary.append({"universe": key, "ticker_count": len(tickers), "source": source})
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
