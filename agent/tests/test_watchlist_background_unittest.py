from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

import api_server


class WatchlistBackgroundTests(unittest.TestCase):
    def test_first_read_returns_before_metrics_and_reuses_result(self) -> None:
        released = threading.Event()
        finished = threading.Event()
        cache: dict[str, dict] = {}
        item = {"symbol": "AAPL", "name": "Apple", "note": "review", "enabled": True}

        def metrics(symbol: str, _item: dict) -> dict:
            released.wait(timeout=3)
            return {"symbol": symbol, "current_price": 123.0, "alpha_win_rate": 0.6}

        def save(key: str, row: dict, ttl_seconds: int) -> None:
            cache[key] = row
            finished.set()

        with (
            patch.object(api_server, "user_watchlist_list", return_value=[item]),
            patch.object(api_server, "cache_get", side_effect=lambda key: cache.get(key)),
            patch.object(api_server, "cache_set", side_effect=save),
            patch.object(api_server, "_watchlist_symbol_metrics", side_effect=metrics),
        ):
            start = time.monotonic()
            first = api_server.get_watchlist(enrich=True, include_disabled=True)
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, 1.0)
            self.assertTrue(first["refreshing"])
            self.assertEqual(first["pending_count"], 1)
            self.assertEqual(first["items"][0]["symbol"], "AAPL")
            released.set()
            self.assertTrue(finished.wait(timeout=3))
            second = api_server.get_watchlist(enrich=True, include_disabled=True)
            self.assertFalse(second["refreshing"])
            self.assertEqual(second["items"][0]["current_price"], 123.0)


if __name__ == "__main__":
    unittest.main()
