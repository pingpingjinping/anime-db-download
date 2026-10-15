#!/usr/bin/env python3
"""Download subtitle queue Releases on the Pi.

GitHub discovers candidate records only. This script opens only the queued
source pages and downloads their subtitle attachments from the Pi/home IP.
Failed items remain in local retry state and are retried on later runs.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
from html import unescape
import hmac
import json
import mimetypes
import os
import re
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from anissia_caption_policy import ANISSIA_CAPTION, caption_item

from subtitle_work_storage import safe_name, work_directory, register_sources, preserve_download
from winpng_decode import decode_winpng_bytes
from tistory_winpng_jamaker import convert_tistory_jmks

from subtitle_queue_metadata import (
    aniall_folder_identity,
    build_download_metadata,
    classify_failure,
    classify_waiting_reason,
    history_title_matches as strict_history_title_matches,
    normalize_work_identity,
    read_episode_evidence,
    reason_counts,
    queue_cache_cleanup,
)

REPO = "pingpingjinping/anime-db-download"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases"
ANISSIA_ANIME_INFO = "https://api.anissia.net/anime/animeNo/{anime_no}"
TAG_RE = re.compile(r"^subs-queue-\d{4}-\d{2}-\d{2}-\d+(?:-\d+)?$")
QUEUE_NAME = "subtitle-queue.json"
DEFAULT_TARGET = Path("/home/pi/streaming-project/subtitle-data")
STATE_FILE = Path.home() / ".local/state/anime-db-download/subtitle-queue-state.json"
FAIL_LOG = Path.home() / ".local/state/anime-db-download/subtitle-download-failed.jsonl"
RETIRED_LOG = Path.home() / ".local/state/anime-db-download/subtitle-download-retired.jsonl"
KST = ZoneInfo("Asia/Seoul")
RETRY_MONTHS = 3
BACKFILL_BASELINE_DATE = dt.date(2026, 9, 28)
HISTORY_SEARCH_MAX_PAGES = 4
HISTORY_SEARCH_MAX_RESULTS = 60
ALLOWED_EXTENSIONS = {".zip", ".7z", ".rar", ".smi", ".srt", ".ass", ".ssa", ".vtt"}
WINPNG_EXTENSIONS = ALLOWED_EXTENSIONS | {".jmk"}
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)



def clean_attachment_name(value: str) -> str:
    name = (value or "").strip()
    name = re.sub(
        r"\s+\d+(?:\.\d+)?\s*(?:B|KB|MB|GB|KIB|MIB|GIB)\s*$",
        "",
        name,
        flags=re.I,
    ).strip()
    try:
        repaired = name.encode("ISO-8859-1").decode("UTF-8")
        if repaired:
            name = repaired
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return name


def github_headers(token: str, accept: str = "application/vnd.github+json") -> dict[str, str]:
    return {
        "Accept": accept,
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "anime-db-download-pi/1.0",
    }


def github_json(url: str, token: str):
    req = urllib.request.Request(url, headers=github_headers(token))
    with urllib.request.urlopen(req, timeout=45) as response:
        return json.load(response)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, response_headers, newurl):
        return None


def download_github_asset(asset: dict, token: str, destination: Path) -> str:
    req = urllib.request.Request(asset["url"], headers=github_headers(token, "application/octet-stream"))
    opener = urllib.request.build_opener(NoRedirect)
    try:
        response = opener.open(req, timeout=60)
    except urllib.error.HTTPError as error:
        if error.code not in (301, 302, 303, 307, 308):
            raise
        location = error.headers.get("Location") or ""
        parsed = urllib.parse.urlparse(location)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not (
            host == "github.com"
            or host.endswith(".githubusercontent.com")
            or host.endswith(".github.com")
        ):
            raise RuntimeError(f"Unexpected GitHub redirect host: {host}")
        response = urllib.request.urlopen(location, timeout=90)

    digest = hashlib.sha256()
    with response, destination.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
            output.write(chunk)
    actual = digest.hexdigest()
    expected = str(asset.get("digest") or "")
    if expected.startswith("sha256:"):
        expected = expected.removeprefix("sha256:")
        if not hmac.compare_digest(actual, expected):
            destination.unlink(missing_ok=True)
            raise RuntimeError(f"SHA-256 mismatch for {asset.get('name')}")
    return actual


def list_queue_releases(token: str) -> list[dict]:
    rows_out: list[dict] = []
    page = 1
    while True:
        rows = github_json(f"{RELEASES_API}?per_page=100&page={page}", token)
        if not isinstance(rows, list):
            raise RuntimeError("Unexpected GitHub releases response")
        rows_out.extend(row for row in rows if TAG_RE.fullmatch(str(row.get("tag_name") or "")))
        if len(rows) < 100:
            break
        page += 1
    return sorted(rows_out, key=lambda row: (row.get("published_at") or "", row.get("tag_name") or ""))


def queue_asset(release: dict) -> dict:
    matches = [asset for asset in release.get("assets", []) if asset.get("name") == QUEUE_NAME]
    if len(matches) != 1:
        raise RuntimeError(f"{release.get('tag_name')} must contain exactly one {QUEUE_NAME}")
    return matches[0]


def load_state(path: Path) -> dict:
    if not path.exists():
        return {
            "version": 1,
            "processed_releases": [],
            "success": {},
            "retry": {},
            "waiting_url": {},
            "anissia_waiting_url_checks": {},
            "erulabo_pending": {},
            "retired": {},
            "backfill_done": {},
            "drive_folders": {},
        }
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("version") != 1:
        raise RuntimeError(f"Unsupported Pi subtitle state: {path}")
    state.setdefault("processed_releases", [])
    state.setdefault("success", {})
    state.setdefault("retry", {})
    state.setdefault("waiting_url", {})
    state.setdefault("anissia_waiting_url_checks", {})
    state.setdefault("erulabo_pending", {})
    state.setdefault("retired", {})
    state.setdefault("backfill_done", {})
    state.setdefault("drive_folders", {})
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def current_month_key() -> str:
    return dt.datetime.now(KST).strftime("%Y-%m")


def month_distance(first_month: str, current_month: str) -> int:
    try:
        first_year, first_mon = (int(part) for part in first_month.split("-", 1))
        current_year, current_mon = (int(part) for part in current_month.split("-", 1))
    except (TypeError, ValueError):
        return 0
    return (current_year * 12 + current_mon) - (first_year * 12 + first_mon)


def first_failed_month(retry: dict) -> str:
    value = str(retry.get("first_failed_month") or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}", value):
        return value
    # Migration fallback for any pre-policy retry state.
    last_attempt = str(retry.get("last_attempt") or "")
    match = re.match(r"(\d{4}-\d{2})", last_attempt)
    return match.group(1) if match else current_month_key()


def source_url(item: dict) -> str:
    return str(item.get("source_url") or "").strip()


def is_erulabo_url(value: str) -> bool:
    host = (urlparse(value or "").hostname or "").lower().rstrip(".")
    return host == "erulabo.com" or host.endswith(".erulabo.com")


def move_to_erulabo_pending(state: dict, item_id: str, item: dict) -> None:
    now = dt.datetime.now(KST).isoformat(timespec="seconds")
    previous = state["erulabo_pending"].get(item_id) or {}
    state["erulabo_pending"][item_id] = {
        "item": item,
        "queued_at": previous.get("queued_at") or now,
        "last_seen": now,
        "reason": "erulabo_turnstile",
    }
    state["retry"].pop(item_id, None)
    state["waiting_url"].pop(item_id, None)


def waiting_identity(item: dict) -> tuple[str, ...]:
    source = str(item.get("source") or "")
    if source == "anissia":
        return (
            source,
            str(item.get("anime_no") or ""),
            str(item.get("episode") or ""),
            str(item.get("creator") or ""),
        )
    if source == "aniall":
        return (source, str(item.get("post_id") or ""))
    return (source, str(item.get("id") or ""))


def remove_matching_waiting(state: dict, item: dict) -> list[str]:
    identity = waiting_identity(item)
    removed: list[str] = []
    for waiting_id, waiting in list(state["waiting_url"].items()):
        waiting_item = waiting.get("item") or {}
        if waiting_identity(waiting_item) != identity:
            continue
        state["waiting_url"].pop(waiting_id, None)
        removed.append(waiting_id)
    return removed


def move_to_waiting_url(state: dict, item_id: str, item: dict) -> None:
    now = dt.datetime.now(KST).isoformat(timespec="seconds")
    previous = state["waiting_url"].get(item_id) or {}

    # The discovery item id includes the source URL. If a blank-URL record is
    # emitted again with other metadata changes, keep only the newest waiting
    # record for the same Anissia work/episode/creator identity.
    for waiting_id in remove_matching_waiting(state, item):
        if waiting_id == item_id:
            continue

    state["waiting_url"][item_id] = {
        "item": item,
        "first_seen": previous.get("first_seen") or now,
        "last_seen": now,
        "reason": classify_waiting_reason(item),
    }
    state["retry"].pop(item_id, None)


def migrate_empty_url_retries(state: dict) -> int:
    moved = 0
    for item_id, retry in list(state["retry"].items()):
        item = retry.get("item") or {}
        if source_url(item):
            continue
        move_to_waiting_url(state, item_id, item)
        moved += 1
    return moved


def refresh_waiting_urls(session: requests.Session, state: dict, state_path: Path,
                         month: str | None = None) -> list[dict]:
    """Check only saved Anissia URL waiters once per work/KST calendar month.

    Persist a newly available URL before attempting its download. Keeping it
    in waiting_url until process_item consumes it also recovers a crash between
    API discovery and the download without another API request.
    """
    month = month or current_month_key()
    checks = state.setdefault("anissia_waiting_url_checks", {})
    groups: dict[str, list[dict]] = {}
    for waiting in state["waiting_url"].values():
        item = waiting.get("item") or {}
        anime_no = str(item.get("anime_no") or "")
        if item.get("source") != "anissia" or source_url(item) or not anime_no:
            continue
        if (checks.get(anime_no) or {}).get("month") == month:
            continue
        groups.setdefault(anime_no, []).append(waiting)

    for anime_no, waiters in groups.items():
        check = {"month": month, "attempted_at": dt.datetime.now(KST).isoformat(timespec="seconds")}
        try:
            payload = request(session, ANISSIA_CAPTION.format(anime_no=anime_no)).json()
            captions = payload.get("data") if isinstance(payload, dict) and "data" in payload else payload
            if not isinstance(captions, list) or not all(isinstance(row, dict) for row in captions):
                raise RuntimeError("Unexpected Anissia caption response")
            available: dict[tuple[str, ...], dict[str, dict]] = {}
            for caption in captions:
                candidate = caption_item(anime_no, "", caption)
                if not source_url(candidate):
                    continue
                available.setdefault(waiting_identity(candidate), {})[source_url(candidate)] = candidate
            resolved = 0
            ambiguous = 0
            for waiting in waiters:
                old = waiting["item"]
                urls = available.get(waiting_identity(old), {})
                if len(urls) != 1:
                    ambiguous += int(len(urls) > 1)
                    continue
                candidate = dict(next(iter(urls.values())))
                candidate["anime_title"] = old.get("anime_title") or anime_no
                candidate["handler"] = classify_source_url(source_url(candidate))
                waiting["item"] = candidate
                waiting["url_available_at"] = check["attempted_at"]
                resolved += 1
            check.update(resolved=resolved, ambiguous=ambiguous)
            print(f"[=] Anissia URL check {anime_no}: {resolved} available, {ambiguous} ambiguous")
        except Exception as exc:
            # An API failure leaves URL waiters intact and outside the download
            # retry/retirement policy. HTTP retries happen inside request(); the
            # next scheduled metadata check is next month.
            check["error"] = str(exc)
            print(f"[-] Anissia URL check {anime_no}: {exc}")
        checks[anime_no] = check
        save_state(state_path, state)

    return [waiting["item"] for waiting in state["waiting_url"].values()
            if (waiting.get("item") or {}).get("source") == "anissia"
            and source_url(waiting["item"])]


def append_retired(item_id: str, retry: dict, reason: str) -> None:
    RETIRED_LOG.parent.mkdir(parents=True, exist_ok=True)
    item = retry.get("item") or {}
    row = {
        "id": item_id,
        "source": item.get("source"),
        "source_url": item.get("source_url"),
        "first_failed_month": first_failed_month(retry),
        "last_error": retry.get("last_error"),
        "last_reason": retry.get("reason"),
        "retired_at": dt.datetime.now(KST).isoformat(timespec="seconds"),
        "reason": reason,
    }
    with RETIRED_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_failure(item: dict, error: Exception | str, reason: str) -> None:
    FAIL_LOG.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "id": item.get("id"),
        "source": item.get("source"),
        "source_url": item.get("source_url"),
        "reason": reason,
        "error": str(error),
        "attempted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    with FAIL_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def request(session: requests.Session, url: str, *, referer: str | None = None,
            stream: bool = False, timeout: tuple[int, int] = (15, 60)) -> requests.Response:
    headers = {
        "User-Agent": UA,
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    if referer:
        headers["Referer"] = referer
    last_error = None
    for attempt, pause in enumerate((0, 5, 15)):
        if pause:
            time.sleep(pause)
        try:
            response = session.get(
                url,
                headers=headers,
                timeout=timeout,
                stream=stream,
                allow_redirects=True,
            )
            if response.status_code in (429, 502, 503, 504) and attempt < 2:
                response.close()
                continue
            response.raise_for_status()
            return response
        except Exception as exc:
            last_error = exc
            if attempt >= 2:
                break
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def classify_source_url(url: str) -> str:
    """Route URLs the same way as GUI-for-SMI-Auto-Downloader."""
    value = unquote(url or "").strip().lower()
    if not value:
        return "empty"
    if "naver" in value:
        return "naver"
    if "blogspot" in value:
        return "blogspot"
    if "tistory" in value:
        return "tistory"
    return "website"


def backfill_identity(item: dict) -> str:
    """Stable one-time key for one Anissia work + subtitle creator."""
    raw = "\n".join([
        "anissia",
        str(item.get("anime_no") or ""),
        str(item.get("creator") or ""),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def episode_number(value) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def episode_label(value) -> str:
    number = episode_number(value)
    if number is None:
        return str(value or "").strip()
    if number.is_integer():
        return str(int(number))
    return ("%s" % number).rstrip("0").rstrip(".")


def extract_episode_from_title(text: str) -> float | None:
    text = str(text or "")
    patterns = (
        r"(?<!\d)(\d{1,3}(?:\.\d+)?)\s*(?:화|話|회)",
        r"\b(?:ep|episode)\s*[.\-_:：#]?\s*0*(\d{1,3}(?:\.\d+)?)\b",
        r"(?:제|第)\s*(\d{1,3}(?:\.\d+)?)\s*(?:화|話|회)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return episode_number(match.group(1))
    return None


def normalize_history_title(value: str) -> str:
    return normalize_work_identity(value)


def history_title_matches(result_title: str, anime_title: str) -> bool:
    return strict_history_title_matches(result_title, anime_title)

def history_candidate(url: str, title: str, anime_title: str,
                      latest_episode: float | None) -> dict | None:
    episode = extract_episode_from_title(title)
    if episode is None or episode <= 0:
        return None
    if latest_episode is not None and episode >= latest_episode:
        return None
    if not history_title_matches(title, anime_title):
        return None
    return {
        "url": url,
        "title": title,
        "episode": episode_label(episode),
    }


def add_history_candidate(out: list[dict], seen: set[str], url: str, title: str,
                          anime_title: str, latest_episode: float | None) -> None:
    if not url or url in seen or len(out) >= HISTORY_SEARCH_MAX_RESULTS:
        return
    candidate = history_candidate(url, title, anime_title, latest_episode)
    if candidate is None:
        return
    seen.add(url)
    out.append(candidate)


def search_tistory_history(session: requests.Session, source_url: str, anime_title: str,
                            latest_episode: float | None) -> list[dict]:
    parsed = urlparse(source_url)
    host = (parsed.hostname or "").lower()
    if not host:
        return []

    out: list[dict] = []
    seen: set[str] = set()
    query = quote(anime_title, safe="")
    for page in range(1, HISTORY_SEARCH_MAX_PAGES + 1):
        search_url = f"{parsed.scheme or 'https'}://{host}/search/{query}?page={page}"
        response = request(session, search_url)
        before = len(out)
        soup = BeautifulSoup(response.text, "html.parser")
        for anchor in soup.find_all("a", href=True):
            href = urljoin(search_url, anchor.get("href", ""))
            target = urlparse(href)
            if (target.hostname or "").lower() != host:
                continue
            if re.fullmatch(r"/\d+/?", target.path or "") is None:
                continue
            title = " ".join(anchor.stripped_strings).strip()
            add_history_candidate(out, seen, href, title, anime_title, latest_episode)
        if len(out) == before and page > 1:
            break
    return out


def search_blogspot_history(session: requests.Session, source_url: str, anime_title: str,
                             latest_episode: float | None) -> list[dict]:
    parsed = urlparse(source_url)
    host = (parsed.hostname or "").lower()
    if not host:
        return []

    search_url = (
        f"{parsed.scheme or 'https'}://{host}/search?"
        + urllib.parse.urlencode({"q": anime_title, "max-results": 50})
    )
    response = request(session, search_url)
    out: list[dict] = []
    seen: set[str] = set()
    soup = BeautifulSoup(response.text, "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = urljoin(search_url, anchor.get("href", ""))
        target = urlparse(href)
        if (target.hostname or "").lower() != host:
            continue
        if re.search(r"/\d{4}/\d{2}/.+\.html$", target.path or "", re.I) is None:
            continue
        title = " ".join(anchor.stripped_strings).strip()
        add_history_candidate(out, seen, href, title, anime_title, latest_episode)
    return out


def naver_post_ids(url: str) -> tuple[str | None, str | None]:
    decoded = unquote(url or "")
    blog_match = re.search(r"[?&]blogId=([^&#]+)", decoded, re.I)
    log_match = re.search(r"[?&]logNo=(\d+)", decoded, re.I)
    if blog_match and log_match:
        return blog_match.group(1), log_match.group(1)
    match = re.search(
        r"(?:https?://)?(?:m\.)?blog\.naver\.com/([^/?#]+)/(\d+)",
        decoded,
        re.I,
    )
    if match:
        return match.group(1), match.group(2)
    return None, None


def naver_history_post_url(blog_id: str, href: str) -> str | None:
    absolute = urljoin("https://m.blog.naver.com", href or "")
    parsed = urlparse(absolute)
    match = re.search(r"/([^/?#]+)/(\d+)", parsed.path or "")
    if match:
        return f"https://blog.naver.com/{match.group(1)}/{match.group(2)}"
    params = parse_qs(parsed.query)
    log_no = (params.get("logNo") or params.get("logno") or [""])[0]
    item_blog_id = (params.get("blogId") or params.get("blogid") or [blog_id])[0]
    if item_blog_id and str(log_no).isdigit():
        return f"https://blog.naver.com/{item_blog_id}/{log_no}"
    return None


def search_naver_history(session: requests.Session, source_url: str, anime_title: str,
                         latest_episode: float | None) -> list[dict]:
    blog_id, _ = naver_post_ids(source_url)
    if not blog_id:
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for page in range(1, HISTORY_SEARCH_MAX_PAGES + 1):
        search_url = (
            "https://m.blog.naver.com/PostSearchList.naver?"
            + urllib.parse.urlencode({
                "blogId": blog_id,
                "searchText": anime_title,
                "orderType": "sim",
                "currentPage": page,
                "countPerPage": 30,
            })
        )
        response = request(session, search_url)
        before = len(out)
        soup = BeautifulSoup(response.text, "html.parser")
        for anchor in soup.find_all("a", href=True):
            href = naver_history_post_url(blog_id, anchor.get("href", ""))
            if not href:
                continue
            title = " ".join(anchor.stripped_strings).strip()
            add_history_candidate(out, seen, href, title, anime_title, latest_episode)
        if len(out) == before and page > 1:
            break
    return out


def find_history_posts(session: requests.Session, item: dict) -> list[dict]:
    source = source_url(item)
    latest_episode = episode_number(item.get("episode"))
    anime_title = str(item.get("anime_title") or "")
    route = item.get("handler") or classify_source_url(source)
    if latest_episode is None or latest_episode <= 1 or not source or not anime_title:
        return []
    if route == "naver":
        return search_naver_history(session, source, anime_title, latest_episode)
    if route == "blogspot":
        return search_blogspot_history(session, source, anime_title, latest_episode)
    if route == "tistory":
        return search_tistory_history(session, source, anime_title, latest_episode)
    return []


def historical_destination(item: dict, target: Path, episode: str) -> Path:
    return (
        work_directory(item, target)
        / safe_name(f"{episode or '0'}화")
        / safe_name(item.get("creator") or "unknown")
    )


def parse_item_date(value: str) -> dt.date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text).astimezone(KST).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = dt.datetime.strptime(text, fmt)
            return parsed.date()
        except ValueError:
            continue
    return None


def mark_baseline_backfill_done(session: requests.Session, state: dict,
                                state_path: Path, item: dict) -> bool:
    """Treat works already covered by the PC backfill baseline as complete."""
    key = backfill_identity(item)
    if key in state["backfill_done"]:
        return True

    item_date = parse_item_date(str(item.get("updated_at") or ""))
    reason = ""
    start_date = None

    if item_date is not None and item_date <= BACKFILL_BASELINE_DATE:
        reason = "pc_backfill_baseline_item_date"
    else:
        anime_no = str(item.get("anime_no") or "")
        if anime_no:
            try:
                response = request(
                    session,
                    ANISSIA_ANIME_INFO.format(anime_no=quote(anime_no)),
                )
                payload = response.json()
                data = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(data, dict):
                    start_date = parse_item_date(str(data.get("startDate") or ""))
                    if start_date is not None and start_date <= BACKFILL_BASELINE_DATE:
                        reason = "pc_backfill_baseline_anime_start_date"
            except Exception as exc:
                print(
                    f"[-] backfill baseline check {anime_no} "
                    f"{item.get('creator')}: {exc}"
                )

    if not reason:
        return False

    state["backfill_done"][key] = {
        "anime_no": str(item.get("anime_no") or ""),
        "anime_title": str(item.get("anime_title") or ""),
        "creator": str(item.get("creator") or ""),
        "source_host": (urlparse(source_url(item)).hostname or "").lower(),
        "baseline_covered": True,
        "baseline_date": BACKFILL_BASELINE_DATE.isoformat(),
        "item_updated_at": str(item.get("updated_at") or ""),
        "anime_start_date": start_date.isoformat() if start_date else "",
        "reason": reason,
        "completed_at": dt.datetime.now(KST).isoformat(timespec="seconds"),
    }
    save_state(state_path, state)
    print(
        f"[=] backfill baseline-covered {item.get('anime_no')} "
        f"{item.get('creator')}: through {BACKFILL_BASELINE_DATE.isoformat()}"
    )
    return True


def maybe_backfill_history(session: requests.Session, state: dict, state_path: Path,
                           item: dict, target: Path) -> None:
    if item.get("source") != "anissia":
        return

    latest_episode = episode_number(item.get("episode"))
    route = item.get("handler") or classify_source_url(source_url(item))
    if latest_episode is None or latest_episode <= 1:
        return
    if route not in {"naver", "blogspot", "tistory"}:
        return

    key = backfill_identity(item)
    if key in state["backfill_done"]:
        return

    # The PC backfill run covers all works already underway through the
    # configured baseline. For items after that date, use the Anissia work
    # start date so an ongoing series does not trigger a redundant archive
    # crawl in October. Only works that started after the baseline remain
    # eligible for one-time Pi backfill.
    if mark_baseline_backfill_done(session, state, state_path, item):
        return

    # If discovery/search itself fails, do not mark this creator/work as done.
    # A later monthly Pi run can retry the one-time search.
    rows = find_history_posts(session, item)
    downloaded = 0
    errors: list[str] = []
    for row in sorted(rows, key=lambda value: episode_number(value.get("episode")) or 0):
        destination = historical_destination(item, target, str(row.get("episode") or ""))
        try:
            files = download_source_page(
                session,
                str(row.get("url") or ""),
                destination,
                classify_source_url(str(row.get("url") or "")),
            )
            register_sources({**item, "episode": row.get("episode"), "source_url": row.get("url")}, files, target)
            downloaded += len(files)
            print(
                f"[+] backfill {item.get('anime_no')} {row.get('episode')}화 "
                f"{item.get('creator')}: {len(files)} file(s)"
            )
        except Exception as exc:
            errors.append(f"{row.get('episode')}화 {row.get('url')}: {exc}")
            print(
                f"[-] backfill {item.get('anime_no')} {row.get('episode')}화 "
                f"{item.get('creator')}: {exc}"
            )

    state["backfill_done"][key] = {
        "anime_no": str(item.get("anime_no") or ""),
        "anime_title": str(item.get("anime_title") or ""),
        "creator": str(item.get("creator") or ""),
        "source_host": (urlparse(source_url(item)).hostname or "").lower(),
        "searched_from_episode": str(item.get("episode") or ""),
        "found_posts": len(rows),
        "downloaded_files": downloaded,
        "failed_posts": len(errors),
        "completed_at": dt.datetime.now(KST).isoformat(timespec="seconds"),
        "errors": errors[:10],
    }
    save_state(state_path, state)
    print(
        f"[=] backfill done {item.get('anime_no')} {item.get('creator')}: "
        f"{len(rows)} post(s), {downloaded} file(s), {len(errors)} failed"
    )



def google_drive_folder_id(url: str) -> str | None:
    """Recognize folder URLs separately from single-file and uc?id= URLs."""
    parsed = urlparse(unescape(url or ""))
    if (parsed.hostname or "").lower() != "drive.google.com":
        return None
    match = re.fullmatch(r"/drive/folders/([A-Za-z0-9_-]{10,})/?", parsed.path)
    return match.group(1) if match else None


def list_google_drive_folder(session: requests.Session, folder_id: str) -> list[tuple[str, str]]:
    """List public shared files, including small nested folders, without Drive credentials.

    The embedded view only exposes a limited set of children. Refuse listings
    with 50+ children rather than silently declaring an incomplete sync done.
    """
    seen_folders: set[str] = set()
    seen_files: set[str] = set()
    files: list[tuple[str, str]] = []

    def visit(current_id: str, depth: int) -> None:
        if current_id in seen_folders:
            return
        if depth > 4:
            raise RuntimeError("Google Drive folder nesting exceeds 4 levels")
        seen_folders.add(current_id)
        url = f"https://drive.google.com/embeddedfolderview?id={quote(current_id)}"
        response = request(session, url)
        try:
            soup = BeautifulSoup(response.text, "html.parser")
        finally:
            response.close()
        children: list[tuple[str, str, str]] = []
        seen_children: set[tuple[str, str]] = set()
        for anchor in soup.find_all("a", href=True):
            link = urljoin("https://drive.google.com", unescape(anchor["href"]))
            name = anchor.get_text(" ", strip=True)
            file_id = google_drive_file_id(link)
            subfolder_id = google_drive_folder_id(link)
            kind, child_id = ("file", file_id) if file_id else ("folder", subfolder_id)
            if not child_id or not name or (kind, child_id) in seen_children:
                continue
            seen_children.add((kind, child_id))
            children.append((kind, child_id, name))
        if not children:
            raise RuntimeError(
                "Google Drive folder has no visible entries; check anyone-with-link access "
                f"and embedded listing: {current_id}"
            )
        if len(children) >= 50:
            raise RuntimeError(
                f"Google Drive folder {current_id} lists at least 50 entries; "
                "embedded view may be incomplete"
            )
        for kind, child_id, name in children:
            if kind == "folder":
                visit(child_id, depth + 1)
            elif child_id not in seen_files:
                seen_files.add(child_id)
                files.append((child_id, name))
                if len(files) > 250:
                    raise RuntimeError("Google Drive folder exceeds 250 files")

    visit(folder_id, 0)
    return files


def drive_folder_episode(name: str, fallback: str) -> str:
    """Use explicit file evidence; leave ambiguous episodes in the post folder."""
    stem = Path(name).stem
    if re.search(r"(?<![A-Za-z])(?:OVA|OAD)(?![A-Za-z])", stem, flags=re.I):
        return "OVA"
    evidence = read_episode_evidence(name, filename=True)
    episodes = evidence.get("episodes", [])
    if len(episodes) == 1 and not evidence.get("ranges"):
        return str(episodes[0])
    # Common BD names: "Series Name - 05 (BD 1280x720).smi".
    if not episodes and not evidence.get("ranges"):
        match = re.search(r"\s[-–—]\s+0*(\d{1,3})(?=\s*(?:\(|\[|$))", stem)
        if match and 0 < int(match.group(1)) < 1000:
            return str(int(match.group(1)))
    return str(fallback or "0")


def download_anissia_drive_folder(session: requests.Session, item: dict, target: Path,
                                  state: dict, state_path: Path) -> list[Path]:
    folder_id = google_drive_folder_id(source_url(item))
    if not folder_id:
        raise ValueError("Not a Google Drive folder")
    entries = list_google_drive_folder(session, folder_id)
    records = state.setdefault("drive_folders", {}).setdefault(item["id"], {})
    if records.get("folder_id") != folder_id:
        records.clear()
        records["folder_id"] = folder_id
    known = records.setdefault("files", {})
    root = work_directory(item, target)
    creator = safe_name(item.get("creator") or "unknown")
    files: list[Path] = []
    errors: list[str] = []
    supported = 0
    for file_id, original_name in entries:
        name = safe_name(original_name, 180)
        if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
            continue
        supported += 1
        previous = known.get(file_id) or {}
        relative = str(previous.get("path") or "")
        parts = Path(relative).parts
        file_path = target / relative
        if (previous.get("name") == original_name
                and len(parts) > 2 and parts[0] == "anissia_subtitles"
                and ".." not in parts and file_path.is_file()
                and not file_path.is_symlink()
                and hashlib.sha256(file_path.read_bytes()).hexdigest() == previous.get("sha256")):
            files.append(file_path)
            continue

        episode = drive_folder_episode(name, str(item.get("episode") or "0"))
        destination = root / ("OVA" if episode == "OVA" else safe_name(f"{episode}화")) / creator
        if destination.is_symlink() or destination.parent.is_symlink():
            raise RuntimeError("Google Drive subtitle destination cannot be a symlink")
        try:
            downloaded = download_google_drive(
                session, f"https://drive.google.com/file/d/{file_id}/view",
                destination, suggested_name=name,
            )
            file_item = dict(item)
            file_item["episode"] = episode
            register_sources(file_item, [downloaded], target)
            known[file_id] = {
                "name": original_name,
                "path": downloaded.relative_to(target).as_posix(),
                "sha256": hashlib.sha256(downloaded.read_bytes()).hexdigest(),
            }
            save_state(state_path, state)  # Commit each good file before the next transfer.
            files.append(downloaded)
        except Exception as exc:
            errors.append(f"{name} ({file_id}): {exc}")
    if not supported:
        raise RuntimeError("Google Drive folder contains no supported subtitle files")
    if errors:
        raise RuntimeError(
            f"Google Drive folder incomplete ({len(files)}/{supported} files): "
            + " | ".join(errors[:5])
        )
    return files


def google_drive_file_id(url: str) -> str | None:
    parsed = urlparse(unescape(url or ""))
    host = (parsed.hostname or "").lower()
    if host not in {"drive.google.com", "docs.google.com", "drive.usercontent.google.com"}:
        return None
    if google_drive_folder_id(url):
        return None
    match = re.search(r"/file/d/([^/]+)", parsed.path)
    if match:
        return match.group(1)
    values = parse_qs(parsed.query).get("id") or []
    return values[0] if values else None


def google_drive_download_url(url: str) -> str | None:
    file_id = google_drive_file_id(url)
    return f"https://drive.google.com/uc?id={quote(file_id)}" if file_id else None


def google_drive_confirmation_url(html_text: str) -> str:
    match = re.search(r'href="(/uc\?export=download[^"]+)', html_text)
    if match:
        return urljoin("https://docs.google.com", unescape(match.group(1)))

    soup = BeautifulSoup(html_text, "html.parser")
    form = soup.select_one("#download-form")
    if form and form.get("action"):
        action = unescape(form["action"])
        parsed = urlparse(action)
        params = parse_qs(parsed.query)
        for hidden in form.find_all("input", attrs={"type": "hidden"}):
            if hidden.get("name"):
                params[hidden["name"]] = [hidden.get("value", "")]
        query = urllib.parse.urlencode(params, doseq=True)
        return urllib.parse.urlunparse(parsed._replace(query=query))

    match = re.search(r'"downloadUrl":"([^"]+)', html_text)
    if match:
        return match.group(1).replace(r"\u003d", "=").replace(r"\u0026", "&").replace(r"\/", "/")
    raise RuntimeError("Google Drive confirmation URL not found")


def filename_from_response(response: requests.Response, fallback_url: str, suggested: str = "") -> str:
    disposition = response.headers.get("Content-Disposition", "")
    match = re.search(r"filename\*=UTF-8''([^;]+)", disposition, flags=re.I)
    if match:
        name = unquote(match.group(1).strip("\"'"))
    else:
        match = re.search(r'filename="?([^";]+)"?', disposition, flags=re.I)
        name = match.group(1).strip() if match else suggested.strip()
    if not name:
        parsed = urlparse(fallback_url)
        query = parse_qs(parsed.query)
        name = (query.get("knm") or [""])[0] or Path(unquote(parsed.path)).name
    name = safe_name(clean_attachment_name(name), 180)
    if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
        guessed = mimetypes.guess_extension(content_type) or ""
        if guessed.lower() in ALLOWED_EXTENSIONS:
            name += guessed
    return name


def save_response_file(response: requests.Response, destination_dir: Path, *,
                       fallback_url: str, suggested_name: str = "") -> Path:
    name = filename_from_response(response, fallback_url, suggested_name)
    if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise RuntimeError(f"Unsupported attachment type: {name}")
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / name
    temporary = destination.with_suffix(destination.suffix + ".part")
    total = 0
    try:
        with temporary.open("wb") as output:
            for chunk in response.iter_content(256 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError("Attachment exceeded size limit")
                output.write(chunk)
        if total == 0:
            raise RuntimeError("Downloaded attachment was empty")
        return preserve_download(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def download_google_drive(session: requests.Session, url: str, destination_dir: Path,
                          suggested_name: str = "") -> Path:
    file_id = google_drive_file_id(url)
    if not file_id:
        raise RuntimeError(f"Google Drive file id not found: {url}")

    current = f"https://drive.google.com/uc?id={quote(file_id)}"
    for _ in range(4):
        response = request(session, current, stream=True)
        content_type = response.headers.get("Content-Type", "").lower()
        if "Content-Disposition" in response.headers and "text/html" not in content_type:
            try:
                return save_response_file(
                    response, destination_dir,
                    fallback_url=current,
                    suggested_name=suggested_name,
                )
            finally:
                response.close()

        if "text/html" not in content_type:
            try:
                return save_response_file(
                    response, destination_dir,
                    fallback_url=current,
                    suggested_name=suggested_name,
                )
            finally:
                response.close()

        html_text = response.text
        response.close()
        current = google_drive_confirmation_url(html_text)

    raise RuntimeError("Google Drive confirmation loop exceeded")


def download_file(session: requests.Session, url: str, destination_dir: Path, *,
                  referer: str | None = None, suggested_name: str = "") -> Path:
    if google_drive_file_id(url):
        return download_google_drive(session, url, destination_dir, suggested_name)

    with request(session, url, referer=referer, stream=True) as response:
        return save_response_file(
            response,
            destination_dir,
            fallback_url=response.url or url,
            suggested_name=suggested_name,
        )


def naver_frame_html(session: requests.Session, page_url: str, html_text: str) -> tuple[str, str]:
    if "naver.com" not in (urlparse(page_url).hostname or ""):
        return page_url, html_text
    soup = BeautifulSoup(html_text, "html.parser")
    frame = soup.find("iframe", id="mainFrame") or soup.find("iframe", attrs={"name": "mainFrame"})
    if frame and frame.get("src"):
        frame_url = urljoin(page_url, frame.get("src"))
        return frame_url, request(session, frame_url, referer=page_url).text
    return page_url, html_text


def attachment_candidates(page_url: str, html_text: str) -> list[dict]:
    candidates: list[dict] = []
    seen: set[str] = set()

    def add(url: str, filename: str = "") -> None:
        url = (url or "").replace("&amp;", "&").strip()
        if not url:
            return
        absolute = urljoin(page_url, url)
        parsed = urlparse(absolute)
        query = parse_qs(parsed.query)
        query_name = (query.get("knm") or [""])[0]
        cleaned = clean_attachment_name(filename or query_name)
        path_ext = Path(unquote(parsed.path)).suffix.lower()
        text_ext = Path(cleaned).suffix.lower()
        is_drive = google_drive_download_url(absolute) is not None
        is_attachment = "attach=1" in parsed.query.lower() or "attachment" in parsed.path.lower()
        if not (path_ext in ALLOWED_EXTENSIONS or text_ext in ALLOWED_EXTENSIONS or is_drive or is_attachment):
            return
        if absolute in seen:
            return
        seen.add(absolute)
        candidates.append({"url": absolute, "filename": cleaned})

    for match in re.finditer(
        r"aPostFiles\[\d+\]\s*=\s*JSON\.parse\('(\[.*?\])'\s*\.replace",
        html_text,
        flags=re.I | re.S,
    ):
        raw = match.group(1).replace("\\\\'", '"').replace("\\'", '"')
        try:
            rows = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for row in rows if isinstance(rows, list) else []:
            add(str(row.get("encodedAttachFileUrl") or ""), str(row.get("encodedAttachFileName") or ""))

    soup = BeautifulSoup(html_text, "html.parser")
    for anchor in soup.find_all("a", href=True):
        add(anchor.get("href", ""), anchor.get("download") or " ".join(anchor.stripped_strings))

    for match in re.finditer(
        r"https?://[^\"'<>\s]+?(?:\.zip|\.7z|\.rar|\.smi|\.ass|\.ssa|\.vtt)(?:\?[^\"'<>\s]*)?",
        html_text,
        flags=re.I,
    ):
        add(match.group(0))

    return candidates


def unique_downloads(session: requests.Session, candidates: list[dict], destination: Path,
                     referer: str) -> list[Path]:
    files: list[Path] = []
    seen_paths: set[Path] = set()
    errors: list[str] = []
    for candidate in candidates:
        try:
            path = download_file(
                session,
                candidate["url"],
                destination,
                referer=referer,
                suggested_name=candidate.get("filename", ""),
            )
            if path not in seen_paths:
                seen_paths.add(path)
                files.append(path)
        except Exception as exc:
            errors.append(f"{candidate['url']}: {exc}")
    if not files:
        raise RuntimeError("All attachment downloads failed: " + " | ".join(errors[:5]))
    return files


def unique_paths(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def anchor_downloads(session: requests.Session, anchors, destination: Path, referer: str) -> list[Path]:
    """Mirror the GUI behavior: inspect every link in the content container.

    Google Drive links use the Drive handler. Other links are opened just far
    enough to inspect the response filename; unsupported files are skipped.
    """
    files: list[Path] = []
    errors: list[str] = []
    for anchor in anchors:
        href = anchor.get("href") if hasattr(anchor, "get") else None
        if not href:
            continue
        link = urljoin(referer, unescape(str(href)).replace("&amp;", "&"))
        if "googleusercontent" in link.lower():
            continue
        suggested = ""
        if hasattr(anchor, "get"):
            suggested = anchor.get("download") or " ".join(anchor.stripped_strings)

        try:
            if google_drive_file_id(link):
                files.append(download_google_drive(session, link, destination, suggested))
                continue

            response = request(session, link, referer=referer, stream=True)
            try:
                candidate_name = filename_from_response(response, response.url or link, suggested)
                if Path(candidate_name).suffix.lower() not in ALLOWED_EXTENSIONS:
                    continue
                files.append(
                    save_response_file(
                        response,
                        destination,
                        fallback_url=response.url or link,
                        suggested_name=suggested,
                    )
                )
            finally:
                response.close()
        except Exception as exc:
            errors.append(f"{link}: {exc}")

    files = unique_paths(files)
    if not files and errors:
        raise RuntimeError("All GUI-style attachment downloads failed: " + " | ".join(errors[:5]))
    return files


def download_naver_gui(session: requests.Session, source_url: str, destination: Path) -> list[Path]:
    response = request(session, source_url)
    page_url = response.url
    html_text = response.text

    # Follow the same mainFrame flow used by the GUI downloader.
    for _ in range(3):
        if "PostView.naver" in page_url or "PostList.naver" in page_url:
            break
        soup = BeautifulSoup(html_text, "html.parser")
        frame = soup.find("iframe", id="mainFrame") or soup.find("iframe", attrs={"name": "mainFrame"})
        if not frame or not frame.get("src"):
            break
        page_url = urljoin("https://blog.naver.com", frame.get("src"))
        response = request(session, page_url, referer=source_url)
        html_text = response.text

    candidates = attachment_candidates(page_url, html_text)
    if candidates:
        files = unique_downloads(session, candidates, destination, page_url)
        if files:
            return files

    soup = BeautifulSoup(html_text, "html.parser")
    container = soup.find("div", class_="se-main-container") or soup
    files = anchor_downloads(session, container.find_all("a", href=True), destination, page_url)
    if not files:
        raise RuntimeError("Naver attached file not found")
    return files


def download_blogspot_gui(session: requests.Session, source_url: str, destination: Path) -> list[Path]:
    response = request(session, source_url)
    soup = BeautifulSoup(response.text, "html.parser")
    container = soup.find("div", class_="post-body")
    if container is None:
        raise RuntimeError("Blogspot post-body not found")
    files = anchor_downloads(session, container.find_all("a", href=True), destination, response.url)
    if not files:
        raise RuntimeError("Blogspot subtitle attachment not found")
    return files


def tistory_attachment_candidates(page_url: str, html_text: str) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()

    for match in re.finditer(
        r'href=[\'\"](\S+?/attachment/.*?)[\'\"]\s*.*?/>\s*(.*?)</',
        html_text,
        flags=re.I | re.S,
    ):
        url = urljoin(page_url, unescape(match.group(1)).replace("&amp;", "&"))
        name = clean_attachment_name(BeautifulSoup(match.group(2), "html.parser").get_text(" ", strip=True))
        if url not in seen:
            seen.add(url)
            out.append({"url": url, "filename": name})

    return out


def tistory_winpng_image_urls(page_url: str, container) -> list[str]:
    """Return original Tistory/Kakao PNG URLs, never resized thumbnail URLs."""
    out: list[str] = []
    seen: set[str] = set()
    for node in container.find_all(True):
        for attr in ("data-url", "data-phocus", "src"):
            raw = str(node.get(attr) or "").strip()
            if not raw:
                continue
            url = urljoin(page_url, unescape(raw).replace("&amp;", "&"))
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            if not (host == "blog.kakaocdn.net" or host.endswith(".kakaocdn.net")):
                continue
            if Path(unquote(parsed.path)).suffix.lower() != ".png":
                continue
            if url in seen:
                continue
            seen.add(url)
            out.append(url)
    return out


def response_bytes(response: requests.Response, max_bytes: int = MAX_DOWNLOAD_BYTES) -> bytes:
    declared = str(response.headers.get("Content-Length") or "").strip()
    if declared.isdigit() and int(declared) > max_bytes:
        raise RuntimeError("WinPNG image exceeded size limit")

    payload = bytearray()
    for chunk in response.iter_content(1024 * 1024):
        if not chunk:
            continue
        payload.extend(chunk)
        if len(payload) > max_bytes:
            raise RuntimeError("WinPNG image exceeded size limit")
    return bytes(payload)


def preserve_winpng_files(embedded, destination: Path,
                          preserved: dict[str, set[Path]] | None = None) -> list[Path]:
    files: list[Path] = []
    for item in embedded:
        relative = Path(item.path)
        if relative.suffix.lower() not in WINPNG_EXTENSIONS:
            continue

        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=target.parent,
                prefix=".winpng-",
                delete=False,
            ) as handle:
                handle.write(item.data)
                temporary = Path(handle.name)
            saved = preserve_download(temporary, target)
            files.append(saved)
            if preserved is not None:
                preserved.setdefault(relative.as_posix().casefold(), set()).add(saved)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return files


def download_tistory_winpng(session: requests.Session, page_url: str,
                            container, destination: Path) -> list[Path]:
    """Decode files embedded in original Kakao PNGs by WinPNG."""
    files: list[Path] = []
    errors: list[str] = []
    preserved: dict[str, set[Path]] = {}
    for image_url in tistory_winpng_image_urls(page_url, container):
        try:
            response = request(session, image_url, referer=page_url, stream=True)
            try:
                payload = response_bytes(response)
            finally:
                response.close()
            embedded = decode_winpng_bytes(payload)
            if not embedded:
                continue
            files.extend(preserve_winpng_files(embedded, destination, preserved))
        except Exception as exc:
            errors.append(f"{image_url}: {exc}")

    files = unique_paths(files)

    jmk_files = [path for path in files if path.suffix.lower() == ".jmk"]
    if jmk_files:
        jmk_sources = {name: paths for name, paths in preserved.items() if name.endswith(".jmk")}
        if any(len(paths) != 1 for paths in jmk_sources.values()):
            raise RuntimeError("Tistory WinPNG has conflicting JMK inputs at the same relative path")
        converted_saved: dict[str, set[Path]] = {}
        try:
            converted = convert_tistory_jmks(
                page_url,
                set(jmk_sources),
            )
            files.extend(preserve_winpng_files(converted, destination, converted_saved))
            files = unique_paths(files)
        except Exception as exc:
            errors.append(f"Jamaker conversion: {exc}")

        playable = [
            path for path in files
            if path.suffix.lower() in ALLOWED_EXTENSIONS
            and path.suffix.lower() not in {".zip", ".7z", ".rar"}
        ]
        if not playable:
            detail = " | ".join(errors[:3]) if errors else "no ASS/SMI output"
            raise RuntimeError(
                "Tistory WinPNG JMK extracted but conversion failed: " + detail
            )

        # Compare the original relative paths before preserve_download adds a
        # digest to a corrected same-name file. A different episode folder is
        # never evidence that this JMK was converted.
        converted_sources = {
            Path(name).with_suffix(".jmk").as_posix().casefold()
            for name, paths in converted_saved.items()
            if Path(name).suffix.lower() in {".ass", ".smi", ".srt", ".ssa", ".vtt"}
            and any(path in files and path.exists() for path in paths)
        }
        missing_conversions = [
            name for name in jmk_sources
            if name not in converted_sources
        ]
        if missing_conversions:
            raise RuntimeError(
                "Tistory WinPNG JMK conversion incomplete: "
                + ", ".join(missing_conversions[:5])
            )

        # WinPNG source PNG bytes are never persisted. Once every extracted JMK
        # has a playable conversion, remove all raw JMK inputs as well so only
        # ASS/SMI/SRT/SSA/VTT (or archive outputs) remain in subtitle-data.
        for jmk_path in jmk_files:
            jmk_path.unlink(missing_ok=True)
        files = [
            path for path in files
            if path.exists() and path.suffix.lower() not in {".jmk", ".png"}
        ]

    if files:
        return files
    if errors:
        raise RuntimeError("Tistory WinPNG decode failed: " + " | ".join(errors[:3]))
    return []


def download_tistory_gui(session: requests.Session, source_url: str, destination: Path) -> list[Path]:
    response = request(session, source_url)
    page_url = response.url
    html_text = response.text

    candidates = tistory_attachment_candidates(page_url, html_text)
    if candidates:
        return unique_downloads(session, candidates, destination, page_url)

    soup = BeautifulSoup(html_text, "html.parser")
    container = (
        soup.find("div", class_="tt_article_useless_p_margin contents_style")
        or soup.find("div", class_="contents_style")
        or soup
    )
    anchor_error = None
    try:
        files = anchor_downloads(session, container.find_all("a", href=True), destination, page_url)
    except RuntimeError as exc:
        anchor_error = exc
        files = []
    if files:
        return files

    files = download_tistory_winpng(session, page_url, container, destination)
    if files:
        return files

    if anchor_error is not None:
        raise RuntimeError(f"Tistory subtitle attachment not found: {anchor_error}")
    raise RuntimeError("Tistory subtitle attachment not found")


def extract_csrf_token(html_text: str) -> str | None:
    soup = BeautifulSoup(html_text, "html.parser")
    meta = soup.find("meta", attrs={"name": "csrf-token"})
    return meta.get("content") if meta else None


def download_erulabo_gui(session: requests.Session, source_url: str, destination: Path,
                         html_text: str) -> list[Path]:
    """Port the GUI's erulabo CSRF/token flow.

    The original GUI marks this path as deprecated because Turnstile can still
    block it. We nevertheless try the same flow before leaving it for retry.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    buttons = soup.find_all("button", attrs={"data-file-url": True})
    if not buttons:
        raise RuntimeError("Erulabo data-file-url not found")

    csrf = extract_csrf_token(html_text)
    if not csrf:
        raise RuntimeError("Erulabo CSRF token not found")

    files: list[Path] = []
    errors: list[str] = []
    for button in buttons:
        file_path = str(button.get("data-file-url") or "")
        if not file_path:
            continue
        token_base = urljoin("https://erulabo.com", file_path)
        try:
            token_resp = session.post(
                token_base.rstrip("/") + "/token",
                json={},
                headers={
                    "User-Agent": UA,
                    "Accept": "*/*",
                    "Content-Type": "application/json",
                    "X-CSRF-TOKEN": csrf,
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": source_url,
                    "Origin": "https://erulabo.com",
                },
                timeout=(15, 35),
            )
            token_resp.raise_for_status()
            download_url = str(token_resp.json().get("download_url") or "")
            if not download_url:
                raise RuntimeError("Erulabo token response has no download_url")

            redirect = session.get(
                download_url,
                headers={"User-Agent": UA, "Referer": source_url},
                allow_redirects=False,
                timeout=(15, 35),
            )
            if redirect.status_code in (301, 302, 303, 307, 308):
                final_url = redirect.headers.get("Location") or download_url
            else:
                final_url = download_url
            files.append(download_file(session, final_url, destination, referer=source_url))
        except Exception as exc:
            errors.append(str(exc))

    files = unique_paths(files)
    if not files:
        raise RuntimeError("Erulabo GUI-style download failed: " + " | ".join(errors[:3]))
    return files


