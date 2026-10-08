#!/usr/bin/env python3
"""Collect Anikoto listing metadata without making cross-source work matches."""
import argparse
import datetime as dt
import hashlib
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

DEFAULT_BASE_URL = "https://anikoto.cz"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "state" / "anikoto-catalog.sqlite"
LISTING_SORT = "latest-added"


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def listing_url(base_url, page):
    return base_url.rstrip("/") + "/filter?" + urlencode({"sort": LISTING_SORT, "page": page})


def text(element):
    return element.get_text(" ", strip=True) if element else ""


def number(value, integer=True):
    value = value.strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?", value):
        return None
    return int(value) if integer and value.isdigit() else float(value)


def parse_listing(html, base_url, page):
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one("#list-items")
    if container is None:
        raise ValueError("Listing container missing (blocked, incomplete or changed HTML)")
    cards = container.select(".item")
    if not cards:
        raise ValueError("Full Anikoto catalog page was empty; refusing a completed empty crawl")
    records = []
    seen = set()
    for card in cards:
        anchor = card.select_one("a.name[href]") or card.select_one('a[href*="/watch/"]')
        href = urljoin(base_url, anchor.get("href", "")) if anchor else ""
        parsed = urlparse(href)
        match = re.match(r"^/watch/([^/]+)(?:/|$)", parsed.path)
        title = text(card.select_one(".name,.d-title"))
        if not match or not title or parsed.netloc != urlparse(base_url).netloc:
            raise ValueError(f"Unrecognized listing card on page {page}")
        slug = unquote(match.group(1))
        if slug in seen:
            raise ValueError(f"Duplicate listing slug on page {page}: {slug}")
        seen.add(slug)
        title_jp = str(anchor.get("data-jp", "")).strip()
        native = title_jp if re.search(r"[\u3040-\u30ff\u3400-\u9fff]", title_jp) else ""
        romanized = title_jp if title_jp and not native else ""
        image = card.select_one("img")
        poster = urljoin(base_url, image.get("data-src") or image.get("data-original") or image.get("src") or "") if image else ""
        genre_links = [{"name": text(a), "url": urljoin(base_url, a.get("href", ""))}
                       for a in card.select(".genre a[href]")]
        counts = {key: text(card.select_one(".ep-status." + key)) for key in ("sub", "dub", "total")}
        tip = card.select_one("[data-tip]")
        records.append({
            "source": "Anikoto", "source_record_id": slug,
            "site_id": str(tip.get("data-tip", "")) if tip else "",
            "title": title, "title_jp_raw": title_jp,
            "title_native": native, "title_romaji": romanized,
            "year": None, "status": "", "match_status": "unmatched",
            "format": text(card.select_one(".poster .meta .right")),
            "genres": [item["name"] for item in genre_links], "genre_links": genre_links,
            "poster_url": poster, "detail_url": base_url.rstrip("/") + "/watch/" + match.group(1),
            "listing_watch_url": href,
            "sub_episodes": number(counts["sub"]), "dub_episodes": number(counts["dub"]),
            "total_episodes": number(counts["total"]), "episode_counts_raw": counts,
            "score": number(text(card.select_one(".rated span")), integer=False),
        })
    last_page = page
    for a in soup.select(".pagination a[href]"):
        target = urlparse(urljoin(base_url, a["href"]))
        if target.netloc != urlparse(base_url).netloc or target.path != "/filter":
            continue
        value = parse_qs(target.query).get("page", [""])[0]
        if value.isdigit() and int(value) > 0:
            last_page = max(last_page, int(value))
    return records, last_page


