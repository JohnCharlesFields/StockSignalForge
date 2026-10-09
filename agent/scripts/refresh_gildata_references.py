"""Optional cache warming. The caller enforces a hard subprocess timeout."""
from __future__ import annotations

import json
import sys
import time

from gildata_shadow_service import refresh_references, refresh_vix, refresh_research
from app_database import cache_get, cache_set
from market_calendar import most_recent_session


def main() -> None:
    options = json.loads(sys.stdin.read() or "{}")
    symbols = list(dict.fromkeys(options.get("symbols") or []))
    session = most_recent_session().isoformat()
    batches = []
    research = []
    cursor = int(cache_get("gildata:research_daily_cursor") or 0)
    if symbols:
        cursor %= len(symbols)
        symbols = symbols[cursor:] + symbols[:cursor]
    deadline = time.monotonic() + 140
    processed = 0
    for offset in range(0, len(symbols), 5):
        if time.monotonic() >= deadline:
            break
        result = refresh_references(symbols[offset:offset + 5], session)
        batches.append(result)
        print(json.dumps({"phase": "reference_batch", **result}, ensure_ascii=False), flush=True)
        evidence = refresh_research(symbols[offset:offset + 5], session)
        research.append(evidence)
        processed += min(5, len(symbols) - offset)
        cache_set("gildata:research_daily_cursor", cursor + processed)
        print(json.dumps({"phase": "research_batch", **evidence}, ensure_ascii=False), flush=True)
        if result.get("status") == "unavailable":
            break
    vix = refresh_vix(session)
    print(json.dumps({"status": "completed" if processed == len(symbols) and all(r.get("status") in {"completed", "cached"} for r in batches) and vix else "partial",
                      "as_of": session, "requested": len(symbols),
                      "written": sum(r.get("written", 0) for r in batches),
                      "research_written": sum(r.get("written", 0) for r in research),
                      "processed": processed, "remaining": len(symbols) - processed,
                      "research": research,
                      "batches": batches, "vix_available": bool(vix)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
