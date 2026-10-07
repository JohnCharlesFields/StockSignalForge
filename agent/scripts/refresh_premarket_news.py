"""Isolated news refresh; the caller owns the hard process timeout."""
from __future__ import annotations

import json
import sys

from premarket_news_service import refresh_premarket_news


if __name__ == "__main__":
    options = json.loads(sys.stdin.read() or "{}")
    result = refresh_premarket_news(**options)
    print(json.dumps(result, ensure_ascii=False), flush=True)
