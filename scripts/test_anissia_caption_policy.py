import datetime as dt
from contextlib import ExitStack
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from anissia_caption_policy import caption_item, is_historical_anime
from discover_subtitles import discover_anissia, KST
import pi_download_subtitle_queues as downloader
from backfill_anissia_empty_urls import caption_key as backfill_key


class WeeklyScopeTests(unittest.TestCase):
    today = dt.date(2026, 10, 8)

    def test_ended_work_boundary_and_active_upcoming_unknown_retained(self):
        boundary = self.today - dt.timedelta(days=90)
        self.assertFalse(is_historical_anime({"status": "END", "endDate": boundary.isoformat()}, self.today))
        self.assertTrue(is_historical_anime({"status": "END", "endDate": (boundary - dt.timedelta(days=1)).isoformat()}, self.today))
        for status in ("ON", "PRE", "", None):
            with self.subTest(status=status):
                self.assertFalse(is_historical_anime({"status": status, "endDate": "2000-01-01"}, self.today))

    def test_missing_end_date_requires_old_start_and_ended_status(self):
        self.assertTrue(is_historical_anime({"status": "END", "startDate": "2020-01-01"}, self.today))
        for start in ("2026-01-01", "", "invalid"):
            self.assertFalse(is_historical_anime({"status": "END", "startDate": start}, self.today))

    def test_historical_caption_api_is_skipped_even_with_old_api_retry(self):
        works = [
            {"animeNo": 1, "status": "END", "endDate": "2020-01-01"},
            {"animeNo": 2, "status": "ON", "startDate": "2020-01-01"},
            {"animeNo": 3, "status": "END", "endDate": "2026-09-30"},
            {"animeNo": 4, "status": "END"},
        ]
        state = {"discovered": {"anissia": {}}, "retry": {"anissia_api": {"1": {}}},
                 "bootstrap_cutoff_kst": "2026-09-25T23:59:59+09:00"}
        def response(_session, url):
            if "/list/" in url:
                return Mock(json=lambda: {"data": {"totalPages": 1, "content": works}})
            return Mock(json=lambda: {"data": [{"episode": "1", "name": "제작자", "updDt": "2026-10-08", "website": ""}]})
        queue = []
        with patch("discover_subtitles.request", side_effect=response) as request:
            scope = discover_anissia(Mock(), state, queue, [], dt.datetime(2026, 10, 1, tzinfo=KST), 0, today=self.today)
        self.assertEqual([row["anime_no"] for row in queue], ["2", "3", "4"])
        self.assertFalse(any("animeNo/1" in call.args[1] for call in request.call_args_list))
        self.assertEqual(scope, {"anissia_scanned_works": 3, "anissia_skipped_historical": 1})

    def test_item_identity_matches_existing_historical_backfill(self):
        caption = {"episode": "1", "name": "제작자", "updDt": "2020-01-01", "website": ""}
        self.assertEqual(caption_item("10", "과거작", caption)["id"], "anissia:" + backfill_key("10", caption))


class MonthlyWaitingUrlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.state = downloader.load_state(self.path)
        self.session = Mock()

    def waiter(self, anime_no="10", episode="1", creator="제작자", url=""):
        item = caption_item(anime_no, "과거작", {"episode": episode, "name": creator, "updDt": "2020-01-01", "website": url})
        downloader.move_to_waiting_url(self.state, item["id"], item)
        return item

    def refresh(self, captions, month="2026-10"):
        with patch.object(downloader, "request", return_value=Mock(json=lambda: {"data": captions})) as request:
            ready = downloader.refresh_waiting_urls(self.session, self.state, self.path, month)
        return ready, request

    def test_only_waiting_works_are_queried_once_each_month(self):
        self.waiter(episode="1")
        self.waiter(episode="2")
        self.state["success"]["other"] = {"item": {"anime_no": "999"}}
        self.state["retry"]["other"] = {"item": {"anime_no": "998", "source_url": "https://example.test"}}
        ready, request = self.refresh([])
        self.assertEqual(ready, [])
        request.assert_called_once_with(self.session, "https://api.anissia.net/anime/caption/animeNo/10")
        _, same_month = self.refresh([])
        same_month.assert_not_called()
        _, next_month = self.refresh([], "2026-11")
        next_month.assert_called_once()
        self.assertEqual(len(self.state["waiting_url"]), 2)

    def test_no_url_waiters_means_no_api_calls(self):
        _, request = self.refresh([])
        request.assert_not_called()

    def test_appearing_url_matches_exact_episode_creator_and_persists_before_download(self):
        old = self.waiter()
        captions = [
            {"episode": "2", "name": "제작자", "website": "https://wrong.test"},
            {"episode": "1", "name": "다른 제작자", "website": "https://wrong.test"},
            {"episode": "1", "name": "제작자", "updDt": "2020-02-01", "website": "https://example.test/sub.srt"},
        ]
        ready, _ = self.refresh(captions)
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]["source_url"], "https://example.test/sub.srt")
        self.assertNotEqual(ready[0]["id"], old["id"])
        self.assertEqual(ready[0]["anime_title"], "과거작")
        self.assertEqual(json.loads(self.path.read_text())["waiting_url"][old["id"]]["item"], ready[0])
        # A restart resumes the resolved item without opening the API again.
        self.state = downloader.load_state(self.path)
        again, request = self.refresh([])
        request.assert_not_called()
        self.assertEqual(again, ready)

    def test_wrong_episode_or_creator_cannot_replace_waiter(self):
        old = self.waiter()
        ready, _ = self.refresh([{"episode": "2", "name": "제작자", "website": "https://wrong.test"},
                                 {"episode": "1", "name": "다른 제작자", "website": "https://wrong.test"}])
        self.assertEqual(ready, [])
        self.assertEqual(self.state["waiting_url"][old["id"]]["item"], old)

    def test_distinct_urls_for_same_identity_remain_waiting(self):
        self.waiter()
        ready, _ = self.refresh([{"episode": "1", "name": "제작자", "website": url}
                                 for url in ("https://one.test", "https://two.test")])
        self.assertEqual(ready, [])
        self.assertEqual(self.state["anissia_waiting_url_checks"]["10"]["ambiguous"], 1)

    def test_duplicate_same_url_is_not_ambiguous(self):
        self.waiter()
        row = {"episode": "1", "name": "제작자", "website": "https://one.test"}
        ready, _ = self.refresh([row, row])
        self.assertEqual(len(ready), 1)

    def test_api_failure_preserves_waiter_without_download_retry_or_retirement(self):
        old = self.waiter()
        with patch.object(downloader, "request", side_effect=RuntimeError("API unavailable")):
            self.assertEqual(downloader.refresh_waiting_urls(self.session, self.state, self.path, "2026-10"), [])
        self.assertEqual(self.state["waiting_url"][old["id"]]["item"], old)
        self.assertEqual(self.state["retry"], {})
        self.assertEqual(self.state["retired"], {})
        self.assertEqual(self.state["anissia_waiting_url_checks"]["10"]["error"], "API unavailable")
        _, request = self.refresh([])
        request.assert_not_called()

    def test_invalid_api_payload_preserves_waiting_state(self):
        self.waiter()
        ready, _ = self.refresh({"unexpected": "data"})
        self.assertEqual(ready, [])
        self.assertIn("Unexpected", self.state["anissia_waiting_url_checks"]["10"]["error"])

    def test_erulabo_url_enters_existing_browser_queue_and_clears_old_waiter(self):
        self.waiter()
        ready, _ = self.refresh([{"episode": "1", "name": "제작자", "website": "https://erulabo.com/sub"}])
        self.assertFalse(downloader.process_item(self.session, self.state, self.path, ready[0], Path(self.tmp.name)))
        self.assertEqual(self.state["waiting_url"], {})
        self.assertIn(ready[0]["id"], self.state["erulabo_pending"])

    def test_previously_downloaded_url_clears_older_blank_identity(self):
        self.waiter()
        ready, _ = self.refresh([{"episode": "1", "name": "제작자", "website": "https://example.test/sub"}])
        self.state["success"][ready[0]["id"]] = {}
        self.assertTrue(downloader.process_item(self.session, self.state, self.path, ready[0], Path(self.tmp.name)))
        self.assertEqual(downloader.load_state(self.path)["waiting_url"], {})

    def test_existing_v1_state_migrates_without_losing_waiters(self):
        old = self.waiter()
        self.state.pop("anissia_waiting_url_checks")
        downloader.save_state(self.path, self.state)
        loaded = downloader.load_state(self.path)
        self.assertEqual(loaded["anissia_waiting_url_checks"], {})
        self.assertEqual(loaded["waiting_url"][old["id"]]["item"], old)

    def test_main_consumes_backfill_then_downloads_new_url_once(self):
        old = self.waiter()
        self.state["waiting_url"].clear()
        downloader.save_state(self.path, self.state)
        tag = "subs-queue-2026-10-08-123-1"
        release = {"tag_name": tag, "assets": [{"name": "subtitle-queue.json"}]}
        def write_queue(_asset, _token, path):
            path.write_text(json.dumps({"version": 1, "items": [old]}))
        with ExitStack() as stack:
            stack.enter_context(patch("sys.argv", ["downloader", "--state", str(self.path), "--target", self.tmp.name]))
            stack.enter_context(patch.dict("os.environ", {"ANIME_DB_GITHUB_TOKEN": "test-token"}))
            stack.enter_context(patch.object(downloader, "list_queue_releases", return_value=[release]))
            stack.enter_context(patch.object(downloader, "download_github_asset", side_effect=write_queue))
            stack.enter_context(patch.object(downloader, "request", return_value=Mock(json=lambda: {"data": [
                {"episode": "1", "name": "제작자", "website": "https://example.test/sub"}]})))
            download = stack.enter_context(patch.object(downloader, "download_anissia", side_effect=RuntimeError("download unavailable")))
            stack.enter_context(patch.object(downloader, "append_failure"))
            downloader.main()
        download.assert_called_once()
        saved = downloader.load_state(self.path)
        self.assertEqual(saved["waiting_url"], {})
        self.assertEqual(saved["processed_releases"], [tag])
        self.assertEqual(len(saved["retry"]), 1)
        self.assertEqual(next(iter(saved["retry"].values()))["item"]["source_url"], "https://example.test/sub")

    def test_resolved_url_download_success_uses_existing_storage(self):
        self.waiter()
        ready, _ = self.refresh([{"episode": "1", "name": "제작자", "website": "https://example.test/sub"}])
        target = Path(self.tmp.name) / "subtitles"
        target.mkdir()
        file = downloader.work_directory(ready[0], target) / "1화" / "제작자" / "1.srt"
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("1\n00:00:00,000 --> 00:00:01,000\n자막\n", encoding="utf-8")
        with patch.object(downloader, "download_anissia", return_value=[file]), patch.object(downloader, "maybe_backfill_history"), patch.object(downloader, "append_failure"):
            self.assertTrue(downloader.process_item(self.session, self.state, self.path, ready[0], target))
        self.assertEqual(self.state["waiting_url"], {})
        self.assertEqual(self.state["success"][ready[0]["id"]]["files"], [file.relative_to(target).as_posix()])


if __name__ == "__main__":
    unittest.main()
