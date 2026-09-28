#!/usr/bin/env python3
"""Add/update source records without changing reviewed cross-source links."""
import argparse
import csv
import datetime as dt
import gzip
import json
import re
import sqlite3
import tempfile
import time
from urllib.parse import parse_qs, urljoin, urlparse
import urllib.request
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from match_new_sources import prepare_tables, refresh_auto_matches

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = ROOT / "snapshot" / "anime.db.gz"
SOURCES = {"AniList", "ReAnime", "Onnada", "Anissia"}
ANILIST_URL = "https://graphql.anilist.co"
ANILIST_LOOKBACK_SECONDS = 30 * 24 * 60 * 60
ANILIST_OVERLAP_SECONDS = 24 * 60 * 60
ANILIST_PAGE_SIZE = 50
ANILIST_MAX_PAGES = 100
ANILIST_DELAY_SECONDS = 5.0  # Deliberately gentle: at most 12 requests/minute.
FIELDS = ("year", "year_basis", "source_year_raw", "title_ko", "title_native",
          "title_romaji", "title_english", "format", "status", "genres")


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "anime-db-download/1.0",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as response:
        return json.load(response)


def fetch_html(url):
    # Follow the supplied Colab collector's serial, low-rate pattern.
    for attempt in range(4):
        try:
            response = requests.get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; anime-db-download/1.0)",
                "Referer": "https://onnada.com/",
                "Accept-Language": "ko-KR,ko;q=0.9,ja;q=0.8,en;q=0.7",
            }, timeout=(15, 35))
            if response.status_code == 429:
                wait = int(response.headers.get("Retry-After", "30"))
                time.sleep(min(wait, 300))
                continue
            response.raise_for_status()
            return response.text
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Unable to fetch {url}")


def load_snapshot(snapshot, destination):
    if not snapshot.is_file():
        raise SystemExit(f"Missing SQLite snapshot: {snapshot}")
    with gzip.open(snapshot, "rb") as src, open(destination, "wb") as out:
        while chunk := src.read(1024 * 1024):
            out.write(chunk)


def pack_snapshot(source, snapshot):
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    temporary = snapshot.with_suffix(snapshot.suffix + ".tmp")
    with open(source, "rb") as db, open(temporary, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6, mtime=0) as out:
            while chunk := db.read(1024 * 1024):
                out.write(chunk)
        raw.flush()
    temporary.replace(snapshot)


def year(value):
    try:
        v = int(str(value)[:4])
        return v if 1900 <= v <= 2200 else None
    except (ValueError, TypeError):
        return None


def normalize(source, row):
    if source == "Anissia":
        source_id = str(row["animeNo"])
        original = str(row.get("startDate") or "")
        fields = (year(original), "startDate.year", original, row.get("subject"),
                  row.get("originalSubject"), "", "", "", row.get("status"), row.get("genres"))
        cross = {"anilist_ids": [], "reanime_ids": [], "onnada_ids": []}
        metadata = {k: v for k, v in row.items() if k not in ("subject", "originalSubject", "genres", "captions")}
    elif source == "ReAnime":
        source_id = str(row.get("reanime_id") or row.get("anime_id") or "")
        titles = row.get("title") if isinstance(row.get("title"), dict) else {}
        original = str(row.get("season_year") or "")
        fields = (year(original), "season_year", original, "",
                  row.get("title_japanese") or titles.get("native"),
                  row.get("title_romaji") or titles.get("romaji"),
                  row.get("title_english") or titles.get("english"),
                  row.get("format"), row.get("status"),
                  ",".join(row["genres"]) if isinstance(row.get("genres"), list) else row.get("genres"))
        ani = str(row.get("anilist_id") or "")
        cross = {"anilist_ids": [ani] if ani and ani != "0" else [], "reanime_ids": [source_id]}
        metadata = {k: v for k, v in row.items() if k not in ("title", "title_japanese", "title_romaji", "title_english", "genres")}
    elif source == "Onnada":
        source_id = str(row["onnada_id"])
        original = str(row.get("season_info") or "")
        fields = (year(original), "onnada_year", original, row.get("title_ko"),
                  row.get("title_native"), row.get("title_romaji"), "", "", "review", "")
        cross = {"anilist_ids": [], "reanime_ids": []}
        metadata = {k: v for k, v in row.items() if k not in ("title_ko", "title_native", "title_romaji")}
    else:
        source_id = str(row["id"])
        titles = row.get("title") or {}
        start = row.get("startDate") or {}
        original = str(start.get("year") or "")
        fields = (year(original), "startDate.year", original, "", titles.get("native"),
                  titles.get("romaji"), titles.get("english"), row.get("format"),
                  row.get("status"), ",".join(row.get("genres") or []))
        cross = {"anilist_ids": [source_id], "mal_ids": [str(row["idMal"])] if row.get("idMal") else []}
        metadata = {k: row.get(k) for k in ("idMal", "startDate", "endDate", "season", "seasonYear", "episodes", "averageScore", "popularity", "updatedAt")}
    if not source_id or source_id == "0":
        raise ValueError(f"Missing {source} ID")
    return source_id, fields, cross, metadata


