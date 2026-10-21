#!/usr/bin/env python3
"""Fail-closed guard for the reviewed 2026-10-11 work/file/episode decisions.

The ledger records exact source IDs and reviewed targets; it never guesses by title
and never writes to a production database. Monthly refresh additionally snapshots
the *input* manual decisions so later user edits remain authoritative.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import gzip
import json
from pathlib import Path
import sqlite3
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RULES = ROOT / "data/matching/reviewed-corrections-20261011.json"
MANUAL_TABLES = (
    "anissia_year_match",
    "anikoto_work_links",
    "anikoto_work_link_targets",
    "packaged_files_v6",
    "subtitle_confirmed_episodes",
    "body_audit_confirmed_episodes_v1",
    "subtitle_review_details",
    "subsubsub_file_registry_v1",
    "subsubsub_file_targets_v1",
)


def read_rules(path=DEFAULT_RULES):
    rules = json.loads(Path(path).read_text(encoding="utf-8"))
    amendment = ROOT / 'data/matching/reviewed-corrections-multimatch-20261021.json.gz.b64'
    if Path(path).resolve() == DEFAULT_RULES.resolve() and amendment.exists():
        revised = json.loads(gzip.decompress(base64.b64decode(amendment.read_bytes())))
        original_hash=hashlib.sha256(json.dumps(rules,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if revised.get('base_guard_semantic_sha256')!=original_hash or revised.get('amendments_multimatch_20261021',{}).get('batch')!='multimatch-20261021':
            raise ValueError('Reviewed ledger changed; reconcile the multimatch amendment before refreshing')
        rules=revised
    if (rules.get("schema"), rules.get("batch"), rules.get("new_episode_assignments")) != (
        1, "matching-20261011", 0
    ):
        raise ValueError("Unexpected reviewed matching rules version/batch/episode assignment")
    for role, tables in rules["roles"].items():
        if role not in ("catalog", "registry"):
            raise ValueError("Unsupported database role")
        for name, definition in tables.items():
            seen = set()
            for rule in definition["rows"]:
                key = tuple(rule["key"].get(k) for k in definition["primary_key"])
                if None in key or key in seen:
                    raise ValueError(f"Duplicate or invalid {name} reviewed key: {key}")
                seen.add(key)
                if rule.get("absent", False) == ("expected" in rule):
                    raise ValueError(f"Expected one of absent/expected in {name} {key}")
    return rules


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def existing_tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def row_for(db, table, key):
    where = " AND ".join(f"{quote(k)}=?" for k in key)
    cur = db.execute(f"SELECT * FROM {quote(table)} WHERE {where}", tuple(key.values()))
    row = cur.fetchone()
    return dict(zip((col[0] for col in cur.description), row)) if row else None


def normalize(column, value):
    if column.endswith("_json") and isinstance(value, str):
        return json.loads(value)
    return value


def verify_role(db, role, rules=None):
    """Verify exact targets, hold/null decisions and removed wrong links.

    This intentionally does not modify records or assign episodes. Changing a
    reviewed decision requires a consciously updated version of the ledger.
    """
    rules = rules or read_rules()
    errors = []
    available = existing_tables(db)
    checked = 0
    for table, definition in rules["roles"][role].items():
        if table not in available:
            errors.append(f"missing table {table}")
            continue
        for decision in definition["rows"]:
            key = decision["key"]
            row = row_for(db, table, key)
            checked += 1
            if decision.get("absent"):
                if row is not None:
                    errors.append(f"{table}:{key} previously rejected link reappeared")
            elif row is None:
                errors.append(f"{table}:{key} reviewed row disappeared")
            else:
                for column, expected in decision["expected"].items():
                    actual = normalize(column, row.get(column))
                    if actual != expected:
                        errors.append(f"{table}:{key} {column} changed")
            if len(errors) >= 15:
                break
        if len(errors) >= 15:
            break
    if errors:
        raise ValueError("Reviewed matching regression: " + "; ".join(errors))
    return checked


def reviewed_source_ids(db):
    """Source IDs that have explicitly reviewed mappings (including holds)."""
    reviewed = set()
    if "anissia_year_match" in existing_tables(db):
        reviewed.update(str(row[0]) for row in db.execute("SELECT anime_no FROM anissia_year_match"))
    return reviewed


def is_reviewed_source(source, source_id, prior, reviewed_anissia):
    """An incoming provider hint must never append to an approved association."""
    if not prior:
        return False
    metadata = json.loads(prior.get("source_metadata_json") or "{}")
    return (bool(metadata.get("reviewed_matching_20261011"))
            or source == "Anissia" and str(source_id) in reviewed_anissia)


def capture_reviewed_state(db):
    """Capture pre-refresh manual choices; do not pin newer manual edits to 2026 values."""
    tables = existing_tables(db)
    snapshot = {}
    for table in MANUAL_TABLES:
        if table in tables:
            # Source-refresh is not allowed to change any manual file/episode rows.
            snapshot[table] = db.execute(f"SELECT * FROM {quote(table)} ORDER BY rowid").fetchall()
    if "entries" in tables:
        reviewed = reviewed_source_ids(db)
        result = {}
        for source, rid, cross, status, metadata in db.execute(
                "SELECT source,source_record_id,cross_source_ids_json,existing_match_status,source_metadata_json "
                "FROM entries"):
            prior = {"source_metadata_json": metadata}
            if is_reviewed_source(source, rid, prior, reviewed):
                result[(source, rid)] = (json.loads(cross or "{}"), status)
        snapshot["reviewed_entries"] = result
    return snapshot


def assert_reviewed_state(db, before):
    tables = existing_tables(db)
    for table, rows in before.items():
        if table == "reviewed_entries":
            reviewed = reviewed_source_ids(db)
            actual = {}
            for source, rid, cross, status, metadata in db.execute(
                    "SELECT source,source_record_id,cross_source_ids_json,existing_match_status,source_metadata_json "
                    "FROM entries"):
                if (source, rid) in rows:
                    actual[(source, rid)] = (json.loads(cross or "{}"), status)
            if actual != rows:
                raise ValueError("Existing reviewed source links/status changed during refresh")
        else:
            if table not in tables or db.execute(
                    f"SELECT * FROM {quote(table)} ORDER BY rowid").fetchall() != rows:
                raise ValueError(f"Manual review/episode table changed during refresh: {table}")


def verify_archive(path, role, rules):
    from split_db_v1_release import extract_database
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "reviewed.db"
        if Path(path).suffix == ".zip":
            extract_database(Path(path), db_path)
        elif str(path).endswith(".gz"):
            with gzip.open(path, "rb") as src:
                db_path.write_bytes(src.read())
        else:
            db_path.write_bytes(Path(path).read_bytes())
        with sqlite3.connect(db_path) as db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError(f"Bad {role} SQLite integrity")
            return verify_role(db, role, rules)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    parser.add_argument("--catalog", type=Path, required=True, help="Catalog ZIP, SQLite or gzip snapshot")
    parser.add_argument("--registry", type=Path, required=True, help="Registry ZIP or SQLite")
    args = parser.parse_args()
    rules = read_rules(args.rules)
    total = {role: verify_archive(path, role, rules) for role, path in (
        ("catalog", args.catalog), ("registry", args.registry))}
    print(json.dumps({"review_batch": rules["batch"], "verified_rows": total,
                      "new_episode_assignments": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()

