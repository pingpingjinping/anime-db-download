#!/usr/bin/env python3
"""Discover new subtitle records and emit a queue for the Pi.

GitHub only discovers candidates:
- AniAll category + post metadata
- Anissia API

It never opens Anissia caption websites or downloads subtitle files.
The Pi consumes the emitted queue and performs the actual page/file download
from the user's home IP.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import re
import time
from pathlib import Path
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

KST = ZoneInfo("Asia/Seoul")
UTC = dt.timezone.utc
UA = "Mozilla/5.0 (compatible; anime-db-download subtitle discovery/1.0)"
ANIALL_BASE = "https://aniall.tistory.com"
ANIALL_CATEGORY = ANIALL_BASE + "/category/" + quote("자막자료실")
ANISSIA_LIST = "https://api.anissia.net/anime/list/{page}?q="
ANISSIA_CAPTION = "https://api.anissia.net/anime/caption/animeNo/{anime_no}"
OVERLAP = dt.timedelta(days=3)


def parse_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    text = value.strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d",
    ):
        try:
            parsed = dt.datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=KST) if parsed.tzinfo is None else parsed.astimezone(KST)
        except ValueError:
            pass
    try:
        parsed = dt.datetime.fromisoformat(text)
        return parsed.replace(tzinfo=KST) if parsed.tzinfo is None else parsed.astimezone(KST)
    except ValueError:
        return None


def iso_kst(value: dt.datetime) -> str:
    return value.astimezone(KST).isoformat(timespec="seconds")


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_state(path: Path) -> dict:
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("version") != 1:
        raise SystemExit("Unsupported subtitle discovery state version")
    state.setdefault("discovered", {}).setdefault("aniall", {})
    state.setdefault("discovered", {}).setdefault("anissia", {})
    state.setdefault("retry", {}).setdefault("anissia_api", {})
    cutoff = parse_datetime(state.get("bootstrap_cutoff_kst"))
    watermark = parse_datetime(state.get("watermark_kst"))
    if not cutoff or not watermark:
        raise SystemExit("Invalid discovery cutoff/watermark")
    return state


def request(session: requests.Session, url: str, timeout: tuple[int, int] = (15, 35)) -> requests.Response:
    last_error = None
    for attempt in range(3):
        try:
            response = session.get(
                url,
                headers={"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7"},
                timeout=timeout,
            )
            if response.status_code in (429, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            response.raise_for_status()
            return response
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1 + attempt)
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def numeric_post_id(href: str) -> str | None:
    href = unquote(href or "")
    match = re.match(r"^(?:https?://aniall\.tistory\.com)?/(\d+)(?:[/?#].*)?$", href)
    return match.group(1) if match else None


def aniall_page_posts(html_text: str, limit: int = 10) -> list[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    rows = []
    seen = set()
    for anchor in soup.find_all("a", href=True):
        post_id = numeric_post_id(anchor.get("href", ""))
        if not post_id or post_id in seen:
            continue
        title = " ".join(anchor.stripped_strings).strip()
        if len(re.sub(r"\W+", "", title)) < 2 or title.isdigit():
            continue
        seen.add(post_id)
        rows.append({
            "post_id": post_id,
            "post_url": f"{ANIALL_BASE}/{post_id}",
            "title": title,
        })
        if len(rows) >= limit:
            break
    return rows


def aniall_expected_pages(html_text: str) -> int | None:
    text = BeautifulSoup(html_text, "html.parser").get_text(" ", strip=True)
    match = re.search(r"자막자료실[^\d]{0,80}([\d,]+)건", text)
    if not match:
        return None
    return math.ceil(int(match.group(1).replace(",", "")) / 10)


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


def caption_key(anime_no: str, caption: dict) -> str:
    raw = "\n".join([
        anime_no,
        str(caption.get("episode") or ""),
        str(caption.get("name") or ""),
        str(caption.get("updDt") or ""),
        str(caption.get("website") or ""),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def discover_aniall(session: requests.Session, state: dict, queue: list[dict],
                    scan_floor: dt.datetime, max_pages: int, delay: float) -> None:
    discovered = state["discovered"]["aniall"]
    first = request(session, ANIALL_CATEGORY).text
    pages = min(max_pages, aniall_expected_pages(first) or max_pages)
    old_page_streak = 0

    for page in range(1, pages + 1):
        body = first if page == 1 else request(session, f"{ANIALL_CATEGORY}?page={page}").text
        posts = aniall_page_posts(body)
        if not posts:
            old_page_streak += 1
            if old_page_streak >= 2:
                break
            continue

        page_dates: list[dt.datetime] = []
        for post in posts:
            post_id = post["post_id"]
            existing = discovered.get(post_id)
            if existing:
                old = parse_datetime(existing.get("published_at"))
                if old:
                    page_dates.append(old)
                continue

            detail = request(session, post["post_url"]).text
            published = aniall_published_at(detail)
            if not published:
                print(f"[-] AniAll {post_id}: could not determine post date")
                continue
            page_dates.append(published)
            if published < scan_floor:
                continue

            item = {
                "id": f"aniall:{post_id}",
                "source": "aniall",
                "post_id": post_id,
                "title": post["title"],
                "published_at": iso_kst(published),
                "source_url": post["post_url"],
            }
            queue.append(item)
            discovered[post_id] = {
                "published_at": item["published_at"],
                "title": item["title"],
                "source_url": item["source_url"],
            }
            print(f"[+] queue AniAll {post_id}: {post['title']}")
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


def discover_anissia(session: requests.Session, state: dict, queue: list[dict],
                     failures: list[dict], scan_floor: dt.datetime, delay: float) -> None:
    discovered = state["discovered"]["anissia"]
    api_retry = state["retry"]["anissia_api"]
    bootstrap = parse_datetime(state["bootstrap_cutoff_kst"])
    assert bootstrap

    first = request(session, ANISSIA_LIST.format(page=0)).json()
    data = first.get("data") if isinstance(first, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected Anissia list response")
    pages = int(data.get("totalPages") or 0)
    if pages <= 0:
        raise RuntimeError("Anissia list did not provide totalPages")

    scanned = 0
    for page in range(pages):
        payload = first if page == 0 else request(session, ANISSIA_LIST.format(page=page)).json()
        content = ((payload.get("data") or {}) if isinstance(payload, dict) else {}).get("content")
        if not isinstance(content, list):
            raise RuntimeError(f"Unexpected Anissia list page {page}")

        for anime in content:
            anime_no = str(anime.get("animeNo") or "")
            if not anime_no:
                continue
            title = str(anime.get("subject") or anime_no)
            scanned += 1
            try:
                captions = anissia_payload(request(session, ANISSIA_CAPTION.format(anime_no=anime_no)))
                if not isinstance(captions, list):
                    raise RuntimeError("Unexpected caption response")
                minimum = bootstrap if anime_no in api_retry else scan_floor
                for caption in captions:
                    updated = parse_datetime(str(caption.get("updDt") or ""))
                    if not updated or updated < minimum:
                        continue
                    key = caption_key(anime_no, caption)
                    if key in discovered:
                        continue
                    item = {
                        "id": f"anissia:{key}",
                        "source": "anissia",
                        "anime_no": anime_no,
                        "anime_title": title,
                        "episode": str(caption.get("episode") or ""),
                        "creator": str(caption.get("name") or ""),
                        "updated_at": iso_kst(updated),
                        "source_url": unquote(str(caption.get("website") or "")).strip(),
                    }
                    queue.append(item)
                    discovered[key] = item
                    print(f"[+] queue Anissia {anime_no} {item['episode']}화 {item['creator']}")
                api_retry.pop(anime_no, None)
            except Exception as exc:
                failure = {
                    "anime_no": anime_no,
                    "anime_title": title,
                    "error": str(exc),
                    "attempted_at": dt.datetime.now(UTC).isoformat(timespec="seconds"),
                }
                failures.append(failure)
                api_retry[anime_no] = failure
                print(f"[-] Anissia API {anime_no}: {exc}")
            if delay:
                time.sleep(delay)
            if scanned % 100 == 0:
                print(f"[=] Anissia API scanned {scanned:,} works")
    print(f"[=] Anissia API scan complete: {scanned:,} works")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--aniall-max-pages", type=int, default=80)
    parser.add_argument("--aniall-delay", type=float, default=0.2)
    parser.add_argument("--anissia-delay", type=float, default=0.02)
    args = parser.parse_args()

    state = load_state(args.state)
    cutoff = parse_datetime(state["bootstrap_cutoff_kst"])
    watermark = parse_datetime(state["watermark_kst"])
    assert cutoff and watermark
    scan_floor = max(cutoff, watermark - OVERLAP)
    run_started = dt.datetime.now(KST)

    queue: list[dict] = []
    failures: list[dict] = []
    session = requests.Session()

    print(f"Bootstrap cutoff : {iso_kst(cutoff)}")
    print(f"Previous watermark: {iso_kst(watermark)}")
    print(f"Scan floor       : {iso_kst(scan_floor)}")

    discover_aniall(
        session, state, queue, scan_floor,
        max_pages=args.aniall_max_pages,
        delay=max(0.0, args.aniall_delay),
    )
    discover_anissia(
        session, state, queue, failures, scan_floor,
        delay=max(0.0, args.anissia_delay),
    )

    queue.sort(key=lambda row: (row["source"], row.get("updated_at") or row.get("published_at") or "", row["id"]))
    state["watermark_kst"] = iso_kst(run_started)
    state["last_run_at_utc"] = dt.datetime.now(UTC).isoformat(timespec="seconds")
    save_json(args.state, state)

    args.out.mkdir(parents=True, exist_ok=True)
    save_json(args.out / "subtitle-queue.json", {
        "version": 1,
        "bootstrap_cutoff_kst": state["bootstrap_cutoff_kst"],
        "scan_floor_kst": iso_kst(scan_floor),
        "generated_at_kst": iso_kst(run_started),
        "count": len(queue),
        "items": queue,
    })
    with (args.out / "discovery-failed.jsonl").open("w", encoding="utf-8") as handle:
        for item in failures:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    save_json(args.out / "summary.json", {
        "queue_count": len(queue),
        "aniall": sum(1 for item in queue if item["source"] == "aniall"),
        "anissia": sum(1 for item in queue if item["source"] == "anissia"),
        "api_failures": len(failures),
        "api_retry": len(state["retry"]["anissia_api"]),
    })

    print(json.dumps(json.loads((args.out / "summary.json").read_text(encoding="utf-8")), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