def incoming_rows(source, path):
    with open(path, encoding="utf-8-sig", newline="") as handle:
        if path.suffix == ".csv":
            yield from csv.DictReader(handle)
        else:
            for line in handle:
                if line.strip():
                    yield json.loads(line)


def anilist_rows(since, known):
    query = """
    query ($page: Int!, $perPage: Int!) {
      Page(page: $page, perPage: $perPage) {
        pageInfo { hasNextPage }
        media(type: ANIME, countryOfOrigin: JP,
          format_in: [TV, TV_SHORT, MOVIE, OVA, ONA],
          sort: [UPDATED_AT_DESC]) {
          id idMal updatedAt
          title { romaji english native }
          format status
          startDate { year month day } endDate { year month day }
          season seasonYear episodes genres averageScore popularity
        }
      }
    }
    """
    page = 1
    saw_old = False
    while page <= ANILIST_MAX_PAGES:
        response = requests.post(ANILIST_URL,
            json={"query": query, "variables": {"page": page, "perPage": ANILIST_PAGE_SIZE}},
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "User-Agent": "anime-db-download/1.0"}, timeout=30)
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", "60"))
            time.sleep(max(1, retry_after))
            continue
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise ValueError(f"AniList GraphQL error: {payload['errors']}")
        page_data = payload.get("data", {}).get("Page")
        if not isinstance(page_data, dict) or not isinstance(page_data.get("media"), list):
            raise ValueError(f"Unexpected AniList response; refusing incomplete update: {payload}")
        for row in page_data["media"]:
            updated = int(row.get("updatedAt") or 0)
            if updated < since:
                saw_old = True
                break
            if str(row.get("id") or "") not in known:
                yield row
        if saw_old or not page_data.get("pageInfo", {}).get("hasNextPage"):
            return
        page += 1
        time.sleep(ANILIST_DELAY_SECONDS)
    raise ValueError(f"AniList update exceeded {ANILIST_MAX_PAGES} pages; refusing partial snapshot")