def download_website_gui(session: requests.Session, source_url: str, destination: Path) -> list[Path]:
    response = request(session, source_url)
    page_url = response.url
    html_text = response.text

    if "erulabo.com" in (urlparse(page_url).hostname or "").lower():
        return download_erulabo_gui(session, page_url, destination, html_text)

    soup = BeautifulSoup(html_text, "html.parser")
    container = soup.find("div") or soup
    files = anchor_downloads(session, container.find_all("a", href=True), destination, page_url)
    if files:
        return files

    # Keep the current broad parser as a fallback for sites the GUI does not
    # know explicitly.
    candidates = attachment_candidates(page_url, html_text)
    if candidates:
        return unique_downloads(session, candidates, destination, page_url)
    raise RuntimeError("General website subtitle attachment not found")


def download_source_page(session: requests.Session, source_url: str, destination: Path,
                         handler: str | None = None) -> list[Path]:
    if not source_url:
        raise RuntimeError("Subtitle source URL is empty")

    parsed = urlparse(source_url)
    if google_drive_folder_id(source_url):
        raise RuntimeError("Google Drive folder needs Anissia folder sync context")
    if Path(unquote(parsed.path)).suffix.lower() in ALLOWED_EXTENSIONS or google_drive_file_id(source_url):
        return [download_file(session, source_url, destination)]

    route = handler or classify_source_url(source_url)
    if route == "empty":
        raise RuntimeError("Subtitle source URL is empty")
    if route == "naver":
        return download_naver_gui(session, source_url, destination)
    if route == "blogspot":
        return download_blogspot_gui(session, source_url, destination)
    if route == "tistory":
        return download_tistory_gui(session, source_url, destination)
    return download_website_gui(session, source_url, destination)


