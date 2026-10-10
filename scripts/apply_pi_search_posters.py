#!/usr/bin/env python3
"""Merge Pi's path-free verified artwork cache into the month-end SQLite ZIP.

This only adds/replaces a separate artwork URL lookup table; original anime
source records, title mappings and subtitle archive contents stay unchanged.
"""
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
from urllib.parse import urlsplit

ID=re.compile(r"^(?:ani_[1-9]\d*|re_[A-Za-z0-9_-]+|ak_[A-Za-z0-9_-]+|db_(?:AniList|Onnada)_[A-Za-z0-9_-]+)$")
SOURCES={"ReAnime","AniList","Anikoto"}

def record_id(work):
    if work.startswith("ani_"):return ("Anissia",work[4:])
    if work.startswith("re_"):return ("ReAnime",work[3:])
    if work.startswith("ak_"):return ("Anikoto",work[3:])
    _,source,record=work.split("_",2)
    return source,record


def validate(rows):
    if not isinstance(rows,list) or len(rows)>30000:raise ValueError("Invalid Pi poster-cache size")
    found={}
    for row in rows:
        if not isinstance(row,dict) or set(row)!={"work_id","poster","source"}:
            raise ValueError("Unexpected Pi cached artwork fields")
        id,poster,source=row["work_id"],row["poster"],row["source"]
        if not isinstance(id,str) or len(id)>180 or not ID.fullmatch(id):
            raise ValueError("Invalid work ID in Pi artwork cache")
        if source not in SOURCES or not isinstance(poster,str) or len(poster)>2048:
            raise ValueError("Invalid Pi artwork cache source or URL")
        uri=urlsplit(poster)
        if uri.scheme!="https" or not uri.hostname or uri.username or uri.password:
            raise ValueError("Non-HTTPS or credentialed cached artwork URL")
        if id in found and found[id]!=(poster,source):
            raise ValueError("Two different cached posters for the same exact work ID")
        found[id]=(poster,source)
    return found


def insert(db,rows):
    found=validate(rows)
    names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    accepted={}
    for work,(poster,source) in found.items():
        provider,source_id=record_id(work)
        if provider=="Anikoto":
            # Anikoto source is a video mirror, not itself a D1 searchable work.
            if "anikoto_catalog" not in names:
                continue
            ok=db.execute("SELECT 1 FROM anikoto_catalog WHERE source_record_id=?",(source_id,)).fetchone()
        else:
            ok=db.execute("SELECT 1 FROM entries WHERE source=? AND source_record_id=?",
                          (provider,source_id)).fetchone()
        if ok: accepted[work]=(poster,source)
    with db:
        db.execute("""CREATE TABLE IF NOT EXISTS pi_search_poster_cache(
            work_id TEXT PRIMARY KEY, poster TEXT NOT NULL, source TEXT NOT NULL
        )""")
        # Preserve earlier monthly posters (URLs only, nothing private) until a
        # user refreshes them on Pi. Newly cached entries replace older entries.
        db.executemany("""INSERT INTO pi_search_poster_cache VALUES(?,?,?)
            ON CONFLICT(work_id) DO UPDATE SET poster=excluded.poster,source=excluded.source""",
            [(k,*v) for k,v in sorted(accepted.items())])
    return len(accepted)


def patch_archive(archive,rows):
    archive=pathlib.Path(archive)
    with tempfile.TemporaryDirectory(prefix="pi-search-art-") as directory:
        root=pathlib.Path(directory)
        with zipfile.ZipFile(archive) as before:
            matches=[name for name in before.namelist() if name.endswith((".db",".db.gz",".sqlite"))]
            if len(matches)!=1:raise ValueError("Expected a single SQLite database")
            name=matches[0]
            old=before.read(name)
        is_gzip=old.startswith(b"\x1f\x8b")
        dbpath=root/"catalog.db"
        dbpath.write_bytes(gzip.decompress(old) if is_gzip else old)
        with sqlite3.connect(dbpath) as db:
            if db.execute("PRAGMA quick_check").fetchone()[0]!="ok":
                raise ValueError("Input Pi catalog integrity failure")
            count=insert(db,rows)
            if db.execute("PRAGMA quick_check").fetchone()[0]!="ok":
                raise ValueError("Final Pi catalog integrity failure")
        updated=dbpath.read_bytes()
        if is_gzip:
            buff=io.BytesIO()
            with gzip.GzipFile(fileobj=buff,mode="wb",mtime=0,compresslevel=6) as f:
                f.write(updated)
            updated=buff.getvalue()
        out=root/"patched.zip"
        with zipfile.ZipFile(archive) as src,zipfile.ZipFile(out,"w") as dest:
            for entry in src.infolist():
                dest.writestr(entry,updated if entry.filename==name else src.read(entry))
        os.replace(out,archive)
        return count
