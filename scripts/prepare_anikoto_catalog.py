#!/usr/bin/env python3
"""Restore the checked-in Anikoto snapshot and export it without network requests."""
import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from crawl_anikoto import DEFAULT_DB, ROOT, export, report

DEFAULT_SEED = ROOT / "data" / "anikoto" / "seed.json"


def validate_catalog(path):
    # Read-only validation must not create or modify an existing database.
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Anikoto database integrity check failed")
        run = db.execute("SELECT run_id FROM anikoto_crawl_runs ORDER BY started_at DESC,rowid DESC LIMIT 1").fetchone()
        if not run:
            raise ValueError("Anikoto database has no crawl run")
        summary = report(db, run[0])
        if summary["records_in_run"] <= 0 or summary["pages_collected"] <= 0:
            raise ValueError("Anikoto database has no collected listing records")
        return summary


def prepare(db_path=DEFAULT_DB, seed_manifest=DEFAULT_SEED):
    db_path, seed_manifest = Path(db_path), Path(seed_manifest)
    if db_path.exists():
        return "existing", validate_catalog(db_path)
    manifest = json.loads(seed_manifest.read_text(encoding="utf-8"))
    name = manifest["archive"]
    if Path(name).name != name:
        raise ValueError("Seed archive must be next to its manifest")
    archive_path = seed_manifest.parent / name
    if hashlib.sha256(archive_path.read_bytes()).hexdigest() != manifest["archive_sha256"]:
        raise ValueError("Seed archive checksum mismatch")
    with zipfile.ZipFile(archive_path) as archive:
        if archive.namelist().count("anikoto-catalog.sqlite") != 1:
            raise ValueError("Seed must contain exactly one Anikoto database")
        content = archive.read("anikoto-catalog.sqlite")
    if hashlib.sha256(content).hexdigest() != manifest["database_sha256"]:
        raise ValueError("Seed database checksum mismatch")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=db_path.parent, suffix=".sqlite", delete=False) as temp:
        temporary_path = Path(temp.name)
        temp.write(content)
    try:
        summary = validate_catalog(temporary_path)
        if not summary["complete"] or any(summary[key] != manifest[key]
                for key in ("run_id", "records_in_run", "pages_collected")):
            raise ValueError("Seed database does not match its completed crawl manifest")
        # Publish only after validation, and never replace a database created meanwhile.
        os.link(temporary_path, db_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return "seed", summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--seed-manifest", type=Path, default=DEFAULT_SEED)
    parser.add_argument("--export-dir", type=Path, default=ROOT / "output" / "anikoto")
    args = parser.parse_args()
    origin, _ = prepare(args.db, args.seed_manifest)
    summary = export(args.db, args.export_dir)
    print(json.dumps({"database_origin": origin, "network_requests": 0, **summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
