#!/usr/bin/env python3
"""Fail-closed review-ledger and explicitly approved source-link updates for db-v1."""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
import shutil
import sqlite3

from reviewed_matching_guard import read_rules, verify_role
from split_db_v1_release import (
    extract_database, pack_database, sha256, table_digest,
    validate_catalog_db, validate_registry_db,
)

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "data/matching/multiwork-release-patch-20261015.json"
CAT = "anime_four_sources_multilingual_prepared.zip"
REG = "anime_reviewed_subtitle_registry_v1.zip"
TABLE = "multiwork_review_decisions_20261015"


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as fd:
        return list(csv.DictReader(fd))


def checked_review(root, plan):
    all_rows = read_csv(root / plan["reference"])
    decisions = read_csv(root / plan["decisions"])
    if len(all_rows) != 236 or len(decisions) != 54:
        raise ValueError(f"Review CSV row count unexpected: {len(all_rows)}, {len(decisions)}")
    positions = [int(r["검수행"]) for r in all_rows]
    if positions != list(range(1, 237)):
        raise ValueError("Review ordinal must be strictly 1..236")
    index = {r["원본키"]: r for r in all_rows}
    if len(index) != 236 or len({r["원본키"] for r in decisions}) != 54:
        raise ValueError("Repeated source key in review input")
    for r in all_rows:
        if not r["릴리즈원본번호"].isdigit() or ":" not in r["원본키"]:
            raise ValueError("Invalid original release ID")
    for r in decisions:
        old = index.get(r["원본키"])
        if old is None or (r["릴리즈원본번호"], r["검수행"]) != (
            old["릴리즈원본번호"], old["검수행"]
        ):
            raise ValueError(f"Decision/release key mismatch: {r['원본키']}")
        if r["상태"] != old["상태"]:
            raise ValueError(f"Decision status differs from full review: {r['원본키']}")
    fixes = set()
    for fix in plan["targeted"]:
        origin = fix["source"] + ":" + fix["id"]
        if origin != fix["origin"] or origin not in index or origin in fixes:
            raise ValueError(f"Invalid targeted origin: {origin}")
        fixes.add(origin)
        if index[origin]["상태"] not in ("수정확정", "연결방침확정"):
            raise ValueError(f"Unapproved/held correction: {origin}")
        if fix["field"] not in ("anilist_ids", "reanime_ids"):
            raise ValueError(f"Unsafe cross-source field: {origin}")
        if set(fix["remove"]) & set(fix["keep"]):
            raise ValueError(f"Overlap remove/keep: {origin}")
    return all_rows, decisions


def make_ledger(db, all_rows, decisions):
    detailed = {r["원본키"]: r for r in decisions}
    db.execute(f"""CREATE TABLE {TABLE} (
      origin_key TEXT PRIMARY KEY,
      review_ordinal INTEGER NOT NULL UNIQUE,
      original_release_number INTEGER NOT NULL,
      work_title TEXT NOT NULL,
      original_targets TEXT NOT NULL,
      status TEXT NOT NULL,
      decision_note TEXT NOT NULL,
      episode_guidance TEXT NOT NULL,
      keep_targets TEXT NOT NULL,
      remove_targets TEXT NOT NULL,
      review_reference TEXT NOT NULL
    )""")
    payload = []
    for r in all_rows:
        d = detailed.get(r["원본키"], r)
        payload.append((
            r["원본키"], int(r["검수행"]), int(r["릴리즈원본번호"]),
            r["원본작품명"], r["릴리즈기존연결작품ID"], r["상태"],
            d["처리지시"], d["회차_순서"], d["유지_연결ID"],
            d["제거_연결ID"], "multiwork-reviewed-20261015",
        ))
    db.executemany(f"INSERT INTO {TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?)", payload)
    if db.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0] != 236:
        raise ValueError("Review ledger not fully loaded")
    return dict(Counter(r["상태"] for r in all_rows))


