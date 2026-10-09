from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import premarket_news_service


class PremarketNewsFreshnessTests(unittest.TestCase):
    def test_commentary_only_queue_does_not_disappear(self) -> None:
        rows = [
            {"news_id": "first", "symbol": "AAPL", "source_tier": "retail_commentary"},
            {"news_id": "second", "symbol": "NVDA", "source_tier": "press_release"},
        ]
        self.assertEqual(premarket_news_service._apply_source_mix_cap(rows, 120), rows)
        self.assertEqual(premarket_news_service._apply_source_mix_cap(rows, 1), rows[:1])

    def test_mixed_queue_retains_source_cap(self) -> None:
        quality = [{"symbol": "AAPL", "source_tier": "authoritative"} for _ in range(20)]
        low = [{"symbol": "NVDA", "source_tier": "retail_commentary"} for _ in range(20)]
        result = premarket_news_service._apply_source_mix_cap(quality + low, 40)
        self.assertLessEqual(sum(row["source_tier"] == "retail_commentary" for row in result) / len(result), .15)
        self.assertEqual(result[:20], quality)

    def test_empty_queue_remains_empty_and_macro_is_exempt(self) -> None:
        self.assertEqual(premarket_news_service._apply_source_mix_cap([], 120), [])
        macro = {"symbol": "MARKET", "source_tier": "retail_commentary"}
        self.assertEqual(premarket_news_service._apply_source_mix_cap([macro], 120), [macro])

    def test_recent_queue_excludes_old_unread_without_deleting_it(self) -> None:
        now = datetime.now(timezone.utc)
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row

        @contextmanager
        def memory_connection():
            yield db

        with (
            patch.object(premarket_news_service, "connection", memory_connection),
            patch.object(premarket_news_service, "ensure_database"),
            patch.object(premarket_news_service, "cache_get", return_value=None),
            patch.object(premarket_news_service, "cache_set"),
            patch.object(premarket_news_service, "_hydrate_missing_metadata", side_effect=lambda rows: rows),
            patch.object(premarket_news_service, "_hydrate_missing_translations", side_effect=lambda rows: rows),
        ):
            premarket_news_service.ensure_premarket_news_tables()
            with memory_connection() as conn:
                for news_id, age_days, score in (("recent", 1, 65), ("old", 30, 95)):
                    conn.execute(
                        """INSERT INTO premarket_news_items
                           (news_id, symbol, title_original, published_utc,
                            importance_score, adjusted_importance_score, fetched_at, updated_at)
                           VALUES (?, 'AAPL', ?, ?, ?, ?, ?, ?)""",
                        (
                            news_id,
                            news_id,
                            (now - timedelta(days=age_days)).isoformat(),
                            score,
                            score,
                            now.isoformat(),
                            now.isoformat(),
                        ),
                    )
                conn.commit()

            recent = premarket_news_service.list_premarket_news(recent_days=7)
            archive = premarket_news_service.list_premarket_news(recent_days=0)
            db.execute("UPDATE premarket_news_items SET publisher='The Motley Fool'")
            commentary = premarket_news_service.list_premarket_news(recent_days=7, hydrate=False)
            db.execute("INSERT INTO premarket_news_feedback(news_id,symbol,decision,created_at,updated_at) VALUES('recent','AAPL','important','now','now')")
            reviewed = premarket_news_service.list_premarket_news(recent_days=7, hydrate=False)
            db.execute("DELETE FROM premarket_news_feedback")
            with (
                patch.object(premarket_news_service, "_hydrate_missing_metadata", side_effect=AssertionError("external metadata lookup")),
                patch.object(premarket_news_service, "_hydrate_missing_translations", side_effect=AssertionError("external translation lookup")),
            ):
                fast = premarket_news_service.list_premarket_news(recent_days=7, hydrate=False)
        db.close()

        self.assertEqual([item["news_id"] for item in recent["items"]], ["recent"])
        self.assertEqual({item["news_id"] for item in archive["items"]}, {"recent", "old"})
        self.assertEqual(recent["total_unreviewed"], 1)
        self.assertEqual(archive["total_unreviewed"], 2)
        self.assertEqual([item["news_id"] for item in fast["items"]], ["recent"])
        self.assertTrue(commentary["source_mix_degraded"])
        self.assertTrue(commentary["source_mix_warning"])
        self.assertEqual([item["news_id"] for item in commentary["items"]], ["recent"])
        self.assertEqual(reviewed["items"], [])
        self.assertFalse(reviewed["source_mix_degraded"])


if __name__ == "__main__":
    unittest.main()
