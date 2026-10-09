"""Manifest-verified CSV access shared by backtest summaries and paging."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from src.tools.path_utils import safe_run_dir

MAX_FILE_BYTES = 64 * 1024 * 1024


def load_card(run_dir: str) -> tuple[Path, dict]:
    root = safe_run_dir(run_dir)
    path = root / "run_card.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("Missing or invalid run_card.json; run the built-in backtest first")
    card = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(card, dict) or not isinstance(card.get("artifacts"), list):
        raise ValueError("Invalid run card manifest")
    return root, card


def verified_artifact(root: Path, card: dict, name: str) -> tuple[Path, dict]:
    aliases = {"equity": "artifacts/equity.csv", "trades": "artifacts/trades.csv", "metrics": "artifacts/metrics.csv"}
    relative = aliases.get(name, name)
    if name.startswith("ohlcv:"):
        ticker = name.split(":", 1)[1]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", ticker):
            raise ValueError("Invalid OHLCV ticker")
        relative = f"artifacts/ohlcv_{ticker}.csv"
    posix, windows = PurePosixPath(relative), PureWindowsPath(relative)
    if "\\" in relative or ":" in relative or posix.is_absolute() or windows.is_absolute() or windows.drive or ".." in posix.parts:
        raise ValueError("Artifact must be a relative manifest path without traversal")
    record = next((a for a in card["artifacts"] if isinstance(a, dict) and a.get("path") == relative), None)
    if not record:
        raise ValueError("Artifact is not listed in the run card")
    path = root / relative
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink():
            raise ValueError("Symlink artifacts are not allowed")
    if not path.resolve().is_relative_to(root) or not path.is_file() or path.suffix.lower() != ".csv":
        raise ValueError("Only CSV files inside the run directory are supported")
    size = path.stat().st_size
    if size > MAX_FILE_BYTES or size != record.get("size_bytes"):
        raise ValueError("Artifact size mismatch or file too large")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != record.get("sha256"):
        raise ValueError("Artifact hash mismatch: regenerate the backtest instead of editing engine outputs")
    return path, record


def typed_cell(value: str | None) -> Any:
    if value is None or value.strip().lower() in {"", "nan", "null", "none", "inf", "-inf", "infinity"}:
        return None
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        number = float(value)
    except ValueError:
        return value
    if not math.isfinite(number):
        return None
    if re.fullmatch(r"-?(0|[1-9][0-9]*)", value):
        return int(value)
    return number


def csv_rows(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            yield {key or "time": typed_cell(value) for key, value in row.items() if key is not None}


def clean_json(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: clean_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_json(item) for item in value]
    return value