def patch_catalog(db, fixes):
    changes = []
    for fix in fixes:
        source, rid, field = fix["source"], fix["id"], fix["field"]
        row = db.execute(
            "SELECT cross_source_ids_json FROM entries WHERE source=? AND source_record_id=?",
            (source, rid)
        ).fetchone()
        if row is None:
            raise ValueError(f"Source row missing from published release: {source}:{rid}")
        cross = json.loads(row[0] or "{}")
        old = [str(v) for v in cross.get(field, [])]
        allowed = set(fix["remove"]) | set(fix["keep"])
        if len(old) != len(set(old)) or any(x not in allowed for x in old):
            raise ValueError(f"Unexpected existing link(s) for {source}:{rid}: {old}")
        target_type = "AniList" if field == "anilist_ids" else "ReAnime"
        for target in fix["keep"]:
            found = db.execute(
                "SELECT 1 FROM entries WHERE source=? AND source_record_id=?",
                (target_type, target)
            ).fetchone()
            if found is None:
                raise ValueError(f"Reviewed target absent from release: {target_type}:{target}")
        updated = [x for x in old if x not in fix["remove"]]
        for x in fix["keep"]:
            if x not in updated:
                updated.append(x)
        if not updated:
            raise ValueError(f"Empty corrected link: {source}:{rid}")
        cross[field] = updated
        after = json.dumps(cross, ensure_ascii=False, separators=(",", ":"))
        db.execute(
            "UPDATE entries SET cross_source_ids_json=? WHERE source=? AND source_record_id=?",
            (after, source, rid),
        )
        changes.append({"source": source, "id": rid, "field": field, "before": old, "after": updated})
    return changes


def build(inputs: Path, output: Path):
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    if plan.get("schema") != 1 or plan.get("batch") != "multiwork-20261015":
        raise ValueError("Unrecognized patch plan")
    if set(plan["expected_assets"]) != {CAT, REG}:
        raise ValueError("Must pin both existing db-v1 release ZIPs")
    for name, expected in plan["expected_assets"].items():
        actual = sha256(inputs / name)
        if actual != expected:
            raise ValueError(f"Published release SHA mismatch for {name}: {actual}")
    all_rows, decisions = checked_review(ROOT, plan)
    output.mkdir(parents=True, exist_ok=True)
    catalog_db = output / "anime_four_sources_multilingual_prepared.db"
    registry_db = output / "anime_reviewed_subtitle_registry_v1.db"
    extract_database(inputs / CAT, catalog_db)
    extract_database(inputs / REG, registry_db)
    rules = read_rules()
    with sqlite3.connect(registry_db) as registry:
        verify_role(registry, "registry", rules)
    registry_validation = validate_registry_db(registry_db)
    with sqlite3.connect(catalog_db) as db:
        verify_role(db, "catalog", rules)
        db.row_factory = sqlite3.Row
        original_tables = [
            x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        ]
        if TABLE in original_tables:
            raise ValueError("Review ledger already exists: refusing replay")
        frozen = {t: table_digest(db, t) for t in original_tables if t != "entries"}
        statuses = make_ledger(db, all_rows, decisions)
        changes = patch_catalog(db, plan["targeted"])
        if len(changes) != len(plan["targeted"]):
            raise ValueError("Incomplete corrections")
        for name, before in frozen.items():
            if table_digest(db, name) != before:
                raise ValueError(f"Unapproved table changed: {name}")
        verify_role(db, "catalog", rules)
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("SQLite catalog integrity failed")
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Foreign key integrity failed")
    catalog_validation = validate_catalog_db(catalog_db)
    pack_database(catalog_db, output / CAT, catalog_db.name)
    # No subtitle file, episode, registry or Pi-manual decision may be changed.
    shutil.copyfile(inputs / REG, output / REG)
    if sha256(output / REG) != plan["expected_assets"][REG]:
        raise ValueError("Registry asset mutated")
    hashes = {name: sha256(output / name) for name in (CAT, REG)}
    report = {
        "batch": plan["batch"], "review_records": len(all_rows),
        "explicit_decision_records": len(decisions), "statuses": statuses,
        "applied_source_link_corrections": changes,
        "new_episode_assignments": 0,
        "catalog": catalog_validation, "registry": registry_validation,
        "baseline_sha256": plan["expected_assets"], "result_sha256": hashes,
        "limitations": "Existing secondary mapping tables unchanged; only explicit entries links and review ledger are written.",
    }
    (output / "multiwork-validation-20261015.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.input, args.output)
