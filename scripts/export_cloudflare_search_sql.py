#!/usr/bin/env python3
"""Export a conservative, read-only anime search index to an existing Cloudflare D1 schema.

Inputs: reviewed four-source catalog SQLite + reviewed subtitle registry SQLite.
Outputs: UTF-8 SQL import and JSON manifest; never changes source DBs.
Only exact source IDs and confirmed registry targets are connected. No title-based joins.
"""
import argparse
import hashlib
import json
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote

SOURCES = {"AniList", "ReAnime", "Onnada", "Anissia"}
LABELS = {"Anissia": "애니시아", "aniall": "AniAll", "naver": "네이버"}
TITLES = ("title_ko", "title_native", "title_romaji", "title_english")


def normalized_title(value):
    value = unicodedata.normalize("NFKC", str(value or "")).lower()
    return "".join(c for c in value if unicodedata.category(c)[0] in "LN" or c in "+☆★")


def printable(value):
    return str(value or "").replace("\x00", "").strip()


def number(value):
    try:
        return max(0, int(value))
    except (ValueError, TypeError):
        return 0


def read_only(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        db.close()
        raise ValueError(f"SQLite integrity check failed: {path}")
    return db


def table_exists(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('view','table') AND name=?", (name,)).fetchone() is not None


def registry_rows(db):
    # The reviewed registry is a separate release from the four-source catalog.
    if not table_exists(db, "subtitle_runtime_metadata"):
        raise ValueError("Not a reviewed subtitle registry")
    meta = {k: json.loads(v) for k, v in db.execute("SELECT key,value FROM subtitle_runtime_metadata")}
    if meta.get("format_version") != 1 or meta.get("completed_bundles") != 1109:
        raise ValueError("Reviewed subtitle registry format or completion differs from validated v6")

    for relation in ("subtitle_runtime_files_export", "subtitle_runtime_files"):
        if table_exists(db, relation):
            columns = {str(row[1]) for row in db.execute(f'PRAGMA table_info("{relation}")')}
            needed = {"pid", "source", "target_source", "target_id", "work_confirmed", "work_title_ko"}
            if not needed <= columns:
                raise ValueError(f"Missing required confirmed fields in {relation}: {needed - columns}")
            return db.execute(f'''SELECT DISTINCT pid,source,target_source,target_id,work_title_ko
                FROM "{relation}" WHERE work_confirmed=1
                  AND source IN ('aniall','naver')
                  AND target_id IS NOT NULL AND target_id != ''
                  AND target_source IN ('AniList','ReAnime','Onnada','Anissia')
            ''').fetchall()
    # Keep the export strict. Guessing from old archive names can merge seasons.
    raise ValueError("Missing reviewed subtitle runtime files relation")


def identity(source, record_id):
    if source == "Anissia":
        return "ani_" + record_id, "/anime/ani_" + quote(record_id, safe="")
    if source == "ReAnime":
        return "re_" + record_id, "/anime/re_" + quote(record_id, safe="")
    return "db_" + source + "_" + record_id, "/catalog/" + source + "/" + quote(record_id, safe="")


def build(catalog_db, registry_db):
    entries = {}
    counts = Counter()
    for raw in catalog_db.execute("SELECT * FROM entries"):
        row = dict(raw)
        source, rid = printable(row.get("source")), printable(row.get("source_record_id"))
        if not rid or source not in SOURCES:
            continue
        entries[(source, rid)] = row
        counts[source] += 1
    if any(counts[source] == 0 for source in SOURCES):
        raise ValueError(f"Incomplete four-source DB: {dict(counts)}")

    confirmed = defaultdict(lambda: {"files": set(), "titles": set(), "providers": set()})
    for raw in registry_rows(registry_db):
        row = dict(raw)
        key = (printable(row["target_source"]), printable(row["target_id"]))
        if key not in entries:
            # The Pi catalog route relies on source records; don't create dead links.
            continue
        item = confirmed[key]
        item["files"].add(printable(row["pid"]))
        item["providers"].add(printable(row["source"]))
        title = printable(row["work_title_ko"])
        if title:
            item["titles"].add(title)

    works = []
    aliases = set()
    for (source, rid), row in sorted(entries.items()):
        key = (source, rid)
        if source != "Anissia" and key not in confirmed:
            continue
        verified = confirmed.get(key)
        from_registry = verified["titles"] if verified else set()
        # Multiple different registry titles may signal a work/season conflict:
        # keep the canonical source title instead of choosing arbitrarily.
        reviewed_title = next(iter(from_registry)) if len(from_registry) == 1 else ""
        main_title = printable(row.get("title_ko"))
        title = reviewed_title if (reviewed_title and any('가' <= c <= '힣' for c in reviewed_title)
                    and not any('가' <= c <= '힣' for c in main_title)) else main_title or reviewed_title
        title = title or next((printable(row.get(k)) for k in TITLES if printable(row.get(k))), "")
        if not title:
            continue
        work_id, detail = identity(source, rid)
        providers = {LABELS.get(source, source)} if source == "Anissia" else set()
        if verified:
            providers.update(LABELS.get(p, p) for p in verified["providers"])
        caption_count = len(verified["files"]) if verified else 0
        if source == "Anissia":
            try:
                metadata = json.loads(row.get("source_metadata_json") or "{}")
            except (TypeError, ValueError):
                metadata = {}
            caption_count += number(metadata.get("captionCount"))
        title_set = {printable(row.get(field)) for field in TITLES} | {title}
        if reviewed_title:
            title_set.add(reviewed_title)
        for candidate in title_set:
            norm = normalized_title(candidate)
            if norm and len(norm) <= 300:
                aliases.add((work_id, norm))
        works.append((work_id, title, number(row.get("year")) or None,
                      " · ".join(sorted(providers)), caption_count, detail, ""))
    if len(works) < 1000 or len(aliases) < len(works):
        raise ValueError(f"Suspiciously small export: {len(works)} works / {len(aliases)} aliases")
    return works, sorted(aliases), dict(counts), len(confirmed)


def literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def emit_sql(works, aliases, output):
    with open(output, "w", encoding="utf-8", newline="\n") as fp:
        fp.write("-- Reviewed four-source + confirmed AniAll/Naver search projection.\n")
        fp.write("-- No explicit transaction statements: Wrangler handles D1 imports.\n")
        fp.write("DELETE FROM work_aliases;\nDELETE FROM works;\n")
        for item in works:
            fp.write("INSERT INTO works (id,title,year,source_name,caption_count,detail_url,poster) VALUES ("
                     + ",".join(map(literal, item)) + ");\n")
        for item in aliases:
            fp.write("INSERT INTO work_aliases (work_id,alias_norm) VALUES ("
                     + ",".join(map(literal, item)) + ");\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--max-rows", type=int, default=35000,
                        help="Safety stop for estimated D1 free-tier writes (not an exact billable count)")
    args = parser.parse_args()
    with read_only(args.catalog) as catalog, read_only(args.registry) as registry:
        works, aliases, source_counts, confirmed_count = build(catalog, registry)
    estimated = len(works) + len(aliases)
    if args.max_rows < 1 or estimated > args.max_rows:
        raise SystemExit(f"Refusing publish: {estimated} inserted rows exceed --max-rows={args.max_rows}; review D1 free-tier write limits")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    emit_sql(works, aliases, args.output)
    manifest = {"works": len(works), "aliases": len(aliases), "inserted_rows": estimated,
                "source_counts": source_counts, "confirmed_source_targets": confirmed_count,
                "sql_sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
                "sql_bytes": args.output.stat().st_size}
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
