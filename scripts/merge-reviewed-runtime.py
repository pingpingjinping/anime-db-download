#!/usr/bin/env python3
"""Keep current release catalog rows and add the completed v6 file registry."""
import argparse
import gzip
import hashlib
import json
import pathlib
import shutil
import sqlite3
import tempfile
import zipfile
from reviewed_subtitle_runtime import refresh_runtime_work_links,create_runtime_views,hash_rows

CATALOG_TABLES = ('entries','year_summary','metadata','anissia_year_match','anissia_year_candidates',
                  'anissia_no_same_year_title_review','anissia_reanime_manual_review')
OPTIONAL_TABLES = ('monthly_review','monthly_match_pending','monthly_auto_matches','monthly_match_candidates')
RUNTIME_TABLES = ('packaged_archives_v6','packaged_files_v6','subtitle_review_details',
                 'subtitle_review_batches','subtitle_file_origins','subtitle_confirmed_episodes','subtitle_runtime_metadata')

def sha(path):
    h=hashlib.sha256()
    with open(path,'rb') as stream:
        for data in iter(lambda:stream.read(1024*1024),b''):h.update(data)
    return h.hexdigest()

def copy_table(source,dest,name):
    row=source.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone()
    if not row:raise ValueError('Missing table '+name)
    dest.execute(row[0])
    cursor=source.execute('SELECT * FROM '+name)
    statement='INSERT INTO '+name+' VALUES ('+','.join('?' for _ in cursor.description)+')'
    while batch:=cursor.fetchmany(1000):dest.executemany(statement,batch)

def extract(zip_path,output):
    with zipfile.ZipFile(zip_path) as archive:
        items=[i for i in archive.infolist() if not i.is_dir() and i.filename.lower().endswith(('.db','.db.gz'))]
        if len(items)!=1:raise ValueError('Expected exactly one database')
        with archive.open(items[0]) as source:
            if items[0].filename.endswith('.gz'):
                with gzip.GzipFile(fileobj=source) as decoded,open(output,'wb') as dest:shutil.copyfileobj(decoded,dest)
            else:
                with open(output,'wb') as dest:shutil.copyfileobj(source,dest)

