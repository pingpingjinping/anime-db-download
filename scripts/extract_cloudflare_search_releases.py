#!/usr/bin/env python3
"""Safely read a single SQLite DB from a reviewed anime-db-download Release ZIP."""
import argparse
import gzip
import io
import shutil
import sqlite3
import zipfile
from pathlib import Path


def extract(zip_path, db_path):
    with zipfile.ZipFile(zip_path) as zf:
        matches = [item for item in zf.infolist()
                   if not item.is_dir() and item.filename.lower().endswith(('.db','.sqlite','.db.gz'))]
        if len(matches) != 1:
            raise ValueError(f'Expected exactly one SQLite database in {zip_path}, found {len(matches)}')
        if matches[0].file_size > 512 * 1024 * 1024:
            raise ValueError('Release database exceeds safety limit')
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(matches[0]) as source, open(db_path, 'wb') as output:
            if matches[0].filename.lower().endswith('.gz'):
                with gzip.GzipFile(fileobj=source) as decompressed:
                    shutil.copyfileobj(decompressed, output)
            else:
                shutil.copyfileobj(source, output)
    with sqlite3.connect(db_path.resolve().as_uri()+'?mode=ro',uri=True) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError(f'Failed SQLite integrity check: {zip_path}')
    print(f'Extracted and validated SQLite: {db_path.name} ({db_path.stat().st_size} bytes)')

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('output', type=Path)
    args=parser.parse_args()
    extract(args.archive,args.output)
