"""Validate and apply exact-ID Korean names from the Pi monthly snapshot."""
import re

ID = re.compile(r"^(?:ani_[1-9]\d*|re_[A-Za-z0-9_-]+|ak_[A-Za-z0-9_-]+|db_(?:AniList|Onnada)_[A-Za-z0-9_-]+)$")


def checked_titles(rows):
    if not isinstance(rows, list) or len(rows) > 30000:
        raise ValueError("Invalid manual Korean title list")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) not in ({"anime_id", "title"}, {"anime_id", "canonical_id", "title"}):
            raise ValueError("Unexpected manual Korean title fields")
        aid, title = row["anime_id"], row["title"]
        canonical=row.get("canonical_id",aid)
        if (not isinstance(aid, str) or len(aid) > 150 or not ID.fullmatch(aid)
                or not isinstance(canonical,str) or len(canonical)>150 or not ID.fullmatch(canonical)
                or aid in seen or not isinstance(title, str) or not 0 < len(title) <= 300
                or not re.search("[가-힣]", title) or any(ord(c) < 32 or ord(c) == 127 for c in title)):
            raise ValueError("Invalid or conflicting manual Korean title")
        seen.add(aid)
    return rows


def apply_titles(db, rows):
    rows = checked_titles(rows)
    # Validate the complete batch before changing either the overlay or source labels.
    for row in rows:
        aid = row["anime_id"]
        if aid.startswith("ak_"):
            found = db.execute("SELECT 1 FROM anikoto_catalog WHERE source_record_id=?", (aid[3:],)).fetchone()
        else:
            source, source_id = decode(aid)
            found = db.execute("SELECT 1 FROM entries WHERE source=? AND source_record_id=?", (source, source_id)).fetchone()
        if not found:
            raise ValueError("Manual Korean title ID absent from catalog: " + aid)
    db.execute("""CREATE TABLE IF NOT EXISTS pi_admin_work_titles (
        anime_id TEXT PRIMARY KEY, title TEXT NOT NULL, canonical_id TEXT NOT NULL
    )""")
    for row in rows:
        aid, title = row["anime_id"], row["title"]
        db.execute("INSERT INTO pi_admin_work_titles VALUES (?,?,?) ON CONFLICT(anime_id) DO UPDATE SET title=excluded.title,canonical_id=excluded.canonical_id",
                   (aid, title,row.get('canonical_id',aid)))
        # Core labels feed both the Korean catalog builder and Cloudflare D1 export.
        # Anikoto's mirror payload is preserved; the exact-ID overlay is imported by Pi.
        if not aid.startswith("ak_"):
            source, source_id = decode(aid)
            db.execute("UPDATE entries SET title_ko=? WHERE source=? AND source_record_id=?", (title, source, source_id))
    return len(rows)


def decode(aid):
    if aid.startswith("ani_"): return "Anissia", aid[4:]
    if aid.startswith("re_"): return "ReAnime", aid[3:]
    _, source, source_id = aid.split("_", 2)
    return source, source_id


def assert_titles(db, rows):
    for row in checked_titles(rows):
        found = db.execute("SELECT title FROM pi_admin_work_titles WHERE anime_id=?", (row["anime_id"],)).fetchone()
        if not found or found[0] != row["title"]:
            raise ValueError("Release lacks manual Korean title: " + row["anime_id"])
        if not row["anime_id"].startswith("ak_"):
            source, source_id = decode(row["anime_id"])
            found = db.execute("SELECT title_ko FROM entries WHERE source=? AND source_record_id=?", (source, source_id)).fetchone()
            if not found or found[0] != row["title"]:
                raise ValueError("Release source label differs from manual Korean title")
