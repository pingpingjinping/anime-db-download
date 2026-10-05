#!/usr/bin/env python3
"""Split or validate the db-v1 catalog and reviewed subtitle registry assets."""
import argparse
import gzip
import hashlib
import json
import pathlib
import shutil
import sqlite3
import tempfile
import zipfile

CATALOG_ASSET = 'anime_four_sources_multilingual_prepared.zip'
REGISTRY_ASSET = 'anime_reviewed_subtitle_registry_v1.zip'
CATALOG_DB_NAME = 'anime_four_sources_multilingual_prepared.db'
REGISTRY_DB_NAME = 'anime_reviewed_subtitle_registry_v1.db'
REGISTRY_TABLES = (
    'packaged_archives_v6',
    'packaged_files_v6',
    'subtitle_review_details',
    'subtitle_review_batches',
    'subtitle_file_origins',
    'subtitle_confirmed_episodes',
    'subtitle_runtime_metadata',
)
DERIVED_REGISTRY_TABLES = ('subtitle_work_reanime_links',)
REGISTRY_VIEWS = ('subtitle_runtime_files', 'subtitle_file_episode_links')
EXPECTED_SOURCES = {'AniList', 'ReAnime', 'Onnada', 'Anissia'}


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def extract_database(archive_path, output):
    with zipfile.ZipFile(archive_path) as archive:
        items = [
            item for item in archive.infolist()
            if not item.is_dir()
            and item.filename.lower().endswith(('.db', '.db.gz', '.sqlite'))
        ]
        if len(items) != 1:
            raise ValueError(f'Expected exactly one SQLite database, found {len(items)}')
        data = archive.read(items[0])
    if data.startswith(b'\x1f\x8b'):
        data = gzip.decompress(data)
    pathlib.Path(output).write_bytes(data)