def online_rows(source, known, anilist_since=None):
    if source == "AniList":
        if anilist_since is None:
            anilist_since = int(time.time()) - ANILIST_LOOKBACK_SECONDS
        yield from anilist_rows(anilist_since, known)
    elif source == "Anissia":
        first = fetch_json("https://api.anissia.net/anime/list/0?q=")
        data = first.get("data") or {}
        pages = int(data.get("totalPages") or 0)
        if not pages:
            raise ValueError("Anissia pagination changed; refusing incomplete update")
        # The existing API lists old works first. Inspect the most recent pages only.
        empty = 0
        for page in range(pages - 1, max(-1, pages - 21), -1):
            content = (fetch_json(f"https://api.anissia.net/anime/list/{page}?q=").get("data") or {}).get("content") or []
            if not isinstance(content, list):
                raise ValueError("Unexpected Anissia page")
            empty = empty + 1 if all(str(r.get("animeNo")) in known for r in content) else 0
            yield from content
            if empty >= 2:
                break
    elif source == "ReAnime":
        empty = 0
        for page in range(30):
            url = "https://reanime.to/api/v1/search?sort=year_desc&offset=" + str(page * 100) + "&limit=100"
            data = fetch_json(url)
            content = data.get("results") or []
            if not isinstance(content, list):
                raise ValueError("Unexpected ReAnime page")
            if not content:
                break
            empty = empty + 1 if all(str(r.get("anime_id")) in known for r in content) else 0
            yield from content
            if empty >= 3:
                break
    elif source == "Onnada":
        base_list = "https://onnada.com/anime"
        detail_base = "https://onnada.com/anime/"
        page_queue, visited, listing_ids = [1], set(), set()
        expected_total = None
        while page_queue:
            page = page_queue.pop(0)
            if page in visited:
                continue
            visited.add(page)
            url = base_list if page == 1 else f"{base_list}?page={page}"
            soup = BeautifulSoup(fetch_html(url), "html.parser")
            visible = soup.get_text(" ", strip=True)
            count = re.search(r"전체\s*([\d,]+)\s*개", visible)
            if count and expected_total is None:
                expected_total = int(count.group(1).replace(",", ""))
            for anchor in soup.select("a[href]"):
                path = urlparse(anchor.get("href", "")).path
                match = re.fullmatch(r"/anime/(\d+)/?", path)
                if match and anchor.select_one("h4"):
                    listing_ids.add(int(match.group(1)))
                parsed = urlparse(urljoin(base_list, anchor.get("href", "")))
                if parsed.path.rstrip("/") == "/anime":
                    raw_page = parse_qs(parsed.query).get("page", ["1"])[0]
                    if raw_page.isdigit() and int(raw_page) not in visited and int(raw_page) not in page_queue:
                        page_queue.append(int(raw_page))
            time.sleep(1.2)
        minimum_expected = max(1, expected_total - max(3, int(expected_total * 0.02))) if expected_total else 1
        if not listing_ids or len(listing_ids) < minimum_expected:
            raise ValueError(f"Incomplete Onnada listing: found {len(listing_ids)}, expected {expected_total}")
        new_ids = sorted(listing_ids - {int(x) for x in known if x.isdigit()})
        for anime_id in new_ids:
            detail = BeautifulSoup(fetch_html(detail_base + str(anime_id)), "html.parser")
            h1 = detail.select_one("h1")
            title_ko = h1.get_text(" ", strip=True) if h1 else ""
            if not title_ko or "페이지를 찾을 수 없습니다" in title_ko:
                raise ValueError(f"Onnada detail {anime_id} had no title")
            subtitle = h1.find_next_sibling("p")
            spans = subtitle.find_all("span", recursive=False) if subtitle else []
            parts = [el.get_text(" ", strip=True) for el in spans
                     if el.get_text(" ", strip=True) and el.get_text(" ", strip=True) != "·"]
            meta = {}
            for dt in detail.select("dt"):
                dd = dt.find_next_sibling("dd")
                if dd:
                    label = re.sub(r"\s+", "", dt.get_text(" ", strip=True))
                    if label:
                        meta[label] = dd.get_text(" ", strip=True)
            country = meta.get("제작국가", meta.get("국가", "")).strip()
            category = meta.get("분류", meta.get("구분", "")).strip()
            season_info = meta.get("방영일", meta.get("방영시기", "")).strip()
            native = parts[0] if parts else ""
            country_tokens = [x.strip() for x in re.split(r"[,，、/|]+", country) if x.strip()]
            if "특전" in category or not (any(x in ("일본", "Japan", "JP") for x in country_tokens)
                    or (not country and re.search(r"[\u3040-\u309F\u30A0-\u30FF]", native))):
                continue
            yield {"onnada_id": anime_id, "title_ko": title_ko,
                   "title_native": native, "title_romaji": parts[1] if len(parts) > 1 else "",
                   "country": country,
                   "country_check": "verified_japan" if country_tokens else "needs_country_review",
                   "category": category, "season_info": season_info,
                   "episodes": meta.get("총화수", ""), "detail_url": detail_base + str(anime_id)}
            time.sleep(1.2)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT,
                        help="Path to the existing gzip-compressed SQLite snapshot")
    parser.add_argument("--online", action="store_true", help="Check recent AniList, Anissia, ReAnime and Onnada updates")
    parser.add_argument("--input", action="append", default=[], metavar="SOURCE=PATH",
                        help="Provide a CSV/JSONL snapshot for AniList, ReAnime, Onnada or Anissia")
    args = parser.parse_args()
    if not args.online and not args.input:
        parser.error("Specify --online and/or at least one --input")
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "anime.db"
        load_snapshot(args.snapshot, db_path)
        db = sqlite3.connect(db_path)
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        db.execute("""CREATE TABLE IF NOT EXISTS monthly_review (
            source TEXT NOT NULL, source_record_id TEXT NOT NULL, reason TEXT NOT NULL,
            observed_at TEXT NOT NULL, PRIMARY KEY(source, source_record_id, reason))""")
        prepare_tables(db)
        db.execute("BEGIN")
        counts = {}
        state_changed = False
        try:
            # This release is the canonical four-source catalog. Remove any
            # legacy AniAll rows if a prior workflow run had stored them here.
            removed_aniall = db.execute("DELETE FROM entries WHERE source='AniAll'").rowcount
            removed_aniall_reviews = db.execute("DELETE FROM monthly_review WHERE source='AniAll'").rowcount
            state_changed = bool(removed_aniall or removed_aniall_reviews)
            tasks = []
            if args.online:
                tasks.extend((source, None) for source in ("AniList", "Anissia", "ReAnime", "Onnada"))
            for item in args.input:
                source, sep, filename = item.partition("=")
                if not sep or source not in SOURCES or not Path(filename).is_file():
                    raise ValueError(f"Invalid --input {item}")
                tasks.append((source, Path(filename)))
            for source, filename in tasks:
                known = {str(r[0]) for r in db.execute("SELECT source_record_id FROM entries WHERE source=?", (source,))}
                now = int(time.time())
                if source == "AniList" and filename is None:
                    saved = db.execute("SELECT value FROM metadata WHERE key='anilist_updated_at_watermark'").fetchone()
                    watermark = int(saved[0]) if saved else now - ANILIST_LOOKBACK_SECONDS + ANILIST_OVERLAP_SECONDS
                    # Re-read the previous day's boundary so same-second updates or moving result pages are not lost.
                    anilist_since = max(0, watermark - ANILIST_OVERLAP_SECONDS)
                else:
                    watermark = None
                    anilist_since = None
                rows = online_rows(source, known, anilist_since) if filename is None else incoming_rows(source, filename)
                seen = set()
                added = modified = scanned = 0
                for row in rows:
                    source_id, values, cross, metadata = normalize(source, row)
                    if source_id in seen:
                        continue
                    seen.add(source_id)
                    scanned += 1
                    previous = db.execute("SELECT * FROM entries WHERE source=? AND source_record_id=?", (source, source_id)).fetchone()
                    if previous:
                        previous = dict(zip(("source", "source_record_id", *FIELDS, "cross_source_ids_json", "existing_match_status", "source_metadata_json"), previous))
                        # Keep reviewed links/status; preserve metadata that the recent listing omits.
                        old_cross = json.loads(previous["cross_source_ids_json"])
                        for k, ids in cross.items():
                            old_cross[k] = list(dict.fromkeys([*old_cross.get(k, []), *ids]))
                        cross = old_cross
                        old_metadata = json.loads(previous["source_metadata_json"])
                        metadata_changed = any(old_metadata.get(k) != v for k, v in metadata.items())
                        metadata = {**old_metadata, **metadata}
                        values = tuple(old if new in (None, "", []) else new for new, old in zip(values, (previous[f] for f in FIELDS)))
                        field_changed = any(previous[f] != v for f, v in zip(FIELDS, values))
                        if field_changed or metadata_changed:
                            modified += 1
                        review_changed = any(previous[f] != values[FIELDS.index(f)]
                                             for f in ("year", "format", "title_ko", "title_native", "title_romaji", "title_english"))
                    else:
                        field_changed = False
                        metadata_changed = False
                        review_changed = True
                        added += 1
                    db.execute("""INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(source,source_record_id) DO UPDATE SET
                      year=excluded.year,year_basis=excluded.year_basis,source_year_raw=excluded.source_year_raw,
                      title_ko=excluded.title_ko,title_native=excluded.title_native,
                      title_romaji=excluded.title_romaji,title_english=excluded.title_english,
                      format=excluded.format,status=excluded.status,genres=excluded.genres,
                      cross_source_ids_json=excluded.cross_source_ids_json,
                      source_metadata_json=excluded.source_metadata_json""",
                      (source, source_id, *values, json.dumps(cross, ensure_ascii=False),
                       previous["existing_match_status"] if previous else None,
                       json.dumps(metadata, ensure_ascii=False)))
                    if review_changed:
                        reason = "new_work" if not previous else "identity_fields_changed"
                        db.execute("INSERT OR IGNORE INTO monthly_review VALUES (?,?,?,?)",
                                   (source, source_id, reason, dt.date.today().isoformat()))
                    if not previous and source in ("ReAnime", "Anissia"):
                        db.execute("INSERT OR IGNORE INTO monthly_match_pending VALUES (?,?)",
                                   (source, source_id))
                if not scanned and filename is not None:
                    raise ValueError(f"{source} yielded no records; refusing incomplete update")
                if source == "AniList" and filename is None:
                    if watermark != now:
                        db.execute("INSERT OR REPLACE INTO metadata VALUES ('anilist_updated_at_watermark', ?)", (str(now),))
                        state_changed = True
                counts[source] = {"scanned": scanned, "added": added, "modified": modified}
            matching = refresh_auto_matches(db)
            state_changed = state_changed or matching["changed"]
            db.execute("DELETE FROM year_summary")
            db.execute("""INSERT INTO year_summary(year,source,record_count)
              SELECT year,source,count(*) FROM entries GROUP BY year,source""")
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('entry_count', ?)",
                       (str(db.execute("SELECT count(*) FROM entries").fetchone()[0]),))
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('source_count', ?)",
                       (str(db.execute("SELECT count(DISTINCT source) FROM entries").fetchone()[0]),))
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('sources', ?)",
                       (json.dumps([r[0] for r in db.execute("SELECT DISTINCT source FROM entries ORDER BY source")], ensure_ascii=False),))
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('last_monthly_refresh', ?)", (dt.date.today().isoformat(),))
            db.commit()
            assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            db.close()
            if state_changed or any(v["added"] or v["modified"] for v in counts.values()):
                pack_snapshot(db_path, args.snapshot)
            print(json.dumps({"sources": counts, "new_work_matching": matching}, ensure_ascii=False, indent=2))
        except Exception:
            db.rollback()
            db.close()
            raise


if __name__ == "__main__":
    main()
