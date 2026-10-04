#!/usr/bin/env python3
"""Verify preserved baseline rows and package separate runtime and crawl assets."""
import argparse
import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path

from prepare_anikoto_catalog import validate_catalog
from catalog_corrections import corrected_rows

RUNTIME_ASSET = 'anime_four_sources_multilingual_prepared.zip'
CATALOG_ASSET = 'anikoto_listing_latest.zip'
GENERATED_TABLES = {'entries', 'subtitle_work_reanime_links', 'anikoto_catalog',
                    'anikoto_work_links', 'anikoto_match_metadata'}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def table_digest(db, table):
    quoted = '"'+table.replace('"','""')+'"'
    columns = db.execute('PRAGMA table_info('+quoted+')').fetchall()
    primary = [row[1] for row in sorted(columns,key=lambda r:r[5]) if row[5]]
    order = ','.join('"'+c.replace('"','""')+'"' for c in primary) if primary else 'rowid'
    hasher, count = hashlib.sha256(), 0
    for row in db.execute('SELECT * FROM '+quoted+' ORDER BY '+order):
        hasher.update(json.dumps(list(row),ensure_ascii=False,separators=(',',':')).encode()+b'\n')
        count += 1
    return {'rows':count,'sha256':hasher.hexdigest()}


def package(baseline, matched, catalog, matching_dir, catalog_exports, output, corrections=None):
    baseline,matched,catalog,matching_dir,catalog_exports,output=map(Path,(baseline,matched,catalog,matching_dir,catalog_exports,output))
    if output.resolve() in (baseline.resolve(),matched.resolve(),catalog.resolve()):
        raise ValueError('Package output must be separate from source databases')
    report=json.loads((matching_dir/'anikoto-match-report.json').read_text())
    crawl=validate_catalog(catalog)
    if not crawl['complete'] or report['baseline_sha256']!=digest(baseline) or report['anikoto_sha256']!=digest(catalog):
        raise ValueError('Incomplete or changed matching inputs; refusing Release package')
    if report['crawl_run_id']!=crawl['run_id']:
        raise ValueError('Report refers to a different crawl')
    with sqlite3.connect(baseline.resolve().as_uri()+'?mode=ro',uri=True) as before, \
            sqlite3.connect(matched.resolve().as_uri()+'?mode=ro',uri=True) as after:
        if after.execute('PRAGMA quick_check').fetchone()[0]!='ok':
            raise ValueError('Runtime database integrity check failed')
        if {r[0] for r in after.execute('SELECT DISTINCT source FROM entries')}!={'AniList','ReAnime','Onnada','Anissia'}:
            raise ValueError('Expected the four-source catalog, without raw Anikoto source rows')
        before.row_factory=after.row_factory=sqlite3.Row
        expected_entries=corrected_rows([dict(row) for row in before.execute('SELECT * FROM entries ORDER BY source,source_record_id')],corrections or [])
        actual_entries=[dict(row) for row in after.execute('SELECT * FROM entries ORDER BY source,source_record_id')]
        if actual_entries!=expected_entries:
            raise ValueError('Source records changed outside the reviewed corrections')
        before.row_factory=after.row_factory=None
        protected={r[0] for r in before.execute("SELECT name FROM sqlite_master WHERE type='table'")} - GENERATED_TABLES
        preserved={}
        for table in sorted(protected):
            previous,current=table_digest(before,table),table_digest(after,table)
            if previous!=current:
                raise ValueError('Protected baseline rows changed: '+table)
            preserved[table]=current
        version=after.execute("SELECT value FROM anikoto_match_metadata WHERE key='runtime_format_version'").fetchone()
        if not version or version[0]!='2':
            raise ValueError('Expected compact Anikoto runtime format 2')
        matched_report=after.execute("SELECT value FROM anikoto_match_metadata WHERE key='latest_match_report'").fetchone()
        if not matched_report or json.loads(matched_report[0])!=report:
            raise ValueError('Runtime database does not correspond to the matching report')
        missing=after.execute("SELECT anikoto_id FROM anikoto_work_links WHERE status IN ('automatic','reviewed') EXCEPT SELECT source_record_id FROM anikoto_catalog").fetchall()
        extra=after.execute("SELECT source_record_id FROM anikoto_catalog EXCEPT SELECT anikoto_id FROM anikoto_work_links WHERE status IN ('automatic','reviewed')").fetchall()
        if missing or extra:
            raise ValueError('Runtime must contain exactly the accepted Anikoto catalog identities')
        count=after.execute('SELECT COUNT(*) FROM anikoto_catalog').fetchone()[0]
    output.mkdir(parents=True,exist_ok=True)
    runtime=output/RUNTIME_ASSET
    raw=output/CATALOG_ASSET
    with zipfile.ZipFile(runtime,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        archive.write(matched,'anime_four_sources_multilingual_prepared.db')
    with zipfile.ZipFile(raw,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        archive.write(catalog,'anikoto-catalog.sqlite')
        for name in ('anikoto-catalog.jsonl','anikoto-crawl-report.json'):
            archive.write(catalog_exports/name,name)
    validation={'runtime_format_version':2,'runtime_works':count,'protected_tables':preserved,
                'baseline_sha256':digest(baseline),'matched_db_sha256':digest(matched),
                'runtime_zip_bytes':runtime.stat().st_size,'runtime_zip_sha256':digest(runtime),
                'catalog_zip_bytes':raw.stat().st_size,'catalog_zip_sha256':digest(raw),
                'crawl_run_id':crawl['run_id']}
    (output/'anikoto-release-validation.json').write_text(json.dumps(validation,ensure_ascii=False,indent=2)+'\n')
    return validation


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',type=Path,required=True)
    parser.add_argument('--matched-db',type=Path,required=True)
    parser.add_argument('--catalog',type=Path,required=True)
    parser.add_argument('--matching-dir',type=Path,required=True)
    parser.add_argument('--catalog-exports',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--corrections',type=Path)
    args=parser.parse_args()
    corrections=json.loads(args.corrections.read_text()) if args.corrections else []
    result=package(args.baseline,args.matched_db,args.catalog,args.matching_dir,args.catalog_exports,args.output,corrections)
    print(json.dumps({k:v for k,v in result.items() if k!='protected_tables'},ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
