#!/usr/bin/env python3
"""Collect new AniAll and Anissia subtitle files into a monthly delta.

The first collection boundary is intentionally fixed at
2026-09-25 23:59:59 Asia/Seoul. Existing subtitle files before that point are
assumed to have already been archived manually.

Only successfully downloaded items are recorded as complete. Failed items are
kept in the retry section of the state file and retried on later runs.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import mimetypes
import os
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

KST = ZoneInfo("Asia/Seoul")
UTC = dt.timezone.utc
UA = "Mozilla/5.0 (compatible; anime-db-download subtitle collector/1.0)"
ANIALL_BASE = "https://aniall.tistory.com"
ANIALL_CATEGORY = ANiall_CATEGORY = ANIALL_BASE + "/category/" + quote("자막자료실")
ANISSIA_LIST = "https://api.anissia.net/anime/list/{page}?q="
ANISSIA_CAPTION = "https://api.anissia.net/anime/caption/animeNo/{anime_no}"
ALLOWED_EXTENSIONS = {".zip", ".7z", ".rar", ".smi", ".ass", ".ssa", ".vtt"}
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
OVERLAP = dt.timedelta(days=3)


def now_kst() -> dt.datetime:
    return dt.datetime.now(KST)


def parse_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    text = value.strip()
    formats = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d",
    )
    for fmt in formats:
        try:
            parsed = dt.datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=KST) if parsed.tzinfo is None else parsed.astimezone(KST)
        except ValueError:
            continue
    try:
        parsed = dt.datetime.fromisoformat(text)
        return parsed.replace(tzinfo=KST) if parsed.tzinfo is None else parsed.astimezone(KST)
    except ValueError:
        return None


def iso_kst(value: dt.datetime) -> str:
    return value.astimezone(KST).isoformat(timespec="seconds")


def safe_name(value: str, limit: int = 120) -> str:
    value = unicodedata.normalize("NFKC", value or "").strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = "_"
    return value[:limit].rstrip(" .") or "_"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def load_state(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"Missing subtitle state: {path}")
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("version") != 1:
        raise SystemExit(f"Unsupported subtitle state version: {state.get('version')}")
    for group in ("success", "retry"):
        state.setdefault(group, {})
        state[group].setdefault("aniall", {})
        state[group].setdefault("anissia", {})
    cutoff = parse_datetime(state.get("bootstrap_cutoff_kst"))
    watermark = parse_datetime(state.get("watermark_kst"))
    if not cutoff or not watermark:
        raise SystemExit("subtitle state is missing a valid bootstrap cutoff/watermark")
    return state


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def append_failure(failures: list[dict], source: str, key: str, error: Exception | str, **fields) -> dict:
    item = {
        "source": source,
        "key": key,
        **fields,
        "error": str(error),
        "attempted_at": dt.datetime.now(UTC).isoformat(timespec="seconds"),
    }
    failures.append(item)
    print(f"[-] {source} {fields.get('title') or fields.get('anime_title') or key}: {error}")
    return item


def request(session: requests.Session, url: str, *, referer: str | None = None,
            timeout: tuple[int, int] = (15, 35), stream: bool = False) -> requests.Response:
    headers = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7"}
    if referer:
        headers["Referer"] = referer
    response = session.get(url, headers=headers, timeout=timeout, stream=stream, allow_redirects=True)
    response.raise_for_status()
    return response


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
    name = safe_name(name, 180)
    if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
        guessed = mimetypes.guess_extension(content_type) or ""
        if guessed.lower() in ALLOWED_EXTENSIONS:
            name += guessed
    return name


def google_drive_download_url(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in {"drive.google.com", "docs.google.com", "drive.usercontent.google.com"}:
        return None
    match = re.search(r"/file/d/([^/]+)", parsed.path)
    file_id = match.group(1) if match else (parse_qs(parsed.query).get("id") or [""])[0]
    if not file_id:
        return None
    return f"https://drive.usercontent.google.com/download?id={quote(file_id)}&export=download&confirm=t"


def download_file(session: requests.Session, url: str, destination_dir: Path, *,
                  referer: str | None = None, suggested_name: str = "") -> Path:
    drive_url = google_drive_download_url(url)
    target_url = drive_url or url
    with request(session, target_url, referer=referer, timeout=(15, 60), stream=True) as response:
        content_type = response.headers.get("Content-Type", "").lower()
        if "text/html" in content_type and drive_url:
            raise RuntimeError("Google Drive returned HTML instead of a file")
        name = filename_from_response(response, target_url, suggested_name)
        if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
            raise RuntimeError(f"Unsupported attachment type: {name or content_type}")
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / name
        temporary = destination.with_suffix(destination.suffix + ".part")
        total = 0
        with temporary.open("wb") as output:
            for chunk in response.iter_content(256 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError(f"Attachment exceeded {MAX_DOWNLOAD_BYTES} bytes")
                output.write(chunk)
        if total == 0:
            temporary.unlink(missing_ok=True)
            raise RuntimeError("Downloaded attachment was empty")
        temporary.replace(destination)
        return destination


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

    def add(url: str, filename: str = ""):
        url = unquote((url or "").replace("&amp;", "&")).strip()
        if not url:
            return
        absolute = urljoin(page_url, url)
        parsed = urlparse(absolute)
        path_ext = Path(unquote(parsed.path)).suffix.lower()
        query = parse_qs(parsed.query)
        query_name = (query.get("knm") or [""])[0]
        text_ext = Path(filename or query_name).suffix.lower()
        is_drive = google_drive_download_url(absolute) is not None
        is_attachment = "attach=1" in parsed.query.lower() or "attachment" in parsed.path.lower()
        if not (path_ext in ALLOWED_EXTENSIONS or text_ext in ALLOWED_EXTENSIONS or is_drive or is_attachment):
            return
        if absolute in seen:
            return
        seen.add(absolute)
        candidates.append({"url": absolute, "filename": filename or query_name})

    # Naver's legacy editor stores attachment metadata in a JSON string.
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
        text = " ".join(anchor.stripped_strings)
        add(anchor.get("href", ""), anchor.get("download") or text)

    # Some Tistory skins emit raw attachment URLs inside script blocks.
    for match in re.finditer(
        r"https?://[^\"'<>\s]+?(?:\.zip|\.7z|\.rar|\.smi|\.ass|\.ssa|\.vtt)(?:\?[^\"'<>\s]*)?",
        html_text,
        flags=re.I,
    ):
        add(match.group(0))

    return candidates


def download_from_source_page(session: requests.Session, source_url: str, destination_dir: Path) -> list[Path]:
    parsed = urlparse(source_url)
    if Path(unquote(parsed.path)).suffix.lower() in ALLOWED_EXTENSIONS or google_drive_download_url(source_url):
        return [download_file(session, source_url, destination_dir)]

    response = request(session, source_url)
    page_url, html_text = naver_frame_html(session, response.url, response.text)
    candidates = attachment_candidates(page_url, html_text)
    if not candidates:
        raise RuntimeError("No downloadable subtitle attachment found")

    downloaded: list[Path] = []
    errors: list[str] = []
    for candidate in candidates:
        try:
            downloaded.append(download_file(
                session,
                candidate["url"],
                destination_dir,
                referer=page_url,
                suggested_name=candidate.get("filename", ""),
            ))
        except Exception as exc:
            errors.append(f"{candidate['url']}: {exc}")
    if not downloaded:
        raise RuntimeError("All attachment downloads failed: " + " | ".join(errors[:5]))
    return downloaded


def record_files(manifest: list[dict], payload_root: Path, files: list[Path], **metadata):
    entries = []
    for path in files:
        relative = path.relative_to(payload_root).as_posix()
        item = {
            **metadata,
            "path": relative,
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        manifest.append(item)
        entries.append({"path": relative, "sha256": item["sha256"], "size": item["size"]})
    return entries


def aniall_post_links(html_text: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    found: list[dict] = []
    seen = set()
    for anchor in soup.find_all("a", href=True):
        href = unquote(anchor.get("href") or "")
        match = re.match(r"^(?:https?://aniall\.tistory\.com)?/(\d+)(?:[/?#].*)?$", href)
        if not match:
            continue
        post_id = match.group(1)
        if post_id in seen:
            continue
        title = " ".join(anchor.stripped_strings).strip()
        if len(re.sub(r"\W+", "", title)) < 2 or title.isdigit():
            continue
        seen.add(post_id)
        found.append({
            "post_id": post_id,
            "post_url": f"{ANIALL_BASE}/{post_id}",
            "title": title,
        })
        if len(found) >= 10:
            break
    return found


def aniall_published_at(html_text: str) -> dt.datetime | None:
    soup = BeautifulSoup(html_text, "html.parser")
    for attrs in (
        {"property": "article:published_time"},
        {"property": "og:regDate"},
        {"name": "article:published_time"},
    ):
        meta = soup.find("meta", attrs=attrs)
        if meta and meta.get("content"):
            parsed = parse_datetime(meta.get("content"))
            if parsed:
                return parsed
    text = soup.get_text(" ", strip=True)
    match = re.search(
        r"자막자료실\s+((?:19|20)\d{2})\.\s*(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{1,2}):(\d{2})",
        text,
    )
    if match:
        return dt.datetime(*(int(x) for x in match.groups()), tzinfo=KST)
    return None


def collect_aniall(session: requests.Session, state: dict, payload_root: Path,
                   manifest: list[dict], failures: list[dict], scan_floor: dt.datetime,
                   max_pages: int, delay: float):
    success = state["success"]["aniall"]
    retry = state["retry"]["aniall"]
    attempted: set[str] = set()

    def process(post: dict) -> dt.datetime | None:
        post_id = str(post["post_id"])
        if post_id in attempted or post_id in success:
            return parse_datetime((success.get(post_id) or {}).get("published_at"))
        attempted.add(post_id)
        try:
            response = request(session, post["post_url"])
            published = aniall_published_at(response.text)
            if not published:
                raise RuntimeError("Could not determine AniAll post date")
            title = post.get("title") or BeautifulSoup(response.text, "html.parser").title.get_text(" ", strip=True)
            destination = payload_root / "aniall_subtitles" / safe_name(title)
            candidates = attachment_candidates(response.url, response.text)
            zip_candidates = [
                item for item in candidates
                if Path(item.get("filename") or urlparse(item["url"]).path).suffix.lower() == ".zip"
                or ".zip" in item["url"].lower()
            ]
            candidates = zip_candidates or candidates
            if not candidates:
                raise RuntimeError("No AniAll attachment found")
            files = []
            errors = []
            for item in candidates:
                try:
                    files.append(download_file(
                        session, item["url"], destination,
                        referer=response.url, suggested_name=item.get("filename", "")
                    ))
                except Exception as exc:
                    errors.append(str(exc))
            if not files:
                raise RuntimeError("All AniAll attachments failed: " + " | ".join(errors[:5]))
            entries = record_files(
                manifest, payload_root, files,
                source="aniall", post_id=post_id, title=title,
                source_url=post["post_url"], published_at=iso_kst(published),
            )
            success[post_id] = {
                "title": title,
                "post_url": post["post_url"],
                "published_at": iso_kst(published),
                "files": entries,
            }
            retry.pop(post_id, None)
            print(f"[+] AniAll {post_id}: {title} ({len(files)} file(s))")
            return published
        except Exception as exc:
            item = append_failure(
                failures, "aniall", post_id, exc,
                post_id=post_id, title=post.get("title", ""), source_url=post["post_url"],
            )
            retry[post_id] = {**post, "last_error": item["error"], "last_attempt": item["attempted_at"]}
            return None

    # Retry old failures first, even if they are outside the moving scan window.
    for post_id, post in list(retry.items()):
        process({
            "post_id": post_id,
            "post_url": post.get("post_url") or f"{ANIALL_BASE}/{post_id}",
            "title": post.get("title", ""),
        })
        if delay:
            time.sleep(delay)

    old_page_streak = 0
    for page in range(1, max_pages + 1):
        url = ANIALL_CATEGORY if page == 1 else f"{ANIALL_CATEGORY}?page={page}"
        body = request(session, url).text
        posts = aniall_post_links(body)
        if not posts:
            old_page_streak += 1
            if old_page_streak >= 2:
                break
            continue

        page_dates: list[dt.datetime] = []
        for post in posts:
            published = process(post)
            if published:
                page_dates.append(published)
            elif post["post_id"] in success:
                old = parse_datetime(success[post["post_id"]].get("published_at"))
                if old:
                    page_dates.append(old)
            if delay:
                time.sleep(delay)

        if page_dates and max(page_dates) < scan_floor:
            old_page_streak += 1
        else:
            old_page_streak = 0
        if old_page_streak >= 2:
            break


def anissia_payload(response: requests.Response):
    payload = response.json()
    return payload.get("data") if isinstance(payload, dict) and "data" in payload else payload


def caption_key(anime_no: str, caption: dict) -> str:
    raw = "\n".join([
        anime_no,
        str(caption.get("episode") or ""),
        str(caption.get("name") or ""),
        str(caption.get("updDt") or ""),
        str(caption.get("website") or ""),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def download_anissia_caption(session: requests.Session, state: dict, payload_root: Path,
                             manifest: list[dict], failures: list[dict],
                             anime_no: str, anime_title: str, caption: dict) -> bool:
    key = caption_key(anime_no, caption)
    success = state["success"]["anissia"]
    retry = state["retry"]["anissia"]
    if key in success:
        retry.pop(key, None)
        return True

    website = unquote(str(caption.get("website") or "")).strip()
    episode = str(caption.get("episode") or "")
    creator = str(caption.get("name") or "")
    updated = str(caption.get("updDt") or "")
    try:
        if not website:
            raise RuntimeError("Anissia caption link is empty")
        destination = (
            payload_root / "anissia_subtitles"
            / safe_name(f"{anime_no}__{anime_title}")
            / safe_name((episode or "0") + "화")
            / safe_name(creator or "unknown")
        )
        files = download_from_source_page(session, website, destination)
        entries = record_files(
            manifest, payload_root, files,
            source="anissia", anime_no=anime_no, anime_title=anime_title,
            episode=episode, creator=creator, caption_updated_at=updated,
            source_url=website,
        )
        success[key] = {
            "anime_no": anime_no,
            "anime_title": anime_title,
            "episode": episode,
            "creator": creator,
            "caption_updated_at": updated,
            "source_url": website,
            "files": entries,
        }
        retry.pop(key, None)
        print(f"[+] Anissia {anime_no} {episode}화 {creator}: {len(files)} file(s)")
        return True
    except Exception as exc:
        item = append_failure(
            failures, "anissia", key, exc,
            anime_no=anime_no, anime_title=anime_title, episode=episode,
            creator=creator, caption_updated_at=updated, source_url=website,
        )
        retry[key] = {
            "anime_no": anime_no,
            "anime_title": anime_title,
            "caption": caption,
            "last_error": item["error"],
            "last_attempt": item["attempted_at"],
        }
        return False


def collect_anissia(session: requests.Session, state: dict, payload_root: Path,
                     manifest: list[dict], failures: list[dict], scan_floor: dt.datetime,
                     delay: float):
    attempted: set[str] = set()

    # Retry failures from older runs before the normal scan.
    for key, item in list(state["retry"]["anissia"].items()):
        caption = item.get("caption") or {}
        actual_key = caption_key(str(item.get("anime_no") or ""), caption)
        if actual_key != key:
            state["retry"]["anissia"].pop(key, None)
            continue
        attempted.add(key)
        download_anissia_caption(
            session, state, payload_root, manifest, failures,
            str(item.get("anime_no") or ""), str(item.get("anime_title") or ""), caption,
        )
        if delay:
            time.sleep(delay)

    first = request(session, ANISSIA_LIST.format(page=0)).json()
    data = first.get("data") if isinstance(first, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected Anissia anime list response")
    pages = int(data.get("totalPages") or 0)
    if pages <= 0:
        raise RuntimeError("Anissia anime list did not provide totalPages")

    anime_count = 0
    for page in range(pages):
        page_payload = first if page == 0 else request(session, ANISSIA_LIST.format(page=page)).json()
        page_data = page_payload.get("data") if isinstance(page_payload, dict) else None
        content = (page_data or {}).get("content")
        if not isinstance(content, list):
            raise RuntimeError(f"Unexpected Anissia anime list page {page}")
        for anime in content:
            anime_no = str(anime.get("animeNo") or "")
            if not anime_no:
                continue
            anime_title = str(anime.get("subject") or anime_no)
            anime_count += 1
            try:
                captions = anissia_payload(request(session, ANISSIA_CAPTION.format(anime_no=anime_no)))
                if not isinstance(captions, list):
                    raise RuntimeError("Unexpected Anissia caption response")
                for caption in captions:
                    updated = parse_datetime(str(caption.get("updDt") or ""))
                    if not updated or updated < scan_floor:
                        continue
                    key = caption_key(anime_no, caption)
                    if key in attempted or key in state["success"]["anissia"]:
                        continue
                    attempted.add(key)
                    download_anissia_caption(
                        session, state, payload_root, manifest, failures,
                        anime_no, anime_title, caption,
                    )
            except Exception as exc:
                # Failure to read one anime's caption list must be visible and retryable
                # on the next full scan, but it must not abort the other thousands of works.
                append_failure(
                    failures, "anissia_api", f"anime:{anime_no}", exc,
                    anime_no=anime_no, anime_title=anime_title,
                    source_url=ANISSIA_CAPTION.format(anime_no=anime_no),
                )
            if delay:
                time.sleep(delay)
            if anime_count % 100 == 0:
                print(f"[=] Anissia scanned {anime_count:,} anime works")
    print(f"[=] Anissia scan complete: {anime_count:,} anime works")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--aniall-max-pages", type=int, default=80)
    parser.add_argument("--aniall-delay", type=float, default=0.4)
    parser.add_argument("--anissia-delay", type=float, default=0.05)
    args = parser.parse_args()

    state = load_state(args.state)
    cutoff = parse_datetime(state["bootstrap_cutoff_kst"])
    watermark = parse_datetime(state["watermark_kst"])
    assert cutoff and watermark
    scan_floor = max(cutoff, watermark - OVERLAP)
    run_started = now_kst()

    out = args.out
    payload_root = out / "payload"
    payload_root.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    failures: list[dict] = []

    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    print(f"Bootstrap cutoff : {iso_kst(cutoff)}")
    print(f"Previous watermark: {iso_kst(watermark)}")
    print(f"Scan floor       : {iso_kst(scan_floor)}")

    collect_aniall(
        session, state, payload_root, manifest, failures, scan_floor,
        max_pages=args.aniall_max_pages, delay=max(0.0, args.aniall_delay),
    )
    collect_anissia(
        session, state, payload_root, manifest, failures, scan_floor,
        delay=max(0.0, args.anissia_delay),
    )

    state["watermark_kst"] = iso_kst(run_started)
    state["last_run_at_utc"] = dt.datetime.now(UTC).isoformat(timespec="seconds")
    save_json(args.state, state)

    manifest_doc = {
        "version": 1,
        "bootstrap_cutoff_kst": state["bootstrap_cutoff_kst"],
        "scan_floor_kst": iso_kst(scan_floor),
        "run_started_kst": iso_kst(run_started),
        "download_count": len(manifest),
        "failure_count": len(failures),
        "downloads": manifest,
    }
    save_json(out / "manifest.json", manifest_doc)
    with (out / "failed.jsonl").open("w", encoding="utf-8") as handle:
        for item in failures:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    save_json(out / "summary.json", {
        "download_count": len(manifest),
        "failure_count": len(failures),
        "retry_aniall": len(state["retry"]["aniall"]),
        "retry_anissia": len(state["retry"]["anissia"]),
    })

    print(json.dumps({
        "download_count": len(manifest),
        "failure_count": len(failures),
        "retry_aniall": len(state["retry"]["aniall"]),
        "retry_anissia": len(state["retry"]["anissia"]),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
