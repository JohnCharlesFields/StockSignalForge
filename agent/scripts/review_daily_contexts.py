"""Run cached-context DeepSeek reviews in a killable subprocess."""
from __future__ import annotations

import json
import sys

from deepseek_decision_assistant import review_top_contexts


if __name__ == "__main__":
    contexts = json.loads(sys.stdin.read() or "[]")
    print(json.dumps(review_top_contexts(contexts, limit=12), ensure_ascii=False), flush=True)
