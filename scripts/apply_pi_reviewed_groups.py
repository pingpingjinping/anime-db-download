#!/usr/bin/env python3
"""Fail-closed monthly reviewed-identity overlay patch for db-v1 catalog ZIP.

Never edits source records or inferred match columns. Only a versioned overlay
table is changed, with every member checked against the immutable source IDs.
"""
import argparse
import gzip
import hashlib
import io
import json
import os
import pathlib
import re
import sqlite3
import tempfile
import zipfile

ID = re.compile(r"^(?:ani_[1-9]\d*|re_[A-Za-z0-9_-]+|ak_[A-Za-z0-9_-]+|db_(?:AniList|Onnada)_[A-Za-z0-9_-]+)$")
KINDS = {"main", "ova", "special", "movie", "preview", "drama", "unknown"}

def decode(member):
    if member.startswith("ani_"): return "Anissia", member[4:]
    if member.startswith("re_"): return "ReAnime", member[3:]
    if member.startswith("ak_"): return "Anikoto", member[3:]
    pieces = member.split("_",2)
    return pieces[1],pieces[2]


def checked_manifest(payload, month):
    if not isinstance(payload,dict) or payload.get("schema") != 1 or payload.get("month") != month:
        raise ValueError("Unexpected reviewed manifest schema/month")
    groups = payload.get("groups")
    if not isinstance(groups,list) or len(groups)>10000 or payload.get("count") != len(groups):
        raise ValueError("Manifest group counts are invalid")
    used = {}
    for group in groups:
        if not isinstance(group,dict): raise ValueError("Invalid group")
        members = group.get("members")
        if not isinstance(members,list) or not 2<=len(members)<=12 or any(
                not isinstance(m,str) or not ID.fullmatch(m) for m in members):
            raise ValueError("Invalid reviewed group source IDs")
        if members != sorted(set(members)):
            raise ValueError("Reviewed group members must be sorted and unique")
        expected_id = hashlib.sha256("\n".join(members).encode()).hexdigest()[:32]
        if group.get("id") != expected_id:
            raise ValueError("Reviewed group checksum mismatch")
        sources=[decode(m)[0] for m in members]
        if len(sources) != len(set(sources)):
            raise ValueError("Multiple same-provider identities within a reviewed group")
        if group.get("kind") not in KINDS or type(group.get("season")) is not int or not 1<=group["season"]<=99:
            raise ValueError("Invalid work kind or season")
        if not isinstance(group.get("title"),str) or not 0<len(group["title"])<=300:
            raise ValueError("Invalid work title")
        for member in members:
            if member.startswith("ak_"):
                continue  # A single reviewed Anikoto URL can cover several seasons.
            if member in used and used[member] != group["id"]:
                raise ValueError("Cross-group conflict for " + member)
            used[member] = group["id"]
    return groups


def apply_overlay(db, groups, month):
    original = {r[0]:r[1] for r in db.execute("SELECT source,COUNT(*) FROM entries GROUP BY source")}
    if not {"AniList","Onnada","ReAnime","Anissia"}.issubset(original):
        raise ValueError("Invalid catalog sources")
    table_names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    all_refs = {decode(m) for group in groups for m in group["members"]}
    for src,id in all_refs:
        if src == "Anikoto":
            if "anikoto_catalog" not in table_names or not db.execute(
                "SELECT 1 FROM anikoto_catalog WHERE source_record_id=?", (id,)
            ).fetchone():
                raise ValueError("Reviewed Anikoto ID absent from published catalog: " + id)
        elif not db.execute(
            "SELECT 1 FROM entries WHERE source=? AND source_record_id=?", (src,id)
        ).fetchone():
            raise ValueError("Reviewed ID absent from published catalog: " + src + ":" + id)
    db.execute("""CREATE TABLE IF NOT EXISTS pi_reviewed_work_groups (
        group_id TEXT PRIMARY KEY,
        reviewed_month TEXT NOT NULL,
        title TEXT NOT NULL,
        kind TEXT NOT NULL,
        season INTEGER NOT NULL,
        members_json TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS pi_reviewed_work_groups_meta (
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    )""")
    previous = db.execute("SELECT COUNT(*) FROM pi_reviewed_work_groups").fetchone()[0]
    # The Pi exports the FULL reviewed work set. A sudden decrease means an
    # accidental deletion/reversion, never silently reduce the release database.
    if len(groups) < previous:
        raise ValueError("Reviewed group count shrank from %d to %d" % (previous,len(groups)))
    with db:
        db.execute("DELETE FROM pi_reviewed_work_groups")
        db.executemany("INSERT INTO pi_reviewed_work_groups VALUES (?,?,?,?,?,?)",[
            (g["id"],month,g["title"],g["kind"],g["season"],
             json.dumps(g["members"],ensure_ascii=False,separators=(",",":"))) for g in groups
        ])
        db.execute("INSERT INTO pi_reviewed_work_groups_meta VALUES('reviewed_month',?)"
                   " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (month,))
    after = {r[0]:r[1] for r in db.execute("SELECT source,COUNT(*) FROM entries GROUP BY source")}
    if after != original:
        raise ValueError("Source catalog rows changed during reviewed overlay merge")
    return len(groups)


def patch_archive(archive, manifest_path, month):
    manifest = json.loads(pathlib.Path(manifest_path).read_text(encoding="utf-8"))
    groups = checked_manifest(manifest,month)
    archive = pathlib.Path(archive)
    with tempfile.TemporaryDirectory(prefix="reviewed-monthly-") as root:
        root = pathlib.Path(root)
        with zipfile.ZipFile(archive) as src:
            filenames = [name for name in src.namelist() if name.lower().endswith((".db",".db.gz",".sqlite"))]
            if len(filenames) != 1: raise ValueError("Catalog archive must contain one SQLite database")
            filename = filenames[0]
            compressed = src.read(filename)
            is_gzip = compressed.startswith(b"\x1f\x8b")
            (root/"catalog.db").write_bytes(gzip.decompress(compressed) if is_gzip else compressed)
        db = sqlite3.connect(root/"catalog.db")
        try:
            if db.execute("PRAGMA quick_check").fetchone()[0]!="ok":
                raise ValueError("Existing catalog failed integrity check")
            result = apply_overlay(db,groups,month)
            if db.execute("PRAGMA quick_check").fetchone()[0]!="ok":
                raise ValueError("Reviewed catalog failed integrity check")
        finally:
            db.close()
        raw = (root/"catalog.db").read_bytes()
        if is_gzip:
            stream=io.BytesIO()
            with gzip.GzipFile(fileobj=stream,mode="wb",mtime=0,compresslevel=6) as out:
                out.write(raw)
            raw=stream.getvalue()
        output=root/"new.zip"
        with zipfile.ZipFile(archive) as before,zipfile.ZipFile(output,"w") as after:
            for entry in before.infolist():
                after.writestr(entry,raw if entry.filename==filename else before.read(entry))
        # Only replace fully validated release archive, never a partial update.
        os.replace(output,archive)
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--archive",type=pathlib.Path,required=True)
    parser.add_argument("--manifest",type=pathlib.Path,required=True)
    parser.add_argument("--month",required=True)
    args=parser.parse_args()
    print("Applied %d reviewed work groups for %s" %
          (patch_archive(args.archive,args.manifest,args.month),args.month))


if __name__=="__main__": main()
