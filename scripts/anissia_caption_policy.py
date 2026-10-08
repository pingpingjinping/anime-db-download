"""Shared Anissia caption identity and weekly discovery scope."""
from __future__ import annotations

import datetime as dt
import hashlib
from urllib.parse import unquote

ANISSIA_CAPTION = "https://api.anissia.net/anime/caption/animeNo/{anime_no}"
RECENT_END_DAYS = 90


def anime_date(value) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


def is_historical_anime(anime: dict, today: dt.date) -> bool:
    # Keep active/upcoming/unknown-status works in weekly discovery even if
    # their dates are stale. A missing date must not hide a current work.
    if anime.get("status") != "END":
        return False
    ended = anime_date(anime.get("endDate"))
    if ended:
        return ended < today - dt.timedelta(days=RECENT_END_DAYS)
    started = anime_date(anime.get("startDate"))
    return bool(started and started < today - dt.timedelta(days=365))


def caption_key(anime_no: str, caption: dict) -> str:
    raw = "\n".join([
        anime_no,
        str(caption.get("episode") or ""),
        str(caption.get("name") or ""),
        str(caption.get("updDt") or ""),
        str(caption.get("website") or ""),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def caption_item(anime_no: str, title: str, caption: dict) -> dict:
    return {
        "id": f"anissia:{caption_key(anime_no, caption)}",
        "source": "anissia",
        "anime_no": anime_no,
        "anime_title": title,
        "episode": str(caption.get("episode") or ""),
        "creator": str(caption.get("name") or ""),
        "updated_at": str(caption.get("updDt") or ""),
        "source_url": unquote(str(caption.get("website") or "")).strip(),
    }
