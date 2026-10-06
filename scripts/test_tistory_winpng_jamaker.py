#!/usr/bin/env python3
import base64
import unittest

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

        files = _decode_result(row, {"episode.jmk"})

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


if __name__ == "__main__":
    unittest.main()