def download_aniall(session: requests.Session, item: dict, target: Path) -> list[Path]:
    folder_identity = aniall_folder_identity(item)
    response = request(session, item["source_url"])
    candidates = attachment_candidates(response.url, response.text)
    zip_candidates = [
        candidate for candidate in candidates
        if Path(clean_attachment_name(candidate.get("filename") or "")).suffix.lower() == ".zip"
        or ".zip" in candidate["url"].lower()
    ]
    candidates = zip_candidates or candidates
    if not candidates:
        raise RuntimeError("No AniAll subtitle attachment found")
    destination = work_directory(item, target)
    return unique_downloads(session, candidates, destination, response.url)


def download_anissia(session: requests.Session, item: dict, target: Path) -> list[Path]:
    destination = (
        work_directory(item, target)
        / safe_name(f"{item.get('episode') or '0'}화")
        / safe_name(item.get("creator") or "unknown")
    )
    return download_source_page(
        session,
        item.get("source_url") or "",
        destination,
        item.get("handler") or classify_source_url(item.get("source_url") or ""),
    )


def process_item(session: requests.Session, state: dict, state_path: Path,
                 item: dict, target: Path) -> bool:
    item_id = str(item.get("id") or "")
    if not item_id:
        return False
    is_drive_folder = item.get("source") == "anissia" and bool(google_drive_folder_id(source_url(item)))
    if item_id in state["success"] and not is_drive_folder:
        state["retry"].pop(item_id, None)
        remove_matching_waiting(state, item)
        state["erulabo_pending"].pop(item_id, None)
        save_state(state_path, state)
        return True

    # Missing source URLs are metadata-incomplete records, not download
    # failures. Keep them out of the 3-month retry/retire lifecycle. Monthly
    # targeted API checks or a later discovery Release can supply a real URL.
    if not source_url(item):
        move_to_waiting_url(state, item_id, item)
        save_state(state_path, state)
        print(f"[~] {item_id}: waiting for source URL")
        return False

    # A later Anissia discovery changes the item id when website/source_url is
    # filled in. Remove the older blank-URL waiting record with the same stable
    # work/episode/creator identity before downloading the new item.
    removed_waiting = remove_matching_waiting(state, item)
    if removed_waiting:
        save_state(state_path, state)
        print(f"[=] {item_id}: source URL appeared; cleared {len(removed_waiting)} waiting item(s)")

    if item_id in state["retired"] and not is_drive_folder:
        state["retry"].pop(item_id, None)
        return False
    if is_drive_folder:
        state["retired"].pop(item_id, None)  # Previously failed folder URLs get a fresh chance.

    if item.get("source") == "anissia" and is_erulabo_url(source_url(item)):
        move_to_erulabo_pending(state, item_id, item)
        save_state(state_path, state)
        print(f"[>] {item_id}: queued for separate Erulabo browser step")
        return False

    try:
        source = item.get("source")
        if source == "aniall":
            files = download_aniall(session, item, target)
        elif source == "anissia":
            files = (download_anissia_drive_folder(session, item, target, state, state_path)
                     if is_drive_folder else download_anissia(session, item, target))
        else:
            raise RuntimeError(f"Unknown subtitle source: {source}")

        relative_files = [path.relative_to(target).as_posix() for path in files]
        if is_drive_folder and item_id in state["success"] and sorted(relative_files) == sorted(state["success"][item_id].get("files", [])):
            state["retry"].pop(item_id, None)
            save_state(state_path, state)
            print(f"[=] {item_id}: Google Drive folder unchanged ({len(files)} files)")
            return True

        if source == "anissia":
            try:
                maybe_backfill_history(session, state, state_path, item, target)
            except Exception as backfill_exc:
                # The current subtitle download succeeded. A failed one-time
                # archive search must not turn the current item into a retry.
                # Leaving backfill_done unset lets a future run try again.
                print(f"[-] backfill search {item_id}: {backfill_exc}")

        if not is_drive_folder:
            register_sources(item, files, target)
        metadata = build_download_metadata(item, files, target)
        queue_cache_cleanup(item, files, target)
        state["success"][item_id] = {
            "source": source,
            "source_url": item.get("source_url"),
            "item": item,
            "files": [str(path.relative_to(target)) for path in files],
            "file_metadata": metadata["files"],
            "coverage": metadata["coverage"],
            "verification": metadata["verification"],
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        state["retry"].pop(item_id, None)
        state["waiting_url"].pop(item_id, None)
        state["erulabo_pending"].pop(item_id, None)
        save_state(state_path, state)
        print(f"[+] {item_id}: {len(files)} file(s)")
        return True
    except Exception as exc:
        previous = state["retry"].get(item_id) or {}
        reason = classify_failure(exc)
        state["retry"][item_id] = {
            "item": item,
            "first_failed_month": first_failed_month(previous),
            "reason": reason,
            "last_error": str(exc),
            "last_attempt": dt.datetime.now(KST).isoformat(timespec="seconds"),
        }
        save_state(state_path, state)
        append_failure(item, exc, reason)
        print(f"[-] {item_id} [{reason}]: {exc}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--state", type=Path, default=STATE_FILE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    token = os.environ.get("ANIME_DB_GITHUB_TOKEN", "").strip()
    if not token:
        raise SystemExit("ANIME_DB_GITHUB_TOKEN is not set")

    state = load_state(args.state)
    processed = set(state["processed_releases"])
    releases = list_queue_releases(token)
    pending = [release for release in releases if release["tag_name"] not in processed]

    if args.dry_run:
        print(f"Local retry items: {len(state['retry'])}")
        print(f"Retry reasons: {reason_counts(state['retry'])}")
        print(f"Waiting for URL: {len(state['waiting_url'])}")
        print(f"Waiting reasons: {reason_counts(state['waiting_url'])}")
        print(f"Erulabo browser pending: {len(state['erulabo_pending'])}")
        print(f"Backfill completed: {len(state['backfill_done'])}")
        print(f"Pending queue Releases: {len(pending)}")
        for release in pending:
            print(f"  - {release['tag_name']}")
        return

    moved_waiting = migrate_empty_url_retries(state)
    if moved_waiting:
        save_state(args.state, state)
        print(f"[=] Migrated {moved_waiting} empty-URL retry item(s) to waiting_url")

    print(f"Local retry items: {len(state['retry'])}")
    print(f"Retry reasons: {reason_counts(state['retry'])}")
    print(f"Waiting for URL: {len(state['waiting_url'])}")
    print(f"Waiting reasons: {reason_counts(state['waiting_url'])}")
    print(f"Erulabo browser pending: {len(state['erulabo_pending'])}")
    print(f"Backfill completed: {len(state['backfill_done'])}")
    print(f"Pending queue Releases: {len(pending)}")

    args.target.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    attempted: set[str] = set()

    # Retry old failures first. Keep a failed URL active for three calendar
    # months including the month it first failed. Starting in the fourth
    # calendar month, retire it instead of opening the page again.
    current_month = current_month_key()
    retired_count = 0
    for item_id, retry in list(state["retry"].items()):
        first_month = first_failed_month(retry)
        if month_distance(first_month, current_month) >= RETRY_MONTHS:
            append_retired(
                item_id,
                retry,
                f"failed for {RETRY_MONTHS} calendar months; removed from retry on month 4",
            )
            state["retry"].pop(item_id, None)
            state["retired"][item_id] = {
                "first_failed_month": first_month,
                "retired_month": current_month,
                "source": (retry.get("item") or {}).get("source"),
                "last_reason": retry.get("reason"),
            }
            save_state(args.state, state)
            retired_count += 1
            print(f"[x] {item_id}: retired after 3 failed calendar months")
            continue
        item = retry.get("item") or {}
        attempted.add(item_id)
        process_item(session, state, args.state, item, args.target)

    for release in pending:
        tag = release["tag_name"]
        asset = queue_asset(release)
        with tempfile.TemporaryDirectory(prefix="subtitle-queue-") as tmp:
            path = Path(tmp) / QUEUE_NAME
            download_github_asset(asset, token, path)
            queue = json.loads(path.read_text(encoding="utf-8"))
        items = queue.get("items")
        if queue.get("version") != 1 or not isinstance(items, list):
            raise RuntimeError(f"Invalid subtitle queue in {tag}")

        print(f"[=] {tag}: {len(items)} queued item(s)")
        for item in items:
            item_id = str(item.get("id") or "")
            if item_id in attempted:
                continue
            attempted.add(item_id)
            process_item(session, state, args.state, item, args.target)

        state["processed_releases"].append(tag)
        save_state(args.state, state)
        print(f"[=] {tag}: queue consumed")

    # Check after consuming Releases so new URL waiters, including the one-time
    # historical backfill, participate in this month's check immediately.
    # Existing waiters are queried only on the first weekly run of each KST
    # month; remaining weeks do not repeat these caption API calls.
    for item in refresh_waiting_urls(session, state, args.state):
        item_id = str(item.get("id") or "")
        if item_id in attempted:
            # Its populated URL was already processed from a Release/retry in
            # this run; clear a duplicate blank-URL record without downloading
            # the same item twice.
            remove_matching_waiting(state, item)
            save_state(args.state, state)
            continue
        attempted.add(item_id)
        process_item(session, state, args.state, item, args.target)

    # A previously successful Drive folder may gain more files without a new
    # Anissia post/URL or GitHub queue release. Recheck once per weekly run.
    for item_id, completed in list(state["success"].items()):
        if item_id in attempted:
            continue
        item = completed.get("item") or {}
        if item.get("source") == "anissia" and google_drive_folder_id(source_url(item)):
            attempted.add(item_id)
            process_item(session, state, args.state, item, args.target)

    print(json.dumps({
        "success": len(state["success"]),
        "retry": len(state["retry"]),
        "retry_reasons": reason_counts(state["retry"]),
        "waiting_url": len(state["waiting_url"]),
        "waiting_reasons": reason_counts(state["waiting_url"]),
        "erulabo_pending": len(state["erulabo_pending"]),
        "backfill_done": len(state["backfill_done"]),
        "processed_releases": len(state["processed_releases"]),
        "retired": len(state["retired"]),
        "retired_this_run": retired_count,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

