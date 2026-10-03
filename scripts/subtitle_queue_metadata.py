#!/usr/bin/env python3
"""Conservative metadata helpers for the Pi subtitle downloader.

These helpers never decide which subtitle should be played. They only record
what the downloaded filenames/archive layout actually say, classify retry
errors, and keep work-title matching strict enough to avoid cross-series
history backfill.
"""
from __future__ import annotations

import re
import hashlib
import json
import os
import unicodedata
import zipfile
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

SUBTITLE_MEMBER_EXTENSIONS = {".smi", ".srt", ".ass", ".ssa", ".vtt"}
ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar"}
MAX_ZIP_MEMBERS = 5000


def queue_cache_cleanup(item: dict, files: list[Path], target: Path) -> Path:
    """Queue only this successful download; never scan historical subtitles."""
    item_id = str(item.get("id") or "")
    if not item_id or item.get("source") not in ("aniall", "anissia"):
        raise ValueError("Missing cleanup job identity")
    relative = [path.relative_to(target).as_posix() for path in files]
    if not relative or any(".." in Path(path).parts for path in relative):
        raise ValueError("Invalid cleanup file paths")
    fields = ("id", "source", "post_id", "anime_no", "title", "anime_title", "source_url", "episode")
    job = {"id": item_id, "item": {key: item.get(key) for key in fields if key in item}, "files": relative}
    pending = target / ".cache-cleanup-pending"
    pending.mkdir(parents=True, exist_ok=True)
    filename = pending / (hashlib.sha256(item_id.encode()).hexdigest() + ".json")
    temporary = filename.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(job, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(filename)
    return filename


def aniall_folder_identity(item: dict) -> str:
    """Validate the exact post identity; its display folder uses only the title."""
    post_id = str(item.get("post_id") or "")
    if not re.fullmatch(r"[1-9]\d*", post_id):
        raise ValueError("Missing or invalid AniAll post ID")
    if str(item.get("source_url") or "") != f"https://aniall.tistory.com/{post_id}":
        raise ValueError("AniAll post ID and source URL disagree")
    title = str(item.get("title") or "").strip()
    if not title:
        raise ValueError("Missing AniAll title")
    return title


def _episode_int(value) -> int | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not number.is_integer():
        return None
    number = int(number)
    return number if 0 < number < 1000 else None


def normalize_work_identity(value: str) -> str:
    """Normalize a work title without throwing away distinguishing subtitles."""
    text = unicodedata.normalize("NFKC", unquote(str(value or ""))).lower().strip()
    text = re.sub(r"^(?:\s*\[[^\]]+\]\s*)+", " ", text)
    text = text.replace("~", " ")

    # Blog post titles usually append the episode number. Remove the episode
    # suffix, but preserve title subtitles such as
    # "책벌레의 하극상 ~사서가 되기 위해서라면 뭐든지 할 수 있어~".
    text = re.sub(
        r"\s*(?:제|第)?\s*\d{1,3}(?:\.\d+)?\s*(?:화|話|회)\b.*$",
        " ",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\s*\b(?:ep|episode)\s*[.\-_:：#]?\s*0*\d{1,3}(?:\.\d+)?\b.*$",
        " ",
        text,
        flags=re.I,
    )

    # Season labels are allowed to be omitted by subtitle blogs. Only remove a
    # trailing season marker; never remove a descriptive subtitle in the title.
    text = re.sub(
        r"\s*(?:\d+\s*기|season\s*\d+|\d+(?:st|nd|rd|th)\s*season)\s*$",
        " ",
        text,
        flags=re.I,
    )
    text = re.sub(r"(?:[_\-\s]*(?:자막|subtitle|subs?))\s*$", " ", text, flags=re.I)
    return re.sub(r"[^0-9a-z가-힣ぁ-んァ-ヶ一-龯]+", "", text)


def history_title_matches(result_title: str, anime_title: str) -> bool:
    """Require the same normalized work identity for automatic history backfill."""
    result = normalize_work_identity(result_title)
    wanted = normalize_work_identity(anime_title)
    return bool(result and wanted and result == wanted)


def classify_waiting_reason(item: dict) -> str:
    if str(item.get("source_url") or "").strip():
        return ""
    if str(item.get("source") or "") == "anissia":
        raw_episode = str(item.get("episode") or "").strip()
        try:
            episode = float(raw_episode)
        except ValueError:
            episode = None
        if episode is None or episode <= 0:
            return "future_or_unreleased"
    return "no_url_yet"


def classify_failure(error: Exception | str) -> str:
    text = str(error or "").lower()

    if any(token in text for token in (
        "name resolution", "getaddrinfo", "nodename nor servname",
        "temporary failure in name resolution", "failed to resolve",
        "name or service not known",
    )):
        return "dns"
    if any(token in text for token in (
        "sslerror", "certificate_verify_failed", "certificate verify failed",
        "tlsv", "ssl:",
    )):
        return "ssl"
    if any(token in text for token in (
        "read timed out", "connect timed out", "timed out",
        "readtimeout", "connecttimeout",
    )):
        return "timeout"

    http_match = re.search(r"(?<!\d)([45]\d{2})(?!\d)", text)
    if http_match and any(token in text for token in (
        "client error", "server error", "response", "status", "http",
    )):
        return f"http_{http_match.group(1)}"

    if any(token in text for token in (
        "connection refused", "connection reset", "remote disconnected",
        "connection aborted", "max retries exceeded",
    )):
        return "connection"
    if "unsupported attachment type" in text:
        return "unsupported_attachment"
    if "downloaded attachment was empty" in text:
        return "empty_download"
    if "attachment exceeded size limit" in text:
        return "size_limit"
    if any(token in text for token in (
        "post-body not found", "attached file not found",
        "subtitle attachment not found", "data-file-url not found",
        "csrf token not found", "confirmation url not found",
        "no aniall subtitle attachment", "general website subtitle attachment not found",
    )):
        return "parser"
    if any(token in text for token in (
        "file id not found", "invalid url", "confirmation loop exceeded",
    )):
        return "broken_link"
    return "other"


def _season_numbers(text: str) -> tuple[set[int], bool]:
    seasons: set[int] = set()
    multiple = False

    for match in re.finditer(r"\bS0*(\d{1,2})E0*\d{1,3}\b", text, re.I):
        seasons.add(int(match.group(1)))

    for match in re.finditer(r"(?:season|시즌)\s*0*(\d{1,2})", text, re.I):
        seasons.add(int(match.group(1)))
    for match in re.finditer(r"(?<!\d)(\d{1,2})\s*기", text):
        seasons.add(int(match.group(1)))

    if re.search(
        r"(?:season|시즌)\s*\d{1,2}\s*(?:-|~|–|—|to|에서)\s*(?:(?:season|시즌)\s*)?\d{1,2}",
        text,
        re.I,
    ):
        multiple = True
    if re.search(r"(?<!\d)\d{1,2}\s*(?:-|~|–|—|에서)\s*\d{1,2}\s*기", text):
        multiple = True
    if re.search(r"(?<!\d)(?:\d{1,2}\s+){2,}\d{1,2}\s*기", text):
        multiple = True

    if len(seasons) > 1:
        multiple = True
    return seasons, multiple


def read_episode_evidence(text: str, *, filename: bool = False) -> dict:
    raw = unicodedata.normalize("NFKC", unquote(str(text or "")))
    seasons, multiple_seasons = _season_numbers(raw)
    episodes: set[int] = set()
    ranges: set[tuple[int, int]] = set()

    # Remove explicit season ranges before looking for episode ranges so
    # "Season 1-4" is not recorded as episodes 1-4.
    working = re.sub(
        r"(?:season|시즌)\s*\d{1,2}\s*(?:-|~|–|—|to|에서)\s*(?:(?:season|시즌)\s*)?\d{1,2}",
        " ",
        raw,
        flags=re.I,
    )
    working = re.sub(
        r"(?<!\d)\d{1,2}\s*(?:-|~|–|—|에서)\s*\d{1,2}\s*기",
        " ",
        working,
    )
    range_matches = list(re.finditer(
        r"(?<!\d)(\d{1,3})\s*(?:-|~|–|—|to)\s*(\d{1,3})(?:\s*(?:화|話|회))?",
        working,
        re.I,
    ))
    for match in range_matches:
        start, end = int(match.group(1)), int(match.group(2))
        if 0 < start < 1000 and 0 < end < 1000 and start <= end:
            ranges.add((start, end))

    # Mask ranges so "1-12화" does not accidentally become a direct 12화.
    chars = list(working)
    for match in range_matches:
        for index in range(match.start(), match.end()):
            chars[index] = " "
    working = "".join(chars)

    for match in re.finditer(r"\bS0*\d{1,2}E0*(\d{1,3})\b", working, re.I):
        value = int(match.group(1))
        if 0 < value < 1000:
            episodes.add(value)
    for match in re.finditer(
        r"(?<!\d)(?:제|第)?\s*0*(\d{1,3})(?:\.\d+)?\s*(?:화|話|회)\b",
        working,
        re.I,
    ):
        value = int(match.group(1))
        if 0 < value < 1000:
            episodes.add(value)
    for match in re.finditer(
        r"\b(?:ep|episode)\s*[.\-_:：#]?\s*0*(\d{1,3})\b",
        working,
        re.I,
    ):
        value = int(match.group(1))
        if 0 < value < 1000:
            episodes.add(value)

    if filename and not ranges:
        stem = Path(raw).stem
        stem = re.sub(r"(?:\s*\[[^\]]*\])+$", "", stem).strip()
        match = re.search(
            r"(?:^|[\s._-])0*(\d{1,3})(?:v\d+|tv|ns)?$",
            stem,
            re.I,
        )
        if match:
            value = int(match.group(1))
            if 0 < value < 1000:
                episodes.add(value)

    return {
        "episodes": sorted(episodes),
        "ranges": [list(value) for value in sorted(ranges)],
        "seasons": sorted(seasons),
        "multiple_seasons": multiple_seasons,
    }


def _merge_evidence(rows: list[dict]) -> dict:
    episodes: set[int] = set()
    ranges: set[tuple[int, int]] = set()
    seasons: set[int] = set()
    multiple_seasons = False
    for row in rows:
        episodes.update(int(value) for value in row.get("episodes", []))
        for value in row.get("ranges", []):
            if isinstance(value, (list, tuple)) and len(value) == 2:
                ranges.add((int(value[0]), int(value[1])))
        seasons.update(int(value) for value in row.get("seasons", []))
        multiple_seasons = multiple_seasons or bool(row.get("multiple_seasons"))
    if len(seasons) > 1:
        multiple_seasons = True
    return {
        "episodes": sorted(episodes),
        "ranges": [list(value) for value in sorted(ranges)],
        "seasons": sorted(seasons),
        "multiple_seasons": multiple_seasons,
    }


def _file_record(path: Path, target: Path) -> dict:
    direct = read_episode_evidence(path.name, filename=True)
    record = {
        "path": str(path.relative_to(target)),
        "name": path.name,
        "size_bytes": path.stat().st_size,
        "direct_evidence": direct,
    }

    suffix = path.suffix.lower()
    if suffix != ".zip":
        if suffix in ARCHIVE_EXTENSIONS:
            record["archive"] = {
                "format": suffix.removeprefix("."),
                "inspectable": False,
            }
        return record

    archive_rows: list[dict] = []
    member_count = 0
    subtitle_member_count = 0
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            member_count = len(names)
            for name in names[:MAX_ZIP_MEMBERS]:
                if name.endswith("/"):
                    continue
                if Path(name).suffix.lower() not in SUBTITLE_MEMBER_EXTENSIONS:
                    continue
                subtitle_member_count += 1
                archive_rows.append(read_episode_evidence(name, filename=True))
        record["archive"] = {
            "format": "zip",
            "inspectable": True,
            "member_count": member_count,
            "inspected_members": min(member_count, MAX_ZIP_MEMBERS),
            "subtitle_member_count": subtitle_member_count,
            "evidence": _merge_evidence(archive_rows),
        }
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        record["archive"] = {
            "format": "zip",
            "inspectable": False,
            "error": str(exc),
        }
    return record


def build_download_metadata(item: dict, files: list[Path], target: Path) -> dict:
    records = [_file_record(path, target) for path in files]

    direct_rows = [record["direct_evidence"] for record in records]
    archive_rows = [
        record["archive"]["evidence"]
        for record in records
        if isinstance(record.get("archive"), dict)
        and record["archive"].get("inspectable")
        and isinstance(record["archive"].get("evidence"), dict)
    ]
    direct = _merge_evidence(direct_rows)
    archive = _merge_evidence(archive_rows)
    coverage = _merge_evidence([direct, archive])

    requested_episode = _episode_int(item.get("episode"))
    source = str(item.get("source") or "")
    status = "not_applicable" if source != "anissia" else "unverified"
    notes: list[str] = []

    if source == "anissia" and requested_episode is not None:
        direct_eps = set(direct["episodes"])
        archive_eps = set(archive["episodes"])
        if requested_episode in direct_eps or requested_episode in archive_eps:
            status = "verified"
        elif direct_eps:
            status = "ambiguous"
            notes.append("downloaded filename episode conflicts with requested episode")
        elif archive_eps:
            # A multi-file archive may use absolute source numbering while
            # Anissia exposes season-relative numbering. Preserve the evidence
            # without guessing an offset.
            status = "unverified"
            notes.append("archive episode numbering does not directly match requested episode")
        elif coverage["ranges"]:
            notes.append("range-only coverage cannot verify one exact episode")

    if coverage["multiple_seasons"]:
        notes.append("download contains evidence for multiple seasons")

    work_title = str(item.get("anime_title") or item.get("title") or "")
    return {
        "files": records,
        "coverage": coverage,
        "verification": {
            "status": status,
            "requested_episode": requested_episode,
            "work_title": work_title,
            "work_key": normalize_work_identity(work_title),
            "notes": notes,
        },
    }


def reason_counts(entries: dict) -> dict[str, int]:
    counter = Counter(
        str(value.get("reason") or "legacy")
        for value in entries.values()
        if isinstance(value, dict)
    )
    return dict(sorted(counter.items()))
