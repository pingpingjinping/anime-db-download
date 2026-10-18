"""Tests for unattended Pi downloads from public Anissia Drive folders."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pi_download_subtitle_queues as downloader
from subtitle_work_storage import load_manifest

FOLDER_ID = "1bX4uEJUwheB09VjjGpjCQ19CwkshVLfr"
FOLDER_URL = f"https://drive.google.com/drive/folders/{FOLDER_ID}"


def item():
    return {
        "id": "anissia:google-folder-test",
        "source": "anissia",
        "anime_no": "123",
        "anime_title": "스트라이크 위치스 501부대 발진합니다",
        "episode": "12",
        "creator": "테스트",
        "source_url": FOLDER_URL,
    }


def subtitle_name(number):
    return f"Strike Witches 501 Butai Hasshin Shimasu! - {number:02d} (BD 1280x720 x264 AAC).smi"


def html_fixture(files, folders=()):
    links = [
        f'<a href="https://drive.google.com/file/d/{file_id}/view?usp=drive_web">{name}</a>'
        for file_id, name in files
    ]
    links.extend(
        f'<a href="https://drive.google.com/drive/folders/{folder_id}">{name}</a>'
        for folder_id, name in folders
    )
    return "<html><body>" + "".join(links) + "</body></html>"


class DriveFolderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.target = Path(self.temp.name) / "subtitle-data"
        self.target.mkdir()
        self.state_path = Path(self.temp.name) / "state.json"
        self.state = downloader.load_state(self.state_path)
        self.work = item()

    def fake_download(self, fail_ids=()):
        calls = []
        def download(session, url, destination, suggested_name=""):
            file_id = url.split("/file/d/", 1)[1].split("/", 1)[0]
            calls.append(file_id)
            if file_id in fail_ids:
                raise RuntimeError("temporary file failure")
            destination.mkdir(parents=True, exist_ok=True)
            output = destination / suggested_name
            output.write_bytes(("subtitle:" + file_id).encode("utf-8"))
            return output
        return calls, download

    def test_folder_and_file_detection(self):
        self.assertEqual(downloader.google_drive_folder_id(FOLDER_URL + "?usp=sharing"), FOLDER_ID)
        self.assertIsNone(downloader.google_drive_file_id(FOLDER_URL))
        for account in (0, 1, 12):
            url = f"https://drive.google.com/drive/u/{account}/folders/{FOLDER_ID}/?usp=sharing"
            with self.subTest(account=account):
                self.assertEqual(downloader.google_drive_folder_id(url), FOLDER_ID)
                self.assertIsNone(downloader.google_drive_file_id(url))
        self.assertIsNone(downloader.google_drive_folder_id("https://evil.com/drive/folders/" + FOLDER_ID))
        self.assertIsNone(downloader.google_drive_folder_id("https://drive.google.com.evil.com/drive/u/0/folders/" + FOLDER_ID))
        file_url = "https://drive.google.com/file/d/1GtLeKHBW1_PANMl488ICaKNNNJQ_GR1N/view"
        self.assertEqual(downloader.google_drive_file_id(file_url), "1GtLeKHBW1_PANMl488ICaKNNNJQ_GR1N")
        self.assertIsNone(downloader.google_drive_folder_id(file_url))

    def test_episodes_and_ova_are_identified_from_original_names(self):
        self.assertEqual(downloader.drive_folder_episode(subtitle_name(5), "12"), "5")
        self.assertEqual(downloader.drive_folder_episode("Series S01E07.srt", "12"), "7")
        self.assertEqual(downloader.drive_folder_episode("Series - OVA (BD).smi", "12"), "OVA")
        self.assertEqual(downloader.drive_folder_episode("unknown.smi", "12"), "12")

    def test_public_embedded_folder_listing_and_subfolder(self):
        nested = "1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        first = html_fixture([("1" * 26, subtitle_name(1))], [(nested, "subfolder")])
        second = html_fixture([("2" * 26, "Series - OVA.smi")])
        with patch.object(downloader, "request", side_effect=[Mock(text=first), Mock(text=second)]) as fetch:
            entries = downloader.list_google_drive_folder(Mock(), FOLDER_ID)
        self.assertEqual(entries, [("1" * 26, subtitle_name(1)), ("2" * 26, "Series - OVA.smi")])
        self.assertEqual(fetch.call_count, 2)
        with patch.object(downloader, "request", return_value=Mock(text="<html>Sign in</html>")):
            with self.assertRaisesRegex(RuntimeError, "no visible entries"):
                downloader.list_google_drive_folder(Mock(), FOLDER_ID)

    def test_incomplete_embedded_listing_is_never_accepted(self):
        page = html_fixture([(f"{n:026d}", f"Episode {n}.smi") for n in range(50)])
        with patch.object(downloader, "request", return_value=Mock(text=page)):
            with self.assertRaisesRegex(RuntimeError, "may be incomplete"):
                downloader.list_google_drive_folder(Mock(), FOLDER_ID)

    def test_empty_movie_subfolder_does_not_discard_tv_subtitles(self):
        tv_id, movie_id = "1" * 26, "2" * 26
        expected = [("3" * 26, "Meitantei Precure! - 37.ass")]
        root = html_fixture([], [(tv_id, "TVA"), (movie_id, "극장판")])
        tv = html_fixture(expected)
        empty = '<html><head><title>극장판</title></head><body><div class="flip-entries"></div></body></html>'
        for pages in ([root, tv, empty], [html_fixture([], [(movie_id, "극장판"), (tv_id, "TVA")]), empty, tv]):
            with self.subTest(pages=pages), patch.object(
                downloader, "request", side_effect=[Mock(text=page) for page in pages],
            ):
                self.assertEqual(downloader.list_google_drive_folder(Mock(), FOLDER_ID), expected)

    def test_empty_root_or_unrecognized_nested_listing_still_fails(self):
        empty = '<html><body><div class="flip-entries"></div></body></html>'
        with patch.object(downloader, "request", return_value=Mock(text=empty)):
            with self.assertRaisesRegex(RuntimeError, "no visible entries"):
                downloader.list_google_drive_folder(Mock(), FOLDER_ID)
        root = html_fixture([("3" * 26, subtitle_name(1))], [("2" * 26, "subfolder")])
        for nested in (
            "<html>Sign in</html>",
            '<div class="flip-entries"><div class="flip-entry">unknown format</div></div>',
            '<div class="flip-entries">Access denied</div>',
        ):
            with self.subTest(nested=nested), patch.object(
                downloader, "request", side_effect=[Mock(text=root), Mock(text=nested)],
            ):
                with self.assertRaisesRegex(RuntimeError, "no visible entries"):
                    downloader.list_google_drive_folder(Mock(), FOLDER_ID)

    def test_account_folder_retry_uses_folder_sync_instead_of_website_parser(self):
        self.work["source_url"] = f"https://drive.google.com/drive/u/0/folders/{FOLDER_ID}"
        self.work["handler"] = "website"
        self.state["retry"][self.work["id"]] = {"item": self.work, "reason": "parser"}
        entries = [("1" * 26, subtitle_name(1))]
        calls, download = self.fake_download()
        with patch.object(downloader, "list_google_drive_folder", return_value=entries), \
             patch.object(downloader, "download_google_drive", side_effect=download), \
             patch.object(downloader, "download_anissia") as website, \
             patch.object(downloader, "maybe_backfill_history"):
            self.assertTrue(downloader.process_item(
                Mock(), self.state, self.state_path, self.work, self.target,
            ))
        website.assert_not_called()
        self.assertEqual(calls, ["1" * 26])
        self.assertNotIn(self.work["id"], self.state["retry"])
        self.assertIn(self.work["id"], self.state["success"])

    def test_downloads_12_and_ova_in_one_work_folder_and_reuses_files(self):
        entries = [(f"{n:026d}", subtitle_name(n)) for n in range(1, 13)]
        entries.append(("9" * 26, "Strike Witches 501 Butai Hasshin Shimasu! - OVA (BD).smi"))
        calls, downloader_stub = self.fake_download()
        with patch.object(downloader, "list_google_drive_folder", return_value=entries), \
             patch.object(downloader, "download_google_drive", side_effect=downloader_stub):
            paths = downloader.download_anissia_drive_folder(
                Mock(), self.work, self.target, self.state, self.state_path,
            )
            same = downloader.download_anissia_drive_folder(
                Mock(), self.work, self.target, self.state, self.state_path,
            )
        self.assertEqual(len(paths), 13)
        self.assertEqual(paths, same)
        self.assertEqual(len(calls), 13)
        self.assertEqual({p.parent for p in paths}, {self.target / "anissia_subtitles" / self.work["anime_title"]})
        manifest = load_manifest(self.target)["files"]
        self.assertEqual(len(manifest), 13)
        source_rows = [record["sources"][0] for record in manifest.values()]
        self.assertEqual({row["episode"] for row in source_rows}, {str(n) for n in range(1, 13)} | {"OVA"})
        self.assertTrue(all(row["source_url"] == FOLDER_URL for row in source_rows))

    def test_partial_failure_retains_good_files_then_retries_only_missing(self):
        entries = [("1" * 26, subtitle_name(1)), ("2" * 26, subtitle_name(2))]
        failed_calls, failed = self.fake_download(fail_ids=["2" * 26])
        with patch.object(downloader, "list_google_drive_folder", return_value=entries), \
             patch.object(downloader, "download_google_drive", side_effect=failed):
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                downloader.download_anissia_drive_folder(
                    Mock(), self.work, self.target, self.state, self.state_path,
                )
        self.assertEqual(len(load_manifest(self.target)["files"]), 1)
        self.assertEqual(len(self.state["drive_folders"][self.work["id"]]["files"]), 1)
        self.assertEqual(len(failed_calls), 2)
        success_calls, success = self.fake_download()
        with patch.object(downloader, "list_google_drive_folder", return_value=entries), \
             patch.object(downloader, "download_google_drive", side_effect=success):
            result = downloader.download_anissia_drive_folder(
                Mock(), self.work, self.target, self.state, self.state_path,
            )
        self.assertEqual(len(result), 2)
        self.assertEqual(success_calls, ["2" * 26])
        self.assertEqual(len(load_manifest(self.target)["files"]), 2)

    def test_new_files_are_downloaded_but_existing_files_untouched(self):
        first = [("1" * 26, subtitle_name(1))]
        calls, download = self.fake_download()
        with patch.object(downloader, "list_google_drive_folder", return_value=first), \
             patch.object(downloader, "download_google_drive", side_effect=download):
            downloader.download_anissia_drive_folder(Mock(), self.work, self.target, self.state, self.state_path)
        second = first + [("2" * 26, subtitle_name(2))]
        with patch.object(downloader, "list_google_drive_folder", return_value=second), \
             patch.object(downloader, "download_google_drive", side_effect=download):
            downloader.download_anissia_drive_folder(Mock(), self.work, self.target, self.state, self.state_path)
        self.assertEqual(calls, ["1" * 26, "2" * 26])


    def test_successful_post_can_be_reprocessed_when_folder_gains_files(self):
        current = [("1" * 26, subtitle_name(1))]
        calls, download = self.fake_download()
        def listing(session, folder_id):
            return list(current)
        with patch.object(downloader, "list_google_drive_folder", side_effect=listing), \
             patch.object(downloader, "download_google_drive", side_effect=download), \
             patch.object(downloader, "maybe_backfill_history"):
            self.assertTrue(downloader.process_item(
                Mock(), self.state, self.state_path, self.work, self.target,
            ))
            self.assertEqual(len(self.state["success"][self.work["id"]]["files"]), 1)
            current.append(("2" * 26, subtitle_name(2)))
            self.assertTrue(downloader.process_item(
                Mock(), self.state, self.state_path, self.work, self.target,
            ))
            self.assertEqual(len(self.state["success"][self.work["id"]]["files"]), 2)
            self.assertTrue(downloader.process_item(
                Mock(), self.state, self.state_path, self.work, self.target,
            ))
        self.assertEqual(calls, ["1" * 26, "2" * 26])


if __name__ == "__main__":
    unittest.main()
