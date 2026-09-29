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

REPO = "pingpingjinping/anime-db-download"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases"
TAG_RE = re.compile(r"^subs-queue-\d{4}-\d{2}-\d{2}-\d+(?:-\d+)?$")
QUEUE_NAME = "subtitle-queue.json"
DEFAULT_TARGET = Path("/home/pi/streaming-project/subtitle-data")
STATE_FILE = Path.home() / ".local/state/anime-db-download/subtitle-queue-state.json"
FAIL_LOG = Path.home() / ".local/state/anime-db-download/subtitle-download-failed.jsonl"
RETIRED_LOG = Path.home() / ".local/state/anime-db-download/subtitle-download-retired.jsonl"
KST = ZoneInfo("Asia/Seoul")
RETRY_MONTHS = 3
ALLOWED_EXTENSIONS = {".zip", ".7z", ".rar", ".smi", ".ass", ".ssa", ".vtt"}
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


def safe_name(value: str, limit: int = 160) -> str:
    value = unicodedata.normalize("NFKC", value or "").strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = "_"
    return value[:limit].rstrip(" .") or "_"


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
        return {"version": 1, "processed_releases": [], "success": {}, "retry": {}, "waiting_url": {}, "retired": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("version") != 1:
        raise RuntimeError(f"Unsupported Pi subtitle state: {path}")
    state.setdefault("processed_releases", [])
    state.setdefault("success", {})
    state.setdefault("retry", {})
    state.setdefault("waiting_url", {})
    state.setdefault("retired", {})
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


def append_retired(item_id: str, retry: dict, reason: str) -> None:
    RETIRED_LOG.parent.mkdir(parents=True, exist_ok=True)
    item = retry.get("item") or {}
    row = {
        "id": item_id,
        "source": item.get("source"),
        "source_url": item.get("source_url"),
        "first_failed_month": first_failed_month(retry),
        "last_error": retry.get("last_error"),
        "retired_at": dt.datetime.now(KST).isoformat(timespec="seconds"),
        "reason": reason,
    }
    with RETIRED_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_failure(item: dict, error: Exception | str) -> None:
    FAIL_LOG.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "id": item.get("id"),
        "source": item.get("source"),
        "source_url": item.get("source_url"),
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


def google_drive_file_id(url: str) -> str | None:
    parsed = urlparse(unescape(url or ""))
    host = (parsed.hostname or "").lower()
    if host not in {"drive.google.com", "docs.google.com", "drive.usercontent.google.com"}:
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
        temporary.replace(destination)
        return destination
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
        url = unquote((url or "").replace("&amp;", "&")).strip()
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
    files = anchor_downloads(session, container.find_all("a", href=True), destination, page_url)
    if not files:
        raise RuntimeError("Tistory subtitle attachment not found")
    return files


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
    destination = target / "aniall_subtitles" / safe_name(item.get("title") or item.get("post_id") or "unknown")
    return unique_downloads(session, candidates, destination, response.url)


def download_anissia(session: requests.Session, item: dict, target: Path) -> list[Path]:
    destination = (
        target / "anissia_subtitles"
        / safe_name(f"{item.get('anime_no', '')}__{item.get('anime_title', '')}")
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
    if item_id in state["success"]:
        state["retry"].pop(item_id, None)
        state["waiting_url"].pop(item_id, None)
        return True

    # Missing source URLs are metadata-incomplete records, not download
    # failures. Keep them out of the 3-month retry/retire lifecycle and wait
    # for a later discovery Release to provide a real URL.
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

    if item_id in state["retired"]:
        state["retry"].pop(item_id, None)
        return False

    try:
        source = item.get("source")
        if source == "aniall":
            files = download_aniall(session, item, target)
        elif source == "anissia":
            files = download_anissia(session, item, target)
        else:
            raise RuntimeError(f"Unknown subtitle source: {source}")

        state["success"][item_id] = {
            "source": source,
            "source_url": item.get("source_url"),
            "files": [str(path.relative_to(target)) for path in files],
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        state["retry"].pop(item_id, None)
        state["waiting_url"].pop(item_id, None)
        save_state(state_path, state)
        print(f"[+] {item_id}: {len(files)} file(s)")
        return True
    except Exception as exc:
        previous = state["retry"].get(item_id) or {}
        state["retry"][item_id] = {
            "item": item,
            "first_failed_month": first_failed_month(previous),
            "last_error": str(exc),
            "last_attempt": dt.datetime.now(KST).isoformat(timespec="seconds"),
        }
        save_state(state_path, state)
        append_failure(item, exc)
        print(f"[-] {item_id}: {exc}")
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

    moved_waiting = migrate_empty_url_retries(state)
    if moved_waiting:
        save_state(args.state, state)
        print(f"[=] Migrated {moved_waiting} empty-URL retry item(s) to waiting_url")

    print(f"Local retry items: {len(state['retry'])}")
    print(f"Waiting for URL: {len(state['waiting_url'])}")
    print(f"Pending queue Releases: {len(pending)}")
    if args.dry_run:
        for release in pending:
            print(f"  - {release['tag_name']}")
        return

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

    print(json.dumps({
        "success": len(state["success"]),
        "retry": len(state["retry"]),
        "waiting_url": len(state["waiting_url"]),
        "processed_releases": len(state["processed_releases"]),
        "retired": len(state["retired"]),
        "retired_this_run": retired_count,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
