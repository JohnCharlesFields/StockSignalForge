from __future__ import annotations

import os
import time
import unittest
from unittest.mock import patch

import deepseek_decision_assistant as assistant


class DeepSeekModelCacheTests(unittest.TestCase):
    def test_model_switch_does_not_reuse_old_review(self) -> None:
        context = {"symbol": "NVDA", "scan_universe": "spx"}
        with patch.dict(os.environ, {"LANGCHAIN_PROVIDER": "deepseek", "LANGCHAIN_MODEL_NAME": "deepseek-v4-pro"}):
            old_key = assistant._context_digest(context)
            old_symbol_key = assistant._symbol_cache_key(context)
        old_review = {
            "available": False,
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "review_focus": "three_layer_research",
        }
        cache = {
            old_key: {"created_ts": time.time(), "review": old_review},
            old_symbol_key: {"created_ts": time.time(), "review": old_review},
        }
        with (
            patch.dict(os.environ, {"LANGCHAIN_PROVIDER": "deepseek", "LANGCHAIN_MODEL_NAME": "deepseek-flash"}),
            patch.object(assistant, "_load_cache", return_value=cache),
        ):
            self.assertNotEqual(assistant._context_digest(context), old_key)
            self.assertIsNone(assistant.get_cached_review(context))


if __name__ == "__main__":
    unittest.main()
