"""Bounded read-only paging of verified engine artifacts."""
from __future__ import annotations

import csv
import json
import os

from src.agent.tools import BaseTool
from src.tools._run_artifacts import csv_rows, load_card, verified_artifact


def read_run_artifact(run_dir: str, artifact: str, *, offset: int = 0, limit: int = 20, columns: list[str] | None = None, mode: str = "rows") -> str:
    try:
        if mode not in {"rows", "meta", "downsample"} or offset < 0 or not 1 <= limit <= 500:
            raise ValueError("Use rows/meta/downsample, offset >= 0 and limit 1..500")
        if mode == "downsample" and (offset != 0 or limit < 2):
            raise ValueError("Downsample requires offset=0 and limit >= 2")
        root, card = load_card(run_dir)
        path, record = verified_artifact(root, card, artifact)
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            fieldnames = [key or "time" for key in next(reader, [])]
            total = sum(1 for _ in reader)
        selected = fieldnames if columns is None else columns
        if not isinstance(selected, list) or any(key not in fieldnames for key in selected):
            raise ValueError("Unknown columns; use mode=meta to inspect available columns")
        payload = {"status": "ok", "artifact": record["path"], "sha256": record["sha256"], "verified": True,
                   "columns": selected, "total_rows": total, "offset": offset, "mode": mode, "rows": [], "next_offset": None}
        if mode == "meta":
            return json.dumps(payload, ensure_ascii=False, allow_nan=False)
        budget = max(1000, min(10000, int(os.getenv("TOOL_RESULT_LIMIT", "10000"))) - 200)
        indices = set(round(i * (total - 1) / (min(limit, total) - 1)) for i in range(min(limit, total))) if mode == "downsample" and total > 1 else {0}
        for index, row in enumerate(csv_rows(path)):
            if mode == "rows":
                if index < offset:
                    continue
                if len(payload["rows"]) >= limit:
                    break
            elif index not in indices:
                continue
            payload["rows"].append({key: row.get(key) for key in selected})
            payload["next_offset"] = index + 1 if mode == "rows" and index + 1 < total else None
            if len(json.dumps(payload, ensure_ascii=False, allow_nan=False)) > budget:
                payload["rows"].pop()
                if mode == "downsample":
                    raise ValueError("Downsample exceeds context budget; reduce limit or select fewer columns")
                payload["next_offset"] = index
                if not payload["rows"]:
                    raise ValueError("One row exceeds context budget; select fewer columns")
                break
        return json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (OSError, ValueError, TypeError, csv.Error) as exc:
        return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)


class ReadRunArtifactTool(BaseTool):
    name = "read_run_artifact"
    description = "Read manifest/hash-verified backtest CSVs without flooding context. Aliases: metrics, equity, trades, ohlcv:TICKER. Use meta to list columns, rows for paging (next_offset), downsample for an equity preview. Never edit engine artifacts."
    parameters = {"type": "object", "properties": {
        "run_dir": {"type": "string"}, "artifact": {"type": "string"},
        "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 500},
        "columns": {"type": "array", "items": {"type": "string"}},
        "mode": {"type": "string", "enum": ["rows", "meta", "downsample"]}}, "required": ["run_dir", "artifact"]}
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs) -> str:
        return read_run_artifact(**kwargs)
