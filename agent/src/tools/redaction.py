"""Redact internal absolute paths from user-facing strings (CWE-209/497, P10).

Redacts ONLY known-internal root prefixes (anchored to the running env) and
keeps the relative tail — caller-supplied/external absolute paths stay intact
for diagnosability; only our own topology is hidden. Idempotent: each root is
replaced by the literal '<redacted>' sentinel, which is not itself a root, so
a second pass is a no-op. No regex (zero ReDoS surface).
"""

from __future__ import annotations

import sys
import sysconfig
from functools import cache
from pathlib import Path

_SENTINEL = "<redacted>"


@cache
def _internal_roots() -> list[str]:
    # AGENT_DIR derived like agent/src/providers/llm.py:90
    # (Path(__file__).resolve().parents[2]); redaction.py is agent/src/tools/
    # so parents[2] == the agent/ dir. Anchor tracks layout, not hardcoded.
    agent_dir = Path(__file__).resolve().parents[2]
    cands = [
        Path.home(),
        Path.cwd(),
        agent_dir,
        agent_dir.parent,
        Path(sys.prefix),
        Path(sys.base_prefix),
        Path(sysconfig.get_paths().get("purelib", "")),
        Path(sysconfig.get_paths().get("platlib", "")),
    ]
    roots: set[str] = set()
    for c in cands:
        s = str(c)
        if len(s) > 3 and s not in (".", "/", "\\"):
            roots.add(s)
            roots.add(s.replace("\\", "/"))
            roots.add(s.replace("/", "\\"))
    return sorted(roots, key=len, reverse=True)


def redact_internal_paths(text: object) -> str:
    """Replace internal root prefixes with '<redacted>', keep relative tail."""
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)
    if not s:
        return s
    for root in _internal_roots():
        if root in s:
            s = s.replace(root, _SENTINEL)
    return s
