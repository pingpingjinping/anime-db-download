#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import json
import unittest
import zipfile
from pathlib import Path

from subtitle_queue_metadata import (
    aniall_folder_identity,
    build_download_metadata,
    classify_failure,
    classify_waiting_reason,
    history_title_matches,
    read_episode_evidence,
    queue_cache_cleanup,
)


class SubtitleQueueMetadataTests(unittest.TestCase):
    def test_cleanup_queue_contains_only_new_paths_and_has_stable_job_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file = root / "anissia_subtitles/100__작품/1화/제작자/01.srt"
            file.parent.mkdir(parents=True)
            file.write_text("subtitle")
            item = {"id": "anissia:test", "source": "anissia", "anime_no": "100", "anime_title": "작품", "episode": "1"}
            job = queue_cache_cleanup(item, [file], root)
            payload = json.loads(job.read_text())
            self.assertEqual(payload["files"], [file.relative_to(root).as_posix()])
            self.assertEqual(queue_cache_cleanup(item, [file], root), job)
            self.assertEqual(len(list(job.parent.glob("*.json"))), 1)
            with self.assertRaises(ValueError):
                queue_cache_cleanup(item, [root.parent / "outside.srt"], root)

    def test_aniall_same_title_posts_use_distinct_source_ids(self):
        first = aniall_folder_identity({"post_id": "100", "title": "동일 제목", "source_url": "https://aniall.tistory.com/100"})
        second = aniall_folder_identity({"post_id": "101", "title": "동일 제목", "source_url": "https://aniall.tistory.com/101"})
        self.assertEqual(first, "100__동일 제목")
        self.assertNotEqual(first, second)

    def test_aniall_invalid_or_conflicting_identity_is_rejected(self):
        for post_id, url in (("", "https://aniall.tistory.com/100"),
                             ("../100", "https://aniall.tistory.com/100"),
                             ("100", "https://aniall.tistory.com/101")):
            with self.subTest(post_id=post_id):
                with self.assertRaises(ValueError):
                    aniall_folder_identity({"post_id": post_id, "source_url": url})

    def test_history_match_keeps_distinguishing_subtitle(self):
        self.assertFalse(history_title_matches(
            "책벌레의 하극상 영주의 양녀 1화",
            "책벌레의 하극상 ~사서가 되기 위해서라면 뭐든지 할 수 있어~",
        ))

    def test_history_match_allows_omitted_season_suffix(self):
        self.assertTrue(history_title_matches(
            "정반대의 너와 나 13화",
            "정반대의 너와 나 2기",
        ))

    def test_history_match_normalizes_punctuation_and_spacing(self):
        self.assertTrue(history_title_matches(
            "무직 전생 III - 이세계에 갔으면 최선을 다한다 - 14화",
            "무직 전생 III ~이세계에 갔으면 최선을 다한다~",
        ))

    def test_waiting_reason_separates_unreleased_from_missing_url(self):
        self.assertEqual(
            classify_waiting_reason({
                "source": "anissia", "episode": "0", "source_url": "",
            }),
            "future_or_unreleased",
        )
        self.assertEqual(
            classify_waiting_reason({
                "source": "anissia", "episode": "12", "source_url": "",
            }),
            "no_url_yet",
        )

    def test_failure_reason_classification(self):
        self.assertEqual(
            classify_failure("NameResolutionError: Temporary failure in name resolution"),
            "dns",
        )
        self.assertEqual(
            classify_failure("SSLError: CERTIFICATE_VERIFY_FAILED"),
            "ssl",
        )
        self.assertEqual(
            classify_failure("404 Client Error: Not Found for url: https://example.test/a"),
            "http_404",
        )
        self.assertEqual(
            classify_failure("Tistory subtitle attachment not found"),
            "parser",
        )

    def test_range_is_coverage_not_direct_episode(self):
        evidence = read_episode_evidence("Show 1-12.srt", filename=True)
        self.assertEqual(evidence["episodes"], [])
        self.assertEqual(evidence["ranges"], [[1, 12]])

    def test_season_range_is_not_episode_range(self):
        evidence = read_episode_evidence("Show Season1-4.zip", filename=True)
        self.assertEqual(evidence["ranges"], [])
        self.assertTrue(evidence["multiple_seasons"])

    def test_explicit_season_file_keeps_episode(self):
        evidence = read_episode_evidence("Season 2/01.srt", filename=True)
        self.assertEqual(evidence["episodes"], [1])
        self.assertEqual(evidence["seasons"], [2])

    def test_zip_coverage_verifies_requested_episode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "all.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                for episode in range(1, 13):
                    zf.writestr(f"Season 2/{episode:02d}.srt", "subtitle")
            metadata = build_download_metadata(
                {
                    "source": "anissia",
                    "anime_title": "정반대의 너와 나 2기",
                    "episode": "12",
                },
                [archive],
                root,
            )
            self.assertEqual(metadata["verification"]["status"], "verified")
            self.assertEqual(metadata["coverage"]["episodes"], list(range(1, 13)))
            self.assertEqual(metadata["coverage"]["seasons"], [2])

    def test_direct_conflicting_filename_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subtitle = root / "12.srt"
            subtitle.write_text("subtitle", encoding="utf-8")
            metadata = build_download_metadata(
                {
                    "source": "anissia",
                    "anime_title": "작품",
                    "episode": "1",
                },
                [subtitle],
                root,
            )
            self.assertEqual(metadata["verification"]["status"], "ambiguous")

    def test_absolute_archive_numbering_is_preserved_without_guessing_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "13-24.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                for episode in range(13, 25):
                    zf.writestr(f"{episode:02d}.srt", "subtitle")
            metadata = build_download_metadata(
                {
                    "source": "anissia",
                    "anime_title": "작품 2기",
                    "episode": "1",
                },
                [archive],
                root,
            )
            self.assertEqual(metadata["verification"]["status"], "unverified")
            self.assertEqual(metadata["coverage"]["episodes"], list(range(13, 25)))
            self.assertIn(
                "archive episode numbering does not directly match requested episode",
                metadata["verification"]["notes"],
            )


if __name__ == "__main__":
    unittest.main()
