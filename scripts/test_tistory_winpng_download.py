import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pi_download_subtitle_queues as downloader
from winpng_decode import EmbeddedFile


class WinPNGDownloadTests(unittest.TestCase):
    def download(self, destination, raw, converted):
        with patch.object(downloader, "tistory_winpng_image_urls", return_value=["image.png"]), \
             patch.object(downloader, "request", return_value=Mock()), \
             patch.object(downloader, "response_bytes", return_value=b"image"), \
             patch.object(downloader, "decode_winpng_bytes", return_value=raw), \
             patch.object(downloader, "convert_tistory_jmks", return_value=converted) as converter:
            result = downloader.download_tistory_winpng(Mock(), "https://author.tistory.com/1", None, destination)
            return result, converter.call_args.args[1]

    def test_corrected_same_name_output_succeeds_without_overwriting_old_ass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "subtitle.ass").write_bytes(b"old")
            files, requested = self.download(root, [EmbeddedFile("subtitle.jmk", b"raw")],
                                             [EmbeddedFile("subtitle.ass", b"corrected")])
            self.assertEqual(requested, {"subtitle.jmk"})
            self.assertEqual((root / "subtitle.ass").read_bytes(), b"old")
            self.assertEqual([p.read_bytes() for p in files], [b"corrected"])
            self.assertFalse(list(root.rglob("*.jmk")))

    def test_partial_conversion_retains_every_raw_input_until_retry_succeeds(self):
        raw = [EmbeddedFile(f"{ep}화/subtitle.jmk", str(ep).encode()) for ep in (1, 2)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(RuntimeError, "2화/subtitle.jmk"):
                self.download(root, raw, [EmbeddedFile("1화/subtitle.ass", b"one")])
            self.assertEqual(len(list(root.rglob("*.jmk"))), 2)
            files, requested = self.download(root, raw, [
                EmbeddedFile("1화/subtitle.ass", b"one"),
                EmbeddedFile("2화/subtitle.ass", b"two")])
            self.assertEqual(requested, {"1화/subtitle.jmk", "2화/subtitle.jmk"})
            self.assertEqual(len(files), 2)
            self.assertFalse(list(root.rglob("*.jmk")))

    def test_digest_renamed_jmk_uses_original_path_and_keeps_older_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "subtitle.jmk").write_bytes(b"older raw")
            files, requested = self.download(root, [EmbeddedFile("subtitle.jmk", b"new raw")],
                                             [EmbeddedFile("subtitle.ass", b"converted")])
            self.assertEqual(requested, {"subtitle.jmk"})
            self.assertEqual((root / "subtitle.jmk").read_bytes(), b"older raw")
            self.assertEqual(len(list(root.rglob("*.jmk"))), 1)
            self.assertEqual(files[0].read_bytes(), b"converted")

    def test_unrelated_playable_file_does_not_authorize_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                self.download(root, [EmbeddedFile("subtitle.jmk", b"raw"),
                                     EmbeddedFile("other.ass", b"unrelated")], [])
            self.assertTrue((root / "subtitle.jmk").exists())

    def test_conflicting_original_paths_are_not_declared_successful(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(RuntimeError, "conflicting JMK"):
                self.download(root, [EmbeddedFile("subtitle.jmk", b"one"),
                                     EmbeddedFile("subtitle.jmk", b"two")],
                              [EmbeddedFile("subtitle.ass", b"converted")])
            self.assertEqual(len(list(root.rglob("*.jmk"))), 2)


if __name__ == "__main__":
    unittest.main()
