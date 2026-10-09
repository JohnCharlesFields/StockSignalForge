"""Ground the backtest tool in existing engine outputs, not stdout snippets."""
from __future__ import annotations

import json
from pathlib import Path

from src.tools._run_artifacts import clean_json, csv_rows, load_card, verified_artifact
from src.tools.run_artifact_tool import read_run_artifact


def build_backtest_summary(run_dir: Path) -> dict:
    root, card = load_card(str(run_dir))
    metrics_path, _ = verified_artifact(root, card, "metrics")
    metrics = next(csv_rows(metrics_path), {})
    # CSV grounds scalars; the run card retains structured validation evidence.
    summary = {"schema_version": "1", "run_id": root.name, "generated_at": card.get("generated_at"),
               "backtest": card.get("backtest", {}), "data_sources": card.get("data_sources", []),
               "reproducibility": card.get("reproducibility", {}), "metrics": metrics,
               "validation": clean_json(card.get("validation")), "warnings": card.get("warnings", []),
               "validation_scope": "Monte Carlo shuffles trade order only; this is not leakage-free out-of-sample alpha validation.",
               "artifact_reader": "read_run_artifact", "artifacts": card["artifacts"]}
    equity = json.loads(read_run_artifact(str(root), "equity", mode="downsample", limit=10))
    if equity.get("status") == "ok":
        summary["equity_preview"] = equity["rows"]
    else:
        summary["warnings"] = list(summary["warnings"]) + [equity.get("error", "Equity preview unavailable")]
    return clean_json(summary)