def open_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS anikoto_catalog (
            source_record_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL,
            first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, last_seen_run TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS anikoto_crawl_runs (
            run_id TEXT PRIMARY KEY, base_url TEXT NOT NULL, sort TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
            next_page INTEGER NOT NULL, total_pages INTEGER, last_error TEXT
        );
        CREATE TABLE IF NOT EXISTS anikoto_crawl_pages (
            run_id TEXT NOT NULL, page INTEGER NOT NULL, fetched_at TEXT NOT NULL,
            html_sha256 TEXT NOT NULL, record_fingerprint TEXT NOT NULL,
            item_count INTEGER NOT NULL, source_ids_json TEXT NOT NULL,
            PRIMARY KEY(run_id, page), UNIQUE(run_id, record_fingerprint)
        );
        CREATE TABLE IF NOT EXISTS anikoto_crawl_policy (
            run_id TEXT PRIMARY KEY, mode TEXT NOT NULL,
            known_page_streak INTEGER NOT NULL DEFAULT 0, stop_reason TEXT
        );
        CREATE TABLE IF NOT EXISTS anikoto_catalog_members (
            run_id TEXT NOT NULL, source_record_id TEXT NOT NULL,
            known_at_start INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(run_id, source_record_id)
        );
    """)
    return db


def crawl_policy(db, run_id):
    # Read-only consumers must also work with the original checked-in seed and
    # older Release catalogs without changing their checksums.
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='anikoto_crawl_policy'").fetchone():
        row = db.execute("SELECT mode,known_page_streak,stop_reason FROM anikoto_crawl_policy WHERE run_id=?", (run_id,)).fetchone()
        if row:
            return {"mode": row[0], "known_page_streak": row[1], "stop_reason": row[2]}
    return {"mode": "full", "known_page_streak": 0, "stop_reason": None}


def catalog_condition(db, run_id):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='anikoto_catalog_members'").fetchone() and db.execute(
            "SELECT 1 FROM anikoto_crawl_policy WHERE run_id=?", (run_id,)).fetchone():
        return "source_record_id IN (SELECT source_record_id FROM anikoto_catalog_members WHERE run_id=?)"
    return "last_seen_run=?"


def catalog_rows(db, run_id):
    """Return the effective catalog: saved baseline plus current observations."""
    return db.execute("SELECT source_record_id,payload_json FROM anikoto_catalog WHERE " +
                      catalog_condition(db, run_id) + " ORDER BY source_record_id", (run_id,))


def report(db, run_id):
    run = dict(db.execute("SELECT * FROM anikoto_crawl_runs WHERE run_id=?", (run_id,)).fetchone())
    policy = crawl_policy(db, run_id)
    run.update({
        "source": "Anikoto", "scope": "listing_only", "complete": run["status"] == "complete",
        "pages_collected": db.execute("SELECT count(*) FROM anikoto_crawl_pages WHERE run_id=?", (run_id,)).fetchone()[0],
        "records_in_run": db.execute("SELECT count(*) FROM anikoto_catalog WHERE last_seen_run=?", (run_id,)).fetchone()[0],
        "records_stored": db.execute("SELECT count(*) FROM anikoto_catalog").fetchone()[0],
        "catalog_records": db.execute("SELECT count(*) FROM anikoto_catalog WHERE " + catalog_condition(db, run_id), (run_id,)).fetchone()[0],
        "scan_mode": policy["mode"], "known_page_streak": policy["known_page_streak"],
        "stop_reason": policy["stop_reason"],
        "work_links_created": 0, "subtitle_or_video_files_downloaded": 0,
        "sqlite_quick_check": db.execute("PRAGMA quick_check").fetchone()[0],
    })
    return run


class Client:
    def __init__(self, base_url, delay=1.2):
        self.base_url = base_url
        self.delay = delay
        self.last_request = 0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; anime-db-download/1.0)",
                                     "Referer": base_url + "/", "Accept-Language": "en-US,en;q=0.9"})

    def __call__(self, url):
        for attempt in range(4):
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                response = self.session.get(url, timeout=(15, 35))
                response.raise_for_status()
                return response.text
            except requests.RequestException:
                if attempt == 3:
                    raise
                # Keep individual waits short enough to report failures promptly.
                time.sleep(min(30, 2 ** (attempt + 1)))


def crawl(db_path, base_url=DEFAULT_BASE_URL, fetch=None, delay=1.2, max_pages=0, fresh=False, full=False):
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("--base-url must be a website origin")
    base_url = base_url.rstrip("/")
    fetch = fetch or Client(base_url, delay)
    db = open_db(Path(db_path))
    try:
        previous = None if fresh else db.execute("""SELECT * FROM anikoto_crawl_runs
            WHERE base_url=? AND sort=?
            ORDER BY started_at DESC,rowid DESC LIMIT 1""", (base_url, LISTING_SORT)).fetchone()
        if previous and previous["status"] == "complete":
            previous = None
        if previous and full and crawl_policy(db, previous["run_id"])["mode"] != "full":
            previous = None
        if previous:
            run_id = previous["run_id"]
            db.execute("UPDATE anikoto_crawl_runs SET status='running',last_error=NULL WHERE run_id=?", (run_id,))
        else:
            baseline = db.execute("""SELECT run_id FROM anikoto_crawl_runs
                WHERE base_url=? AND status='complete'
                ORDER BY started_at DESC,rowid DESC LIMIT 1""", (base_url,)).fetchone()
            incremental = bool(baseline and not full)
            run_id = uuid.uuid4().hex
            db.execute("INSERT INTO anikoto_crawl_runs VALUES (?,?,?, ?,NULL,'running',1,NULL,NULL)",
                       (run_id, base_url, LISTING_SORT, now()))
            db.execute("INSERT INTO anikoto_crawl_policy(run_id,mode) VALUES (?,?)",
                       (run_id, "incremental" if incremental else "full"))
            if incremental:
                # Membership is separate from last_seen_run/last_seen_at, so
                # unvisited old works survive without claiming we fetched them.
                db.execute("""INSERT INTO anikoto_catalog_members(run_id,source_record_id,known_at_start)
                    SELECT ?,source_record_id,1 FROM anikoto_catalog WHERE """ + catalog_condition(db, baseline[0]),
                           (run_id, baseline[0]))
        db.commit()
        fetched = 0
        try:
            while True:
                run = db.execute("SELECT * FROM anikoto_crawl_runs WHERE run_id=?", (run_id,)).fetchone()
                page = run["next_page"]
                html = fetch(listing_url(base_url, page))
                records, total_pages = parse_listing(html, base_url, page)
                policy = crawl_policy(db, run_id)
                expected = run["total_pages"]
                if policy["mode"] == "full" and expected is not None and expected != total_pages:
                    raise ValueError(f"Catalog pagination changed during scan: {expected} -> {total_pages}; use --fresh to restart")
                ids = [r["source_record_id"] for r in records]
                fingerprint = hashlib.sha256(json.dumps(sorted(ids)).encode()).hexdigest()
                if db.execute("SELECT 1 FROM anikoto_crawl_pages WHERE run_id=? AND record_fingerprint=?", (run_id, fingerprint)).fetchone():
                    raise ValueError(f"Page {page} repeated an earlier page; pagination may have been ignored")
                # Full scans reject shifted pages. Incremental pages can partly
                # overlap due to live/cache changes; keep distinct observations
                # and reject any page which makes no progress at all.
                overlap = [slug for slug in ids if db.execute("SELECT 1 FROM anikoto_catalog WHERE source_record_id=? AND last_seen_run=?", (slug, run_id)).fetchone()]
                if overlap and (policy["mode"] == "full" or len(overlap) == len(ids)):
                    raise ValueError(f"Page {page} overlapped earlier pages: {overlap[:3]}; use --fresh")
                all_known = all(db.execute("SELECT 1 FROM anikoto_catalog_members WHERE run_id=? AND source_record_id=? AND known_at_start=1",
                                           (run_id, slug)).fetchone() for slug in ids)
                streak = policy["known_page_streak"] + 1 if all_known else 0
                stop_known = policy["mode"] == "incremental" and streak >= 2
                timestamp = now()
                with db:
                    for row in records:
                        db.execute("""INSERT INTO anikoto_catalog VALUES (?,?,?,?,?)
                            ON CONFLICT(source_record_id) DO UPDATE SET payload_json=excluded.payload_json,
                            last_seen_at=excluded.last_seen_at,last_seen_run=excluded.last_seen_run""",
                            (row["source_record_id"], json.dumps(row, ensure_ascii=False, sort_keys=True), timestamp, timestamp, run_id))
                        db.execute("INSERT OR IGNORE INTO anikoto_catalog_members(run_id,source_record_id) VALUES (?,?)", (run_id, row["source_record_id"]))
                    db.execute("INSERT INTO anikoto_crawl_pages VALUES (?,?,?,?,?,?,?)", (run_id, page, timestamp,
                        hashlib.sha256(html.encode()).hexdigest(), fingerprint, len(ids), json.dumps(ids)))
                    done = page == total_pages or stop_known
                    db.execute("UPDATE anikoto_crawl_runs SET next_page=?,total_pages=?,status=?,finished_at=? WHERE run_id=?",
                        (page + 1, total_pages, "complete" if done else "running", timestamp if done else None, run_id))
                    db.execute("UPDATE anikoto_crawl_policy SET known_page_streak=?,stop_reason=? WHERE run_id=?",
                               (streak, "known_pages" if stop_known else "last_page" if done else None, run_id))
                fetched += 1
                print(json.dumps({"page": page, "total_pages": total_pages, "items": len(records), "run_id": run_id}), flush=True)
                if done:
                    break
                if max_pages and fetched >= max_pages:
                    db.execute("UPDATE anikoto_crawl_runs SET status='paused' WHERE run_id=?", (run_id,))
                    db.commit()
                    break
        except Exception as error:
            db.rollback()
            db.execute("UPDATE anikoto_crawl_runs SET status='failed',last_error=? WHERE run_id=?", (str(error), run_id))
            db.commit()
            raise
        return report(db, run_id)
    finally:
        db.close()


def export(db_path, output):
    output.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        latest = db.execute("SELECT run_id FROM anikoto_crawl_runs ORDER BY started_at DESC,rowid DESC LIMIT 1").fetchone()
        if not latest:
            raise ValueError("No crawl run to export")
        summary = report(db, latest[0])
        with (output / "anikoto-catalog.jsonl").open("w", encoding="utf-8") as out:
            for row in catalog_rows(db, latest[0]):
                out.write(row[1] + "\n")
        (output / "anikoto-crawl-report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--delay", type=float, default=1.2)
    parser.add_argument("--max-pages", type=int, default=0, help="Page budget; 0 runs until the overlap checkpoint or last page")
    parser.add_argument("--fresh", action="store_true", help="Restart from page one instead of resuming an interrupted run")
    parser.add_argument("--full", action="store_true", help="Ignore known-page checkpoints and scan the full listing")
    parser.add_argument("--export-dir", type=Path)
    args = parser.parse_args()
    if args.delay < 0 or args.max_pages < 0:
        parser.error("delay and max-pages must be nonnegative")
    try:
        summary = crawl(args.db, args.base_url, delay=args.delay, max_pages=args.max_pages, fresh=args.fresh, full=args.full)
    finally:
        if args.export_dir and args.db.exists():
            export(args.db, args.export_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

