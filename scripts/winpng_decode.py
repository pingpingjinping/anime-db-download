from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath

from PIL import Image, UnidentifiedImageError

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_EMBEDDED_FILE_BYTES = 20 * 1024 * 1024
MAX_PATH_BYTES = 255
MAX_RECURSION = 4


@dataclass(frozen=True)
class EmbeddedFile:
    path: str
    data: bytes


def _rgb24(value) -> int:
    if isinstance(value, int):
        return value & 0xFFFFFF
    r, g, b = value[:3]
    return (r << 16) | (g << 8) | b


def _image_pixels(image: Image.Image) -> tuple[int, int, list[int]]:
    rgb = image.convert("RGB")
    width, height = rgb.size
    return width, height, [_rgb24(value) for value in rgb.getdata()]


def _path_length_from_rgb(rgb: int) -> int:
    rgb &= 0xFFFFFF
    if (rgb & 0xFFFF00) == 0:
        return rgb & 0xFF
    return ((rgb & 0x070000) >> 11) | ((rgb & 0x000300) >> 5) | (rgb & 0x000007)


def _bytes_from_pixels(pixels: list[int], offset: int, length: int) -> bytes:
    out = bytearray(length)
    for i in range(length):
        pixel = pixels[offset + i // 3] & 0xFFFFFF
        out[i] = (pixel >> (8 * (2 - i % 3))) & 0xFF
    return bytes(out)


def _safe_embedded_path(path: str) -> str | None:
    normalized = path.replace("\\", "/").strip("/ \t\r\n")
    if not normalized or "\x00" in normalized:
        return None
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        return None
    if any(any(ord(ch) < 32 for ch in part) for part in pure.parts):
        return None
    if any(any(ch in part for ch in '*?"<>|') for part in pure.parts):
        return None
    return pure.as_posix()


def _decode_raw_container_image(width: int, height: int, pixels: list[int],
                                depth: int) -> list[EmbeddedFile]:
    if width < 2 or height < 1 or len(pixels) != width * height:
        return []

    files: list[EmbeddedFile] = []
    offset_y = 0
    while offset_y < height:
        row_start = offset_y * width
        if row_start + 1 >= len(pixels):
            break

        path_length = _path_length_from_rgb(pixels[row_start])
        binary_length = pixels[row_start + 1] & 0xFFFFFF
        if path_length > MAX_PATH_BYTES or binary_length > MAX_EMBEDDED_FILE_BYTES:
            return []

        pixel_count = 2 + ((path_length + 2) // 3) + ((binary_length + 2) // 3)
        container_height = (pixel_count + width - 1) // width
        if container_height <= 0 or offset_y + container_height > height:
            return []

        container_pixels = pixels[row_start:(offset_y + container_height) * width]
        data_offset = 2
        try:
            path_bytes = _bytes_from_pixels(container_pixels, data_offset, path_length)
            data_offset += (path_length + 2) // 3
            binary = _bytes_from_pixels(container_pixels, data_offset, binary_length)
        except (IndexError, ValueError):
            return []

        if path_length == 0:
            # WinPNG's "twice" mode stores a complete first-pass PNG inside a
            # path-less container. Random junk uses the same zero-path marker,
            # so only recurse when the payload is actually a PNG.
            if depth < MAX_RECURSION and binary.startswith(PNG_SIGNATURE):
                files.extend(decode_winpng_bytes(binary, depth=depth + 1))
        else:
            try:
                raw_path = path_bytes.decode("utf-8")
            except UnicodeDecodeError:
                return []
            safe_path = _safe_embedded_path(raw_path)
            if safe_path is None:
                return []
            files.append(EmbeddedFile(safe_path, binary))

        offset_y += container_height

    return files


def _get_d(b: int, c: int) -> int:
    return (
        (0x800000 + (b & 0xFF0000) - (c & 0xFF0000))
        | (0x008000 + (b & 0x00FF00) - (c & 0x00FF00))
        | (0x000080 + (b & 0x0000FF) - (c & 0x0000FF))
    ) & 0xFFFFFF


def _get_ap(a: int, an: int) -> int:
    return 0x808080 + (an & 0xFFFFFF) - (a & 0xFFFFFF)


def _b2_to_b(bn: int, ap: int) -> int:
    return (
        0x808080 + bn
        - (0x800000 if (ap & 0x010000) == 0 else (0x1000000 - (ap & 0xFF0000)))
        - (0x008000 if (ap & 0x000100) == 0 else (0x0010000 - (ap & 0x00FF00)))
        - (0x000080 if (ap & 0x000001) == 0 else (0x0000100 - (ap & 0x0000FF)))
    ) & 0xFFFFFF


def _c2_to_c(cn: int, ap: int) -> int:
    return (
        0x808080 + cn
        - (0x800000 if (ap & 0x010000) == 0x010000 else (0x1000000 - (ap & 0xFF0000)))
        - (0x008000 if (ap & 0x000100) == 0x000100 else (0x0010000 - (ap & 0x00FF00)))
        - (0x000080 if (ap & 0x000001) == 0x000001 else (0x0000100 - (ap & 0x0000FF)))
    ) & 0xFFFFFF


def _b3_to_b(bn: int, ap: int) -> int:
    return (
        0x808080 + bn
        - ((ap & 0xFF0000) if (ap & 0x010000) == 0
           else (0x17F0000 - ((ap & 0xFF0000) << 1)))
        - ((ap & 0x00FF00) if (ap & 0x000100) == 0
           else (0x0017F00 - ((ap & 0x00FF00) << 1)))
        - ((ap & 0x0000FF) if (ap & 0x000001) == 0
           else (0x000017F - ((ap & 0x0000FF) << 1)))
    ) & 0xFFFFFF


def _c3_to_c(cn: int, ap: int) -> int:
    return (
        0x808080 + cn
        - ((0x1800000 - ((ap & 0xFF0000) << 1)) if (ap & 0x010000) == 0
           else ((ap & 0xFF0000) + 0x010000))
        - ((0x0018000 - ((ap & 0x00FF00) << 1)) if (ap & 0x000100) == 0
           else ((ap & 0x00FF00) + 0x000100))
        - ((0x0000180 - ((ap & 0x0000FF) << 1)) if (ap & 0x000001) == 0
           else ((ap & 0x0000FF) + 0x000001))
    ) & 0xFFFFFF


def _decode_114v3(width: int, height: int,
                  pixels: list[int]) -> tuple[int, int, list[int]] | None:
    if width % 2 or height % 2:
        return None
    out_w, out_h = width // 2, height // 2
    out: list[int] = [0] * (out_w * out_h)
    for y in range(out_h):
        src_row = (2 * y) * width
        src_row2 = src_row + width
        dst_row = y * out_w
        for x in range(out_w):
            a = pixels[src_row + 2 * x]
            bn = pixels[src_row + 2 * x + 1]
            cn = pixels[src_row2 + 2 * x]
            an = pixels[src_row2 + 2 * x + 1]
            ap = _get_ap(a, an)
            out[dst_row + x] = _get_d(_b3_to_b(bn, ap), _c3_to_c(cn, ap))
    return out_w, out_h, out


def _decode_114v2(width: int, height: int,
                  pixels: list[int]) -> tuple[int, int, list[int]] | None:
    if width % 2 or height % 2:
        return None
    out_w, out_h = width // 2, height // 2
    out: list[int] = [0] * (out_w * out_h)
    for y in range(out_h):
        src_row = (2 * y) * width
        src_row2 = src_row + width
        dst_row = y * out_w
        for x in range(out_w):
            a = pixels[src_row + 2 * x]
            bn = pixels[src_row + 2 * x + 1]
            cn = pixels[src_row2 + 2 * x]
            an = pixels[src_row2 + 2 * x + 1]
            ap = _get_ap(a, an)
            out[dst_row + x] = _get_d(_b2_to_b(bn, ap), _c2_to_c(cn, ap))
    return out_w, out_h, out


def _decode_114v1(width: int, height: int,
                  pixels: list[int]) -> tuple[int, int, list[int]] | None:
    if width % 2 or height % 2:
        return None
    out_w, out_h = width // 2, height // 2
    out: list[int] = [0] * (out_w * out_h)
    for y in range(out_h):
        row1 = (2 * y) * width
        row2 = row1 + width
        dst = y * out_w
        for x in range(out_w):
            out[dst + x] = _get_d(
                pixels[row1 + 2 * x + 1],
                pixels[row2 + 2 * x],
            )
    return out_w, out_h, out


def _decode_149(width: int, height: int,
                pixels: list[int]) -> tuple[int, int, list[int]] | None:
    if width % 3 or height % 3:
        return None
    x_size, y_size = width // 3, height // 3
    out_w, out_h = x_size * 2, y_size * 2
    out = [0] * (out_w * out_h)
    for y in range(y_size):
        r0 = (3 * y) * width
        r1 = (3 * y + 1) * width
        r2 = (3 * y + 2) * width
        for x in range(x_size):
            sx = 3 * x
            d1 = _get_d(pixels[r0 + sx], pixels[r0 + sx + 1])
            d2 = _get_d(pixels[r0 + sx + 2], pixels[r1 + sx + 2])
            d3 = _get_d(pixels[r2 + sx + 2], pixels[r2 + sx + 1])
            d4 = _get_d(pixels[r2 + sx], pixels[r1 + sx])
            base = (2 * y) * out_w + 2 * x
            out[base] = d1
            out[base + 1] = d2
            out[base + out_w + 1] = d3
            out[base + out_w] = d4
    return out_w, out_h, out


def _decode_238(width: int, height: int,
                pixels: list[int]) -> tuple[int, int, list[int]] | None:
    if width % 2 or height % 4:
        return None
    x_size, y_size = width // 2, height // 4
    out_w, out_h = x_size, y_size * 3
    out = [0] * (out_w * out_h)
    for y in range(y_size):
        rows = [(4 * y + i) * width for i in range(4)]
        for x in range(x_size):
            sx = 2 * x
            out[(3 * y) * out_w + x] = _get_d(
                pixels[rows[0] + sx], pixels[rows[1] + sx],
            )
            out[(3 * y + 1) * out_w + x] = _get_d(
                pixels[rows[1] + sx + 1], pixels[rows[2] + sx + 1],
            )
            out[(3 * y + 2) * out_w + x] = _get_d(
                pixels[rows[3] + sx], pixels[rows[3] + sx + 1],
            )
    return out_w, out_h, out


def _decode_429(width: int, height: int,
                pixels: list[int]) -> tuple[int, int, list[int]] | None:
    if width % 3 or height % 3:
        return None
    x_size, y_size = width // 3, height // 3
    out_w, out_h = x_size, y_size * 2
    out = [0] * (out_w * out_h)
    for y in range(y_size):
        r0 = (3 * y) * width
        r1 = (3 * y + 1) * width
        r2 = (3 * y + 2) * width
        for x in range(x_size):
            sx = 3 * x
            out[(2 * y) * out_w + x] = _get_d(
                pixels[r0 + sx + 1], pixels[r1 + sx],
            )
            out[(2 * y + 1) * out_w + x] = _get_d(
                pixels[r2 + sx + 1], pixels[r1 + sx + 2],
            )
    return out_w, out_h, out


def _decode_124(width: int, height: int,
                pixels: list[int]) -> tuple[int, int, list[int]] | None:
    if width % 2 or height % 2:
        return None
    x_size, y_size = width // 2, height // 2
    out_w, out_h = x_size, y_size * 2
    out = [0] * (out_w * out_h)
    for y in range(y_size):
        r0 = (2 * y) * width
        r1 = (2 * y + 1) * width
        for x in range(x_size):
            sx = 2 * x
            ad1 = pixels[r0 + sx + 1]
            ad2 = pixels[r1 + sx]
            a1 = pixels[r1 + sx + 1]
            out[(2 * y) * out_w + x] = (
                ((a1 << 4) & 0xC0C0C0) | (ad1 & 0x3F3F3F)
            ) & 0xFFFFFF
            out[(2 * y + 1) * out_w + x] = (
                ((a1 << 6) & 0xC0C0C0) | (ad2 & 0x3F3F3F)
            ) & 0xFFFFFF
    return out_w, out_h, out


def decode_winpng_image(image: Image.Image, *, depth: int = 0) -> list[EmbeddedFile]:
    if depth > MAX_RECURSION:
        return []
    width, height, pixels = _image_pixels(image)

    decoders = (
        _decode_114v3,
        _decode_114v2,
        _decode_114v1,
        _decode_149,
        _decode_238,
        _decode_429,
        _decode_124,
    )
    for decoder in decoders:
        transformed = decoder(width, height, pixels)
        if transformed is None:
            continue
        data_w, data_h, data_pixels = transformed
        files = _decode_raw_container_image(data_w, data_h, data_pixels, depth)
        if files:
            return files

    return _decode_raw_container_image(width, height, pixels, depth)


def decode_winpng_bytes(data: bytes, *, depth: int = 0) -> list[EmbeddedFile]:
    if depth > MAX_RECURSION or not data.startswith(PNG_SIGNATURE):
        return []
    try:
        with Image.open(BytesIO(data)) as image:
            image.load()
            return decode_winpng_image(image, depth=depth)
    except (OSError, UnidentifiedImageError, ValueError):
        return []