def pack_database(db_path, archive_path, arcname):
    archive_path = pathlib.Path(archive_path)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        archive_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as archive:
        info = zipfile.ZipInfo(arcname, date_time=(2026, 10, 6, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o100644 << 16
        archive.writestr(info, pathlib.Path(db_path).read_bytes())


def table_digest(db, table):
    quoted = '"' + table.replace('"', '""') + '"'
    columns = db.execute(f'PRAGMA table_info({quoted})').fetchall()
    primary = [r[1] for r in sorted(columns, key=lambda r: r[5]) if r[5]]
    order = ','.join('"' + c.replace('"', '""') + '"' for c in primary)
    if not order:
        order = 'rowid'
    hasher = hashlib.sha256()
    count = 0
    for row in db.execute(f'SELECT * FROM {quoted} ORDER BY {order}'):
        hasher.update(
            json.dumps(list(row), ensure_ascii=False, separators=(',', ':')).encode()
            + b'\n'
        )
        count += 1
    return {'rows': count, 'sha256': hasher.hexdigest()}


def copy_table(source, dest, name):
    schema = source.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    if not schema:
        raise ValueError(f'Missing table {name}')
    dest.execute(schema[0])
    cursor = source.execute('SELECT * FROM "' + name.replace('"', '""') + '"')
    placeholders = ','.join('?' for _ in cursor.description)
    statement = 'INSERT INTO "' + name.replace('"', '""') + f'" VALUES ({placeholders})'
    while batch := cursor.fetchmany(1000):
        dest.executemany(statement, batch)
    for (index_sql,) in source.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
        (name,),
    ):
        dest.execute(index_sql)


def validate_catalog_db(path, require_split=True):
    with sqlite3.connect(path) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Catalog SQLite quick_check failed')
        sources = {row[0] for row in db.execute('SELECT DISTINCT source FROM entries')}
        if sources != EXPECTED_SOURCES:
            raise ValueError(f'Unexpected catalog sources: {sorted(sources)}')
        tables = {
            row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if require_split:
            leaked = set(REGISTRY_TABLES + DERIVED_REGISTRY_TABLES) & tables
            if leaked:
                raise ValueError(f'Catalog still contains registry tables: {sorted(leaked)}')
        return {
            'entries': db.execute('SELECT COUNT(*) FROM entries').fetchone()[0],
            'anikoto_catalog': (
                db.execute('SELECT COUNT(*) FROM anikoto_catalog').fetchone()[0]
                if 'anikoto_catalog' in tables else 0
            ),
        }


def validate_registry_db(path):
    with sqlite3.connect(path) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Registry SQLite quick_check failed')
        tables = {
            row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        missing = set(REGISTRY_TABLES) - tables
        if missing:
            raise ValueError(f'Registry is missing tables: {sorted(missing)}')
        if 'entries' in tables:
            raise ValueError('Registry asset must not contain the catalog entries table')
        if 'subtitle_runtime_files_export' not in tables:
            raise ValueError('Registry is missing materialized runtime export')
        metadata = {
            key: json.loads(value)
            for key, value in db.execute('SELECT key,value FROM subtitle_runtime_metadata')
        }
        if metadata.get('format_version') != 1 or metadata.get('completed_bundles') != 1109:
            raise ValueError('Registry metadata is incomplete')
        files = db.execute(
            'SELECT COUNT(*) FROM subtitle_runtime_files_export'
        ).fetchone()[0]
        episodes = db.execute(
            'SELECT COUNT(*) FROM subtitle_confirmed_episodes'
        ).fetchone()[0]
        if files != 55530 or episodes != 12055:
            raise ValueError(
                f'Unexpected reviewed registry counts: files={files}, episodes={episodes}'
            )
        return {
            'subtitle_files': files,
            'confirmed_episodes': episodes,
            'completed_bundles': metadata.get('completed_bundles'),
        }


def split_release(source_zip, output_dir):
    source_zip = pathlib.Path(source_zip)
    output_dir = pathlib.Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    source_digest = sha256(source_zip)

    with tempfile.TemporaryDirectory(prefix='db-v1-split-') as temp:
        temp = pathlib.Path(temp)
        combined = temp / 'combined.db'
        catalog = temp / CATALOG_DB_NAME
        registry = temp / REGISTRY_DB_NAME
        extract_database(source_zip, combined)

        with sqlite3.connect(combined) as before:
            if before.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('Combined SQLite quick_check failed')
            validate_catalog_db(combined, require_split=False)
            before_tables = {
                row[0] for row in before.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            missing_registry = set(REGISTRY_TABLES) - before_tables
            if missing_registry:
                raise ValueError(
                    'Source is not the legacy combined asset; missing '
                    + ', '.join(sorted(missing_registry))
                )
            if not before.execute(
                "SELECT 1 FROM sqlite_master WHERE name='subtitle_runtime_files' "
                "AND type='view'"
            ).fetchone():
                raise ValueError('Legacy combined asset has no subtitle_runtime_files view')

            protected_tables = sorted(
                name for name in (
                    before_tables
                    - set(REGISTRY_TABLES)
                    - set(DERIVED_REGISTRY_TABLES)
                )
                if not name.startswith('sqlite_')
            )
            protected_hashes = {
                name: table_digest(before, name)
                for name in protected_tables
            }
            registry_hashes = {
                name: table_digest(before, name)
                for name in REGISTRY_TABLES
            }

            shutil.copy2(combined, catalog)
            with sqlite3.connect(catalog) as cat:
                for view in REGISTRY_VIEWS:
                    cat.execute(f'DROP VIEW IF EXISTS "{view}"')
                for table in (*REGISTRY_TABLES, *DERIVED_REGISTRY_TABLES):
                    cat.execute(f'DROP TABLE IF EXISTS "{table}"')
                cat.commit()
                cat.execute('VACUUM')
                for name, expected in protected_hashes.items():
                    actual = table_digest(cat, name)
                    if actual != expected:
                        raise ValueError(f'Catalog row hash changed while splitting: {name}')

            reg = sqlite3.connect(registry)
            try:
                for name in REGISTRY_TABLES:
                    copy_table(before, reg, name)
                reg.execute(
                    'CREATE TABLE subtitle_runtime_files_export AS '
                    'SELECT * FROM subtitle_runtime_files ORDER BY pid'
                )
                reg.execute(
                    'CREATE UNIQUE INDEX idx_subtitle_runtime_export_pid '
                    'ON subtitle_runtime_files_export(pid)'
                )
                reg.execute(
                    "INSERT OR REPLACE INTO subtitle_runtime_metadata VALUES ('asset_role',?)",
                    (json.dumps('reviewed_subtitle_registry'),),
                )
                reg.execute(
                    "INSERT OR REPLACE INTO subtitle_runtime_metadata VALUES ('asset_format_version',?)",
                    (json.dumps(1),),
                )
                reg.execute(
                    "INSERT OR REPLACE INTO subtitle_runtime_metadata VALUES ('source_combined_asset_sha256',?)",
                    (json.dumps(source_digest),),
                )
                reg.commit()
                reg.execute('VACUUM')
                for name, expected in registry_hashes.items():
                    if name == 'subtitle_runtime_metadata':
                        continue
                    actual = table_digest(reg, name)
                    if actual != expected:
                        raise ValueError(f'Registry row hash changed while splitting: {name}')
            finally:
                reg.close()

        validate_catalog_db(catalog)
        validate_registry_db(registry)
        catalog_zip = output_dir / CATALOG_ASSET
        registry_zip = output_dir / REGISTRY_ASSET
        pack_database(catalog, catalog_zip, CATALOG_DB_NAME)
        pack_database(registry, registry_zip, REGISTRY_DB_NAME)

    before_bytes = source_zip.stat().st_size
    catalog_bytes = catalog_zip.stat().st_size
    registry_bytes = registry_zip.stat().st_size
    report = {
        'layout_version': 1,
        'source_combined_asset_sha256': source_digest,
        'source_combined_zip_bytes': before_bytes,
        'catalog_asset': CATALOG_ASSET,
        'catalog_zip_bytes': catalog_bytes,
        'catalog_zip_sha256': sha256(catalog_zip),
        'registry_asset': REGISTRY_ASSET,
        'registry_zip_bytes': registry_bytes,
        'registry_zip_sha256': sha256(registry_zip),
        'monthly_catalog_download_bytes_saved': before_bytes - catalog_bytes,
        'monthly_catalog_download_reduction_percent': round(
            100.0 * (before_bytes - catalog_bytes) / before_bytes, 2
        ),
        'split_total_zip_bytes': catalog_bytes + registry_bytes,
    }
    (output_dir / 'db-v1-split-validation.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    return report


def validate_split(catalog_zip, registry_zip):
    catalog_zip = pathlib.Path(catalog_zip)
    registry_zip = pathlib.Path(registry_zip)
    with tempfile.TemporaryDirectory(prefix='db-v1-validate-') as temp:
        temp = pathlib.Path(temp)
        catalog_db = temp / 'catalog.db'
        registry_db = temp / 'registry.db'
        extract_database(catalog_zip, catalog_db)
        extract_database(registry_zip, registry_db)
        catalog = validate_catalog_db(catalog_db)
        registry = validate_registry_db(registry_db)
    return {
        'layout_version': 1,
        'catalog_zip_bytes': catalog_zip.stat().st_size,
        'catalog_zip_sha256': sha256(catalog_zip),
        'registry_zip_bytes': registry_zip.stat().st_size,
        'registry_zip_sha256': sha256(registry_zip),
        'catalog': catalog,
        'registry': registry,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    split = sub.add_parser('split')
    split.add_argument('--source', type=pathlib.Path, required=True)
    split.add_argument('--output', type=pathlib.Path, required=True)
    validate = sub.add_parser('validate')
    validate.add_argument('--catalog', type=pathlib.Path, required=True)
    validate.add_argument('--registry', type=pathlib.Path, required=True)
    args = parser.parse_args()
    result = (
        split_release(args.source, args.output)
        if args.command == 'split'
        else validate_split(args.catalog, args.registry)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
