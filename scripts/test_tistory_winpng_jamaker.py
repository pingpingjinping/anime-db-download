#!/usr/bin/env python3
import base64
import unittest
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import tistory_winpng_jamaker as jamaker
from tistory_winpng_jamaker import _decode_result


class TistoryWinPNGJamakerTests(unittest.TestCase):
    def test_collects_ass_and_smi_for_expected_jmk(self):
        row = {
            "dir": "subs/",
            "name": "episode.jmk",
            "assName": "episode.ass",
            "ass": base64.b64encode(b"ASS DATA").decode("ascii"),
            "smiName": "episode.smi",
            "smi": base64.b64encode(b"SMI DATA").decode("ascii"),
        }

        files = _decode_result(row, {"subs/episode.jmk"})

        self.assertEqual(
            [(item.path, item.data) for item in files],
            [("subs/episode.ass", b"ASS DATA"), ("subs/episode.smi", b"SMI DATA")],
        )

    def test_ignores_unexpected_jmk(self):
        row = {
            "dir": "",
            "name": "other.jmk",
            "assName": "other.ass",
            "ass": base64.b64encode(b"ASS").decode("ascii"),
            "smiName": None,
            "smi": None,
        }
        self.assertEqual(_decode_result(row, {"episode.jmk"}), [])

    def test_rejects_generated_path_traversal(self):
        row = {
            "dir": "../",
            "name": "episode.jmk",
            "assName": "episode.ass",
            "ass": base64.b64encode(b"ASS").decode("ascii"),
            "smiName": None,
            "smi": None,
        }
        self.assertEqual(_decode_result(row, {"episode.jmk"}), [])

    def test_same_basename_in_another_episode_is_not_a_match(self):
        row = {"dir": "2화/", "name": "subtitle.jmk", "assName": "subtitle.ass",
               "ass": base64.b64encode(b"ASS").decode("ascii")}
        self.assertEqual(_decode_result(row, {"1화/subtitle.jmk"}), [])
        self.assertEqual(_decode_result(row, {"2화/subtitle.jmk"})[0].path, "2화/subtitle.ass")

    def test_generated_output_must_belong_to_the_original_jmk(self):
        row = {"dir": "1화/", "name": "subtitle.jmk", "assName": "other.ass",
               "ass": base64.b64encode(b"ASS").decode("ascii")}
        self.assertEqual(_decode_result(row, {"1화/subtitle.jmk"}), [])

    def test_converter_visits_both_images_with_the_same_basename(self):
        def row(episode):
            return {"dir": f"{episode}화/", "name": "subtitle.jmk", "assName": "subtitle.ass",
                    "ass": base64.b64encode(str(episode).encode()).decode("ascii")}
        page = MagicMock()
        page.evaluate.side_effect = [
            None, [{"key": "one"}, {"key": "two"}],
            True, [row(1)], True, [row(2)],
        ]
        playwright = MagicMock()
        playwright.chromium.launch.return_value.new_page.return_value = page
        manager = MagicMock()
        manager.return_value.__enter__.return_value = playwright
        api = ModuleType("playwright.sync_api")
        api.sync_playwright = manager
        with patch.object(jamaker.shutil, "which", return_value="/usr/bin/chromium"), \
             patch.dict(sys.modules, {"playwright": ModuleType("playwright"), "playwright.sync_api": api}), \
             patch.object(jamaker, "_wait_state", return_value="done"):
            files = jamaker.convert_tistory_jmks("https://author.tistory.com/1",
                                                {"1화/subtitle.jmk", "2화/subtitle.jmk"})
        self.assertEqual([item.path for item in files], ["1화/subtitle.ass", "2화/subtitle.ass"])


if __name__ == "__main__":
    unittest.main()
