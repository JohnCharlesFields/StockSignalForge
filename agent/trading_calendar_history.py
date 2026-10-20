"""Complete event versions shared by persistence and offline replay."""
from __future__ import annotations

from copy import deepcopy

SNAPSHOT_KEY = "_calendar_snapshot_version"
SNAPSHOT_VERSION = 2
_REQUIRED = {"event_id", "category", "date", "title", "status", "confidence", "symbols", "detail"}


def semantic_payload(event):
    value = {k: v for k, v in event.items() if k not in {"updated_at", "fetched_at", SNAPSHOT_KEY}}
    value.setdefault("symbols", [])
    value.setdefault("detail", {})
    return value


def full_snapshot(event):
    value = deepcopy(event)
    value.setdefault("symbols", [])
    value.setdefault("detail", {})
    value[SNAPSHOT_KEY] = SNAPSHOT_VERSION
    return value


def is_full_snapshot(value):
    return (isinstance(value, dict) and value.get(SNAPSHOT_KEY) == SNAPSHOT_VERSION
            and _REQUIRED <= value.keys() and isinstance(value["symbols"], list)
            and all(isinstance(s, dict) for s in value["symbols"])
            and isinstance(value["detail"], dict))
