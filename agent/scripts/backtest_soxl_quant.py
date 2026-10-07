"""CLI wrapper for the SOXL-only Blue Ocean paper strategy."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


AGENT = Path(__file__).resolve().parents[1]
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

from soxl_quant_service import estimate_history_cost, run_research_backtest  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2025-08-24")
    parser.add_argument("--end", default=None)
    parser.add_argument("--capital", type=float, default=2000.0)
    parser.add_argument("--force-download", action="store_true")
    parser.add_argument("--estimate-only", action="store_true")
    parser.add_argument("--source", choices=("databento", "yfinance_proxy"), default="databento")
    args = parser.parse_args()
    if args.estimate_only:
        end = args.end or "2026-07-10"
        print(json.dumps(estimate_history_cost(args.start, end), indent=2))
        return
    result = run_research_backtest(
        start=args.start,
        end=args.end,
        initial_capital=args.capital,
        force_download=args.force_download,
        persist=True,
        source=args.source,
    )
    compact = {
        "run_id": result["run_id"],
        "selected_config": result["selected_config"],
        "splits": result["splits"],
        "train": {k: v for k, v in result["train"].items() if k not in {"trades", "equity_curve"}},
        "validation": {k: v for k, v in result["validation"].items() if k not in {"trades", "equity_curve"}},
        "test": {k: v for k, v in result["test"].items() if k not in {"trades", "equity_curve"}},
        "test_always_long_benchmark": result["test_always_long_benchmark"],
        "test_excess_vs_always_long": result["test_excess_vs_always_long"],
        "limitations": result["limitations"],
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
