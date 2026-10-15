#!/usr/bin/env python3
"""Review-pinned correction of two wrong ReAnime references, based on live db-v1."""
from __future__ import annotations
import argparse
import json
import sqlite3
import shutil
from pathlib import Path

from split_db_v1_release import (
    extract_database, pack_database, sha256, table_digest,
    validate_catalog_db, validate_registry_db
)
from reviewed_matching_guard import read_rules, verify_role

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "data/matching/reanime-corrections-20261015.json"
CAT = "anime_four_sources_multilingual_prepared.zip"
REG = "anime_reviewed_subtitle_registry_v1.zip"
LEDGER = "multiwork_review_decisions_20261015"


def apply(db, plan):
    db.row_factory = sqlite3.Row
    changes = []
    for change in plan["changes"]:
        source, rid = change["source"], change["id"]
        row = db.execute("SELECT * FROM entries WHERE source=? AND source_record_id=?", (source,rid)).fetchone()
        if row is None:
            raise ValueError(f"Missing original source work {source}:{rid}")
        cross = json.loads(row["cross_source_ids_json"])
        if cross.get("anilist_ids") != change["original_anilist_ids"]:
            raise ValueError(f"Protected AniList identity differs {source}:{rid}: {cross.get('anilist_ids')}")
        if cross.get(change["field"]) != change["before"]:
            raise ValueError(f"Prior ReAnime identities differ {source}:{rid}: {cross.get(change['field'])}")
        for target in change["after"]:
            if not db.execute("SELECT 1 FROM entries WHERE source='ReAnime' AND source_record_id=?", (target,)).fetchone():
                raise ValueError(f"Verified 2023 ReAnime target absent: {target}")
        cross[change["field"]] = change["after"]
        encoded=json.dumps(cross,ensure_ascii=False,separators=(",",":"))
        db.execute("UPDATE entries SET cross_source_ids_json=? WHERE source=? AND source_record_id=?",
                   (encoded,source,rid))
        changes.append({"source":source,"source_record_id":rid,"previous_reanime_ids":change["before"],
                        "updated_reanime_ids":change["after"]})
    return changes


def check_previous_review(db, plan):
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if LEDGER not in tables:
        raise ValueError("2026-10-15 reviewed work ledger missing from current db-v1")
    count=db.execute(f"SELECT COUNT(*) FROM {LEDGER}").fetchone()[0]
    if count!=plan["expected_review_ledger_rows"]:
        raise ValueError(f"Previously reviewed ledger is incomplete: {count}")
    states=dict(db.execute("SELECT origin_key,status FROM "+LEDGER))
    for change in plan["changes"]:
        origin=change["source"]+":"+change["id"]
        if origin not in states or states[origin] in {"전체보류","부분보류","공개대기"}:
            raise ValueError("Attempt to modify an unreviewed or held item: "+origin)
    return count


def build(input_dir,output_dir):
    plan=json.loads(PLAN.read_text(encoding="utf-8"))
    if plan.get("schema")!=1 or plan.get("batch")!="reanime-source-correction-20261015":
        raise ValueError("Unexpected correction plan")
    expected={CAT:plan["base_catalog_sha256"],REG:plan["base_registry_sha256"]}
    for name,digest in expected.items():
        if sha256(input_dir/name)!=digest:
            raise ValueError("Original db-v1 release changed; must re-review "+name)
    output_dir.mkdir(parents=True,exist_ok=True)
    path=output_dir/"corrected-catalog.db"
    reg=output_dir/"untouched-registry.db"
    extract_database(input_dir/CAT,path)
    extract_database(input_dir/REG,reg)
    rules=read_rules()
    with sqlite3.connect(reg) as db:
        verify_role(db,"registry",rules)
    registry=validate_registry_db(reg)
    with sqlite3.connect(path) as db:
        db.row_factory=sqlite3.Row
        verify_role(db,"catalog",rules)
        count=check_previous_review(db,plan)
        all_tables=[row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        table_hashes={table:table_digest(db,table) for table in all_tables if table!="entries"}
        protected_entries={(r["source"],r["source_record_id"]):tuple(r)
          for r in db.execute("SELECT * FROM entries WHERE NOT (source='Onnada' AND source_record_id='384') AND NOT (source='Anissia' AND source_record_id='2695')")}
        updates=apply(db,plan)
        # Exhaustive immutable-row assertion, not only row counts.
        after_entries={(r["source"],r["source_record_id"]):tuple(r)
          for r in db.execute("SELECT * FROM entries WHERE NOT (source='Onnada' AND source_record_id='384') AND NOT (source='Anissia' AND source_record_id='2695')")}
        if after_entries!=protected_entries:
            raise ValueError("Unrelated catalog entries were mutated")
        for table,digest in table_hashes.items():
            if table_digest(db,table)!=digest:
                raise ValueError("Protected table changed: "+table)
        verify_role(db,"catalog",rules)
        if db.execute("PRAGMA quick_check").fetchone()[0]!="ok" or db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Catalog integrity verification failed")
    catalog=validate_catalog_db(path)
    pack_database(path,output_dir/CAT,"anime_four_sources_multilingual_prepared.db")
    shutil.copyfile(input_dir/REG,output_dir/REG)
    if sha256(output_dir/REG)!=expected[REG]:
        raise ValueError("Registry was unexpectedly modified")
    report={"batch":plan["batch"],"reviewed_ledger_rows":count,
            "source_link_changes":updates,"catalog":catalog,"registry":registry,
            "baseline_sha256":expected,
            "result_sha256":{name:sha256(output_dir/name) for name in (CAT,REG)},
            "new_episode_assignments":0,"preserves_all_other_entry_rows":True,
            "preserves_other_catalog_tables":True,
            "preserves_registry_bitwise":True}
    (output_dir/"reanime-corrections-validation-20261015.json").write_text(
       json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False))
    return report


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--input",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    a=p.parse_args()
    build(a.input,a.output)