def merge(latest_zip,capsule_gz,output,expected_latest,expected_capsule):
    if sha(latest_zip)!=expected_latest or sha(capsule_gz)!=expected_capsule:
        raise ValueError('Input SHA256 mismatch')
    output=pathlib.Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    if output.exists():raise ValueError('Output exists')
    with tempfile.TemporaryDirectory() as temp:
        latest_path=pathlib.Path(temp)/'latest.db';capsule_path=pathlib.Path(temp)/'capsule.db'
        extract(latest_zip,latest_path)
        with gzip.open(capsule_gz,'rb') as source,open(capsule_path,'wb') as dest:shutil.copyfileobj(source,dest)
        latest=sqlite3.connect(latest_path);capsule=sqlite3.connect(capsule_path);dest=sqlite3.connect(output)
        assert latest.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        assert capsule.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        original_rows=dict(((r[0],r[1]),r) for r in latest.execute('SELECT * FROM entries'))
        latest_tables={r[0] for r in latest.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        tables=(*CATALOG_TABLES,*(n for n in OPTIONAL_TABLES if n in latest_tables))
        catalog_hashes={n:hash_rows(latest,n,','.join(r[1] for r in latest.execute('PRAGMA table_info('+n+')') if r[5])) for n in tables if n not in ('metadata','year_summary')}
        for name in tables:copy_table(latest,dest,name)
        for name,statement,table in latest.execute("SELECT name,sql,tbl_name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"):
            if table in tables:dest.execute(statement)
        added=0
        for row in capsule.execute('SELECT * FROM fallback_entries'):
            if (row[0],row[1]) not in original_rows:
                dest.execute('INSERT INTO entries VALUES ('+','.join('?' for _ in row)+')',row);added+=1
        for name in RUNTIME_TABLES:copy_table(capsule,dest,name)
        for name,order in [('packaged_files_v6','pid'),('subtitle_confirmed_episodes','map_id')]:
            assert hash_rows(capsule,name,order)==hash_rows(dest,name,order)
        # Preserve every current release entry byte-for-byte; only absent reviewed IDs may be added.
        copied_rows=dict(((r[0],r[1]),r) for r in dest.execute('SELECT * FROM entries'))
        assert all(copied_rows[k]==row for k,row in original_rows.items())
        for name,expected in catalog_hashes.items():
            if name!='entries':
                order=','.join(r[1] for r in latest.execute('PRAGMA table_info('+name+')') if r[5])
                assert hash_rows(dest,name,order)==expected
        refresh_runtime_work_links(dest);create_runtime_views(dest)
        for name,statement in (
            ('idx_runtime_file_work','CREATE INDEX idx_runtime_file_work ON packaged_files_v6(target_source,target_id,action)'),
            ('idx_runtime_file_pack','CREATE INDEX idx_runtime_file_pack ON packaged_files_v6(pack_id,action)'),
            ('idx_subtitle_origin_file','CREATE INDEX idx_subtitle_origin_file ON subtitle_file_origins(file_id)'),
            ('idx_subtitle_episode_file','CREATE INDEX idx_subtitle_episode_file ON subtitle_confirmed_episodes(file_id)'),
        ):dest.execute(statement)
        dest.execute("INSERT OR REPLACE INTO subtitle_runtime_metadata VALUES ('latest_catalog_release_sha256',?)",(json.dumps(expected_latest),))
        dest.execute("INSERT OR REPLACE INTO subtitle_runtime_metadata VALUES ('baseline_catalog_date',?)",(json.dumps('Current db-v1 release preserved; reviewed file registry from v6 + completed deltas'),))
        if added:
            dest.execute('DELETE FROM year_summary')
            dest.execute('INSERT INTO year_summary SELECT year,source,count(*) FROM entries GROUP BY year,source')
            dest.execute("INSERT OR REPLACE INTO metadata VALUES ('entry_count',?)",(str(len(copied_rows)),))
        dest.commit();dest.execute('VACUUM')
        assert dest.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert not dest.execute('PRAGMA foreign_key_check').fetchall()
        assert dest.execute('SELECT count(*) FROM subtitle_runtime_files').fetchone()[0]==55530
        assert dest.execute('SELECT count(*) FROM subtitle_runtime_files WHERE work_confirmed=1').fetchone()[0]==55222
        assert dest.execute('SELECT count(*) FROM subtitle_confirmed_episodes').fetchone()[0]==12055
        assert dest.execute('SELECT count(*) FROM subtitle_runtime_files WHERE work_confirmed=0 AND reanime_id IS NOT NULL').fetchone()[0]==0
        assert dest.execute('''SELECT count(*) FROM subtitle_runtime_files f LEFT JOIN entries e
          ON e.source=f.target_source AND e.source_record_id=f.target_id
          WHERE f.target_id IS NOT NULL AND e.source IS NULL''').fetchone()[0]==0
        report={'current_release_zip_sha256':expected_latest,'capsule_sha256':expected_capsule,
            'current_catalog_rows_preserved':len(original_rows),'existing_catalog_rows_modified':0,'fallback_work_records_added':added,
            'merged_catalog_source_counts':dict(dest.execute('SELECT source,count(*) FROM entries GROUP BY source')),
            'subtitle_files':55530,'work_linked_files':55222,'held_files':308,
            'reanime_linked_files':dest.execute('SELECT count(*) FROM subtitle_runtime_files WHERE reanime_id IS NOT NULL').fetchone()[0],
            'confirmed_episode_rows_preserved':12055,'new_episode_assignments':0,
            'reviewed_file_rows_sha256':hash_rows(dest,'packaged_files_v6','pid'),
            'confirmed_episode_rows_sha256':hash_rows(dest,'subtitle_confirmed_episodes','map_id'),
            'current_catalog_table_hashes_preserved':catalog_hashes,'sqlite_integrity':'ok','foreign_keys':'ok'}
        latest.close();capsule.close();dest.close()
    report['database_sha256']=sha(output);report['database_bytes']=output.stat().st_size
    output.with_suffix('.merge.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('latest_zip');p.add_argument('capsule_gz');p.add_argument('output')
    p.add_argument('--expected-latest',required=True);p.add_argument('--expected-capsule',required=True);a=p.parse_args()
    print(json.dumps(merge(a.latest_zip,a.capsule_gz,a.output,a.expected_latest,a.expected_capsule),ensure_ascii=False,indent=2))
