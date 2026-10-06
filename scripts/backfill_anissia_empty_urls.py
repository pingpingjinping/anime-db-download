#!/usr/bin/env python3
"""Build a one-time Pi queue from all Anissia caption rows with no source URL.

This intentionally ignores the normal subtitle discovery bootstrap cutoff.
The generated queue uses the same item IDs as regular discovery, so the Pi can
store these rows in waiting_url and later replace them when Anissia publishes a
real website URL.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

import requests

UA = "Mozilla/5.0 (compatible; anime-db-download empty-url-backfill/1.0)"
ANISSIA_LIST = "https://api.anissia.net/anime/list/{page}?q="
ANISSIA_CAPTION = "https://api.anissia.net/anime/caption/animeNo/{anime_no}"


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--delay", type=float, default=0.02)
    args = parser.parse_args()

    session = requests.Session()
    first = request(session, ANISSIA_LIST.format(page=0)).json()
    data = first.get("data") if isinstance(first, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected Anissia list response")
    pages = int(data.get("totalPages") or 0)
    if pages <= 0:
        raise RuntimeError("Anissia list did not provide totalPages")

    items: list[dict] = []
    failures: list[dict] = []
    scanned_works = 0
    scanned_captions = 0

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
            scanned_works += 1

            try:
                captions = anissia_payload(request(session, ANISSIA_CAPTION.format(anime_no=anime_no)))
                if not isinstance(captions, list):
                    raise RuntimeError("Unexpected caption response")

                for caption in captions:
                    scanned_captions += 1
                    source_url = unquote(str(caption.get("website") or "")).strip()
                    if source_url:
                        continue

                    key = caption_key(anime_no, caption)
                    items.append({
                        "id": f"anissia:{key}",
                        "source": "anissia",
                        "anime_no": anime_no,
                        "anime_title": title,
                        "episode": str(caption.get("episode") or ""),
                        "creator": str(caption.get("name") or ""),
                        "updated_at": str(caption.get("updDt") or ""),
                        "source_url": "",
                        "handler": "empty",
                    })
            except Exception as exc:
                failures.append({
                    "anime_no": anime_no,
                    "anime_title": title,
                    "error": str(exc),
                })
                print(f"[-] Anissia API {anime_no}: {exc}")

            if args.delay > 0:
                time.sleep(args.delay)
            if scanned_works % 100 == 0:
                print(
                    f"[=] scanned {scanned_works:,} works / "
                    f"{scanned_captions:,} captions / {len(items):,} empty URLs"
                )

    # Avoid duplicate rows in case the API happens to return a caption twice.
    unique = {item["id"]: item for item in items}
    items = sorted(
        unique.values(),
        key=lambda row: (
            row.get("anime_title") or "",
            row.get("episode") or "",
            row.get("creator") or "",
            row["id"],
        ),
    )

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    args.out.mkdir(parents=True, exist_ok=True)
    save_json(args.out / "subtitle-queue.json", {
        "version": 1,
        "generated_at_utc": generated_at,
        "kind": "anissia-empty-url-backfill",
        "count": len(items),
        "items": items,
    })
    save_json(args.out / "summary.json", {
        "scanned_works": scanned_works,
        "scanned_captions": scanned_captions,
        "queue_count": len(items),
        "api_failures": len(failures),
    })
    with (args.out / "backfill-failed.jsonl").open("w", encoding="utf-8") as handle:
        for row in failures:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(json.dumps({
        "scanned_works": scanned_works,
        "scanned_captions": scanned_captions,
        "queue_count": len(items),
        "api_failures": len(failures),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
