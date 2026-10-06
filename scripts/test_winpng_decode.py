#!/usr/bin/env python3
from __future__ import annotations

from io import BytesIO
import unittest

from PIL import Image

import winpng_decode


def _pack_bytes(data: bytes) -> list[int]:
    pixels: list[int] = []
    for offset in range(0, len(data), 3):
        chunk = data[offset:offset + 3].ljust(3, b"\x00")
        pixels.append((chunk[0] << 16) | (chunk[1] << 8) | chunk[2])
    return pixels


def _raw_container_png(path: str, data: bytes, width: int = 48) -> bytes:
    path_bytes = path.encode("utf-8")
    pixels = [len(path_bytes), len(data)] + _pack_bytes(path_bytes) + _pack_bytes(data)
    height = (len(pixels) + width - 1) // width
    pixels += [0] * (width * height - len(pixels))
    image = Image.new("RGB", (width, height))
    image.putdata([((p >> 16) & 0xFF, (p >> 8) & 0xFF, p & 0xFF) for p in pixels])
    output = BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def _get_b(a: int, d: int) -> int:
    return (
        (((a & 0xFF0000) + (d & 0xFF0000)) & 0x1FE0000)
        | (((a & 0x00FF00) + (d & 0x00FF00)) & 0x001FE00)
        | (((a & 0x0000FF) + (d & 0x0000FF)) & 0x00001FE)
    ) >> 1


def _get_c(a: int, d: int) -> int:
    return (
        ((0x1000000 + (a & 0xFF0000) - (d & 0xFF0000)) & 0x1FE0000)
        | ((0x0010000 + (a & 0x00FF00) - (d & 0x00FF00)) & 0x001FE00)
        | ((0x0000100 + (a & 0x0000FF) - (d & 0x0000FF)) & 0x00001FE)
    ) >> 1


def _get_bp3(a: int, d: int) -> int:
    return (
        (
            0xC00000 - (((0x0800000 + (a & 0xFF0000) + 2 * (d & 0xFF0000)) >> 3) & 0xFE0000)
            if ((a & 0x800000) ^ (d & 0x800000)) == 0
            else (((0x2800000 + (a & 0xFF0000) - 2 * (d & 0xFF0000)) >> 2) & 0xFC0000) | 0x010000
        )
        | (
            0x00C000 - (((0x0008000 + (a & 0x00FF00) + 2 * (d & 0x00FF00)) >> 3) & 0x00FE00)
            if ((a & 0x008000) ^ (d & 0x008000)) == 0
            else (((0x0028000 + (a & 0x00FF00) - 2 * (d & 0x00FF00)) >> 2) & 0x00FC00) | 0x000100
        )
        | (
            0x0000C0 - (((0x0000080 + (a & 0x0000FF) + 2 * (d & 0x0000FF)) >> 3) & 0x0000FE)
            if ((a & 0x000080) ^ (d & 0x000080)) == 0
            else (((0x0000280 + (a & 0x0000FF) - 2 * (d & 0x0000FF)) >> 2) & 0x0000FC) | 0x000001
        )
    )


def _get_cp3(a: int, d: int) -> int:
    return (
        (
            0xC00000 - (((0x2800000 + (a & 0xFF0000) - 2 * (d & 0xFF0000)) >> 3) & 0xFE0000)
            if ((a & 0x800000) ^ (d & 0x800000)) == 0x800000
            else ((0x0800000 + (a & 0xFF0000) + 2 * (d & 0xFF0000)) >> 2) & 0xFC0000
        )
        | (
            0x00C000 - (((0x0028000 + (a & 0x00FF00) - 2 * (d & 0x00FF00)) >> 3) & 0x00FE00)
            if ((a & 0x008000) ^ (d & 0x008000)) == 0x008000
            else ((0x0008000 + (a & 0x00FF00) + 2 * (d & 0x00FF00)) >> 2) & 0x00FC00
        )
        | (
            0x0000C0 - (((0x0000280 + (a & 0x0000FF) - 2 * (d & 0x0000FF)) >> 3) & 0x0000FE)
            if ((a & 0x000080) ^ (d & 0x000080)) == 0x000080
            else ((0x0000080 + (a & 0x0000FF) + 2 * (d & 0x0000FF)) >> 2) & 0x0000FC
        )
    )


def _wrap_114v3(data_png: bytes) -> bytes:
    with Image.open(BytesIO(data_png)) as source:
        source = source.convert("RGB")
        width, height = source.size
        data_pixels = [(r << 16) | (g << 8) | b for r, g, b in source.getdata()]

    out_width = width * 2
    out = [0] * (out_width * height * 2)
    for y in range(height):
        for x in range(width):
            d = data_pixels[y * width + x]
            a = (
                (((40 + x * 3 + y * 5) & 0xFF) << 16)
                | (((100 + x * 7) & 0xFF) << 8)
                | ((160 + y * 11) & 0xFF)
            )
            bp = _get_bp3(a, d)
            cp = _get_cp3(a, d)
            b = (_get_b(a, d) + bp - 0x808080) & 0xFFFFFF
            c = (_get_c(a, d) + cp - 0x808080) & 0xFFFFFF
            an = (a + 0x1010100 - bp - cp) & 0xFFFFFF
            base = (2 * y) * out_width + 2 * x
            out[base] = a
            out[base + 1] = b
            out[base + out_width] = c
            out[base + out_width + 1] = an

    image = Image.new("RGB", (out_width, height * 2))
    image.putdata([((p >> 16) & 0xFF, (p >> 8) & 0xFF, p & 0xFF) for p in out])
    output = BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


class WinPNGDecodeTests(unittest.TestCase):
    def test_current_114v3_twice_round_trip(self):
        expected = b"[Script Info]\nTitle: WinPNG test\n" + bytes(range(64))
        first_pass = _raw_container_png("subs/test.ass", expected, width=32)
        twice = _raw_container_png("", first_pass, width=48)
        encoded = _wrap_114v3(twice)

        files = winpng_decode.decode_winpng_bytes(encoded)

        self.assertEqual([(item.path, item.data) for item in files], [("subs/test.ass", expected)])

    def test_rejects_path_traversal(self):
        encoded = _raw_container_png("../escape.ass", b"nope", width=16)
        self.assertEqual(winpng_decode.decode_winpng_bytes(encoded), [])


if __name__ == "__main__":
    unittest.main()
