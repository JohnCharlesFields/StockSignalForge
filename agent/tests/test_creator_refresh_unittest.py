import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app_database as db
import creator_opinion_service as service


def video(number=1, published="2026-10-08T23:25:16Z"):
    return {"video_id": f"testVid{number:04d}", "title": f"Video {number}", "published_at": published,
            "url": f"https://www.youtube.com/watch?v=testVid{number:04d}"}


class CreatorRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stopall)
        patch.object(db, "DB_PATH", Path(self.tmp.name) / "test.sqlite3").start()
        patch.object(db, "_INITIALIZED", False).start()
        patch.object(service, "_JOB_ACTIVE", False).start()
        service.ensure_creator_tables()

    def test_seven_defaults_preserve_disabled_and_existing_channel_id(self):
        service.seed_default_channels()
        service._upsert_channel("NaNaShuoMeiGu", channel_id="UC" + "a" * 22)
        with db.connection() as conn:
            conn.execute("UPDATE creator_channels SET enabled=0 WHERE handle='TradesMax'")
            conn.commit()
        channels = service.list_channels()
        self.assertEqual(len(channels), 7)
        self.assertEqual(next(c for c in channels if c["handle"] == "TradesMax")["enabled"], 0)
        self.assertEqual(next(c for c in channels if c["handle"] == "NaNaShuoMeiGu")["channel_id"], "UC" + "a" * 22)

    def test_all_metadata_is_saved_before_first_analysis(self):
        channels = [{"handle": "first", "channel_id": "UC" + "a" * 22, "enabled": 1},
                    {"handle": "second", "channel_id": "UC" + "b" * 22, "enabled": 1}]
        events = []
        def analyze(video_id, **kwargs):
            with db.connection() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM creator_videos").fetchone()[0], 4)
            events.append(video_id)
            return {"available": False, "reason": "subtitle unavailable"}
        with patch.object(service, "list_channels", return_value=channels), \
                patch.object(service, "_rss_latest", side_effect=[[video(1), video(2)], [video(3), video(4)]]), \
                patch.object(service, "extract_opinion", side_effect=analyze), patch.object(service, "save_sector_signals_from_opinions"):
            result = service.refresh_creator_opinions(limit_per_channel=3)
        self.assertEqual(result["new_videos"], 4)
        self.assertEqual(len(events), 2)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["pending_analysis"], 2)

    def test_one_failed_channel_does_not_block_other_channels(self):
        channels = [{"handle": "first", "channel_id": "UC" + "a" * 22, "enabled": 1},
                    {"handle": "second", "channel_id": "UC" + "b" * 22, "enabled": 1}]
        with patch.object(service, "list_channels", return_value=channels), \
                patch.object(service, "_rss_latest", side_effect=[RuntimeError("secret-token"), [video()]]), \
                patch.object(service, "extract_opinion", return_value={"available": True}), \
                patch.object(service, "save_sector_signals_from_opinions"):
            result = service.refresh_creator_opinions()
        self.assertEqual(result["new_videos"], 1)
        self.assertEqual(result["status"], "partial")
        self.assertNotIn("secret-token", str(result))

    def test_repeated_discovery_preserves_ready_opinion(self):
        service._store_video("first", "UC" + "a" * 22, video())
        payload = {"available": True, "status": "llm_reviewed", "summary": "Existing verified transcript summary"}
        with db.connection() as conn:
            conn.execute("INSERT INTO creator_opinions VALUES (?,?,?,?,?,?,?,?)",
                         (video()["video_id"], "first", video()["published_at"], "llm_reviewed", "deepseek-flash", json.dumps(payload), "2026-10-01", "2026-10-01"))
            conn.commit()
        with patch.object(service, "fetch_transcript") as transcript:
            result = service.extract_opinion(video()["video_id"])
        self.assertEqual(result["summary"], payload["summary"])
        transcript.assert_not_called()

    def test_unavailable_transcript_never_generates_title_only_opinion(self):
        service._store_video("first", "UC" + "a" * 22, video())
        with patch.object(service, "fetch_transcript", return_value={"available": False, "error": "subtitle_unavailable"}), \
                patch.object(service, "_deepseek_opinion") as llm, patch.object(service, "_heuristic_opinion") as heuristic:
            result = service.extract_opinion(video()["video_id"])
        self.assertFalse(result["available"])
        self.assertEqual(result["ticker_views"], [])
        llm.assert_not_called()
        heuristic.assert_not_called()

    def test_failed_opinion_retries_when_real_transcript_appears(self):
        service._store_video("first", "UC" + "a" * 22, video())
        with patch.object(service, "fetch_transcript", return_value={"available": False, "error": "none"}):
            service.extract_opinion(video()["video_id"])
        with db.connection() as conn:
            conn.execute("INSERT INTO creator_transcripts VALUES (?,?,?,?,?,?,?)", (video()["video_id"], "zh", "subtitles", "test", 100, "actual transcript", service._utc_now()))
            conn.commit()
        with patch.object(service, "_deepseek_opinion", return_value={"available": True, "status": "llm_reviewed"}) as llm:
            result = service.extract_opinion(video()["video_id"])
        self.assertTrue(result["available"])
        llm.assert_called_once()

    def test_failed_opinion_has_retry_cooldown(self):
        service._store_video("first", "UC" + "a" * 22, video())
        with patch.object(service, "fetch_transcript", return_value={"available": False, "error": "none"}):
            service.extract_opinion(video()["video_id"])
        with patch.object(service, "fetch_transcript") as transcript:
            result = service.extract_opinion(video()["video_id"])
        self.assertFalse(result["available"])
        self.assertEqual(result["retry_after_seconds"], 3600)
        transcript.assert_not_called()

    def test_hard_timeout_copies_cookies_without_modifying_original(self):
        original = Path(self.tmp.name) / "original.txt"
        original.write_text("synthetic test cookie", encoding="utf-8")
        def process(args, **kwargs):
            copy = Path(args[-1])
            self.assertNotEqual(copy, original)
            copy.write_text("worker changed only copy", encoding="utf-8")
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        with patch.object(service, "COOKIES_FILE", original), patch.object(service.subprocess, "run", side_effect=process):
            with self.assertRaises(subprocess.TimeoutExpired):
                service._bounded_transcript(video()["video_id"])
        self.assertEqual(original.read_text(), "synthetic test cookie")

    def test_extractor_enables_installed_node_without_remote_scripts(self):
        import yt_dlp
        subtitle = {"ext": "json3", "url": "https://www.youtube.com/api/timedtext?test=synthetic"}
        with patch.object(service.shutil, "which", return_value="/usr/local/bin/node"), \
                patch.object(yt_dlp, "YoutubeDL") as ydl:
            ydl.return_value.__enter__.return_value.extract_info.return_value = {"subtitles": {"zh-CN": [subtitle]}}
            result = service._extract_with_ytdlp(video()["video_id"])
        opts = ydl.call_args.args[0]
        self.assertEqual(opts["js_runtimes"], {"node": {"path": "/usr/local/bin/node"}})
        self.assertNotIn("remote_components", opts)
        self.assertTrue(opts["skip_download"])
        self.assertEqual(result["language"], "zh-CN")

    def test_login_warning_is_not_mislabeled_as_missing_subtitles(self):
        import yt_dlp
        def info(*args, **kwargs):
            opts = ydl.call_args.args[0]
            opts["logger"].warning("Sign in to confirm you are not a bot. sensitive-test-cookie")
            return {"subtitles": {}, "automatic_captions": {}}
        with patch.object(yt_dlp, "YoutubeDL") as ydl:
            ydl.return_value.__enter__.return_value.extract_info.side_effect = info
            with self.assertRaisesRegex(RuntimeError, "^youtube_auth_required$"):
                service._extract_with_ytdlp(video()["video_id"])
        self.assertEqual(ydl.call_count, 1)
        message = service._transcript_error(RuntimeError("youtube_auth_required"))
        self.assertIn("人工验证", message)
        self.assertNotIn("sensitive-test-cookie", message)

    def test_other_warnings_do_not_imply_expired_cookies(self):
        logger = service._CaptionLogger()
        logger.warning("This client does not support cookies")
        logger.debug("synthetic secret")
        self.assertFalse(logger.auth_required)

    def test_job_is_async_deduplicated_and_terminal(self):
        with patch.object(service.threading, "Thread") as thread, \
                patch.object(service.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout='{"status":"completed","finished_at":"2020-01-01T00:00:00Z"}')):
            first = service.start_creator_refresh()
            duplicate = service.start_creator_refresh()
            self.assertTrue(first["started"])
            self.assertFalse(duplicate["started"])
            thread.call_args.kwargs["target"]()
        self.assertFalse(service._JOB_ACTIVE)
        self.assertEqual(service.creator_refresh_status()["status"], "completed")

    def test_job_timeout_keeps_metadata_and_becomes_partial(self):
        service._store_video("first", "UC" + "a" * 22, video())
        with patch.object(service.threading, "Thread") as thread, patch.object(service.subprocess, "run", side_effect=subprocess.TimeoutExpired("worker", 300)):
            service.start_creator_refresh()
            thread.call_args.kwargs["target"]()
        self.assertEqual(service.creator_refresh_status()["status"], "partial")
        self.assertEqual(len(service.list_opinion_feed()["items"]), 1)

    def test_restart_marks_persisted_running_as_interrupted(self):
        db.cache_set(service._STATUS_KEY, {"status": "running"})
        self.assertEqual(service.creator_refresh_status()["status"], "interrupted")

    def test_display_date_and_order_respect_timezones(self):
        service._store_video("first", "UC" + "a" * 22, video(1, "2026-10-08T23:25:16Z"))
        service._store_video("first", "UC" + "a" * 22, video(2, "2026-10-09T06:00:00+08:00"))
        feed = service.list_opinion_feed()
        self.assertEqual(feed["items"][0]["video_id"], video(1)["video_id"])
        self.assertEqual(feed["latest_date"], "2026-10-09")
        self.assertEqual(feed["latest_count"], 2)

    def test_all_failed_channels_report_failed_not_success(self):
        channels = [{"handle": "first", "channel_id": "UC" + "a" * 22, "enabled": 1}]
        with patch.object(service, "list_channels", return_value=channels), \
                patch.object(service, "_rss_latest", side_effect=TimeoutError()), \
                patch.object(service, "save_sector_signals_from_opinions"):
            self.assertEqual(service.refresh_creator_opinions()["status"], "failed")

    def test_mixed_success_and_missing_caption_is_partial(self):
        channels = [{"handle": "first", "channel_id": "UC" + "a" * 22, "enabled": 1}]
        with patch.object(service, "list_channels", return_value=channels), \
                patch.object(service, "_rss_latest", return_value=[video(1), video(2)]), \
                patch.object(service, "extract_opinion", side_effect=[{"available": True}, {"available": False}]), \
                patch.object(service, "save_sector_signals_from_opinions"):
            result = service.refresh_creator_opinions(limit_per_channel=2)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["opinions_ready"], 1)
        self.assertEqual(result["unavailable_opinions"], 1)

    def test_old_completed_record_with_failed_opinion_displays_partial(self):
        db.cache_set(service._STATUS_KEY, {"status": "completed", "channels": [
            {"videos": [{"opinion_available": True}, {"opinion_available": False}]}]})
        self.assertEqual(service.creator_refresh_status()["status"], "partial")

    def test_background_api_returns_without_waiting_or_running_sync_scan(self):
        import api_server as api
        with patch.object(service, "start_creator_refresh", return_value={"started": True, "status": "queued"}) as start, \
                patch.object(service, "refresh_creator_opinions") as sync, \
                patch.object(api.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            result = asyncio.run(api.creator_opinion_refresh(api.CreatorOpinionRefreshRequest(background=True)))
        self.assertEqual(result["status"], "queued")
        start.assert_called_once()
        sync.assert_not_called()
        sleep.assert_not_called()

    def test_legacy_api_waits_for_same_job_instead_of_starting_second_scan(self):
        import api_server as api
        with patch.object(service, "start_creator_refresh", return_value={"started": True, "status": "queued"}) as start, \
                patch.object(service, "creator_refresh_status", return_value={"status": "completed"}), \
                patch.object(service, "refresh_creator_opinions") as sync, \
                patch.object(api.asyncio, "sleep", new_callable=AsyncMock):
            result = asyncio.run(api.creator_opinion_refresh(api.CreatorOpinionRefreshRequest()))
        self.assertEqual(result["status"], "completed")
        start.assert_called_once()
        sync.assert_not_called()

    def test_scheduler_disabled_never_starts_job(self):
        import api_server as api
        with patch.dict(service.os.environ, {"CREATOR_OPINION_AUTO_ENABLED": "0"}), \
                patch.object(service, "start_creator_refresh") as start:
            api._creator_opinion_auto_loop()
        start.assert_not_called()

    def test_scheduler_defers_while_daily_scan_is_running(self):
        import api_server as api
        with patch.dict(service.os.environ, {"CREATOR_OPINION_AUTO_ENABLED": "1"}), \
                patch.object(api, "_AUTO_SCAN_JOB", {"status": "running"}), \
                patch.object(api.time, "sleep", side_effect=[None, RuntimeError("stop loop")]), \
                patch.object(service, "start_creator_refresh") as start:
            with self.assertRaisesRegex(RuntimeError, "stop loop"):
                api._creator_opinion_auto_loop()
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
