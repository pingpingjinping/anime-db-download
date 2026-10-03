#!/usr/bin/env python3
"""Import the exported file registry into streaming-project's Postgres.

Run from the streaming-project repository root. Does not change the catalog,
existing stream links, episode settings, or manually edited subtitle rows.
Use --dry-run to validate and generate SQL without calling Docker/Postgres.
"""
import argparse
import csv
import io
import json
import pathlib
import sqlite3
import subprocess

DATASET = 'reviewed_v6_1109'
NULL = '\\N'

def append_copy(stream, table, columns, rows):
    stream.write('COPY ' + table + ' (' + ','.join(columns) + ") FROM STDIN WITH (FORMAT csv, NULL '\\N');\n")
    writer = csv.writer(stream, lineterminator='\n')
    count = 0
    for row in rows:
        writer.writerow([NULL if value is None else value for value in row])
        count += 1
    stream.write('\\.\n')
    return count

def prepare(path):
    db = sqlite3.connect(pathlib.Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('SQLite quick_check failed')
        metadata = {k: json.loads(v) for k, v in db.execute('SELECT key,value FROM subtitle_runtime_metadata')}
        if metadata['format_version'] != 1 or metadata['completed_bundles'] != 1109:
            raise ValueError('Unsupported or incomplete export')
        stream = io.StringIO()
        stream.write('''BEGIN;
CREATE TABLE IF NOT EXISTS anime_subtitle_archives (
 dataset_key TEXT NOT NULL, pack_id TEXT NOT NULL, source TEXT NOT NULL,
 zip_relative_path TEXT NOT NULL, sha256 TEXT NOT NULL, member_count INTEGER NOT NULL,
 PRIMARY KEY(dataset_key,pack_id)
);
CREATE TABLE IF NOT EXISTS anime_subtitle_files (
 dataset_key TEXT NOT NULL, pid TEXT NOT NULL, source TEXT NOT NULL,
 pack_id TEXT NOT NULL, zip_relative_path TEXT NOT NULL, member_path TEXT NOT NULL,
 sha256 TEXT NOT NULL, bundle_id TEXT, target_source TEXT, target_id TEXT,
 review_status TEXT NOT NULL, reason TEXT, work_title_ko TEXT, season_format TEXT,
 work_format TEXT, reanime_id TEXT, language_review TEXT, quality_notes TEXT,
 content_extension TEXT, work_confirmed BOOLEAN NOT NULL,
 is_manual BOOLEAN NOT NULL DEFAULT FALSE,
 PRIMARY KEY(dataset_key,pid)
);
CREATE TABLE IF NOT EXISTS anime_subtitle_file_origins (
 dataset_key TEXT NOT NULL, pid TEXT NOT NULL, file_id TEXT NOT NULL,
 PRIMARY KEY(dataset_key,pid,file_id)
);
CREATE TABLE IF NOT EXISTS anime_subtitle_confirmed_episodes (
 dataset_key TEXT NOT NULL, map_id BIGINT NOT NULL, bundle_id TEXT, file_id TEXT,
 target_source TEXT, target_id TEXT, source_episode TEXT, target_episode INTEGER,
 status TEXT NOT NULL, PRIMARY KEY(dataset_key,map_id)
);
CREATE TABLE IF NOT EXISTS anime_subtitle_work_reanime_links (
 dataset_key TEXT NOT NULL, target_source TEXT NOT NULL, target_id TEXT NOT NULL,
 reanime_id TEXT NOT NULL, link_status TEXT NOT NULL, automatic_eligible BOOLEAN NOT NULL,
 PRIMARY KEY(dataset_key,target_source,target_id,reanime_id)
);
CREATE TABLE IF NOT EXISTS anime_subtitle_dataset_metadata (
 dataset_key TEXT PRIMARY KEY, payload JSONB NOT NULL,
 imported_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_anime_subtitle_reanime ON anime_subtitle_files(reanime_id,work_confirmed);
CREATE INDEX IF NOT EXISTS idx_anime_subtitle_work ON anime_subtitle_files(target_source,target_id);
CREATE INDEX IF NOT EXISTS idx_anime_subtitle_file_origin ON anime_subtitle_file_origins(file_id);
CREATE INDEX IF NOT EXISTS idx_anime_subtitle_episode_file ON anime_subtitle_confirmed_episodes(file_id);
CREATE TEMP TABLE subtitle_archive_stage (LIKE anime_subtitle_archives INCLUDING DEFAULTS);
CREATE TEMP TABLE subtitle_file_stage (LIKE anime_subtitle_files INCLUDING DEFAULTS);
CREATE TEMP TABLE subtitle_origin_stage (LIKE anime_subtitle_file_origins INCLUDING DEFAULTS);
CREATE TEMP TABLE subtitle_episode_stage (LIKE anime_subtitle_confirmed_episodes INCLUDING DEFAULTS);
CREATE TEMP TABLE subtitle_work_link_stage (LIKE anime_subtitle_work_reanime_links INCLUDING DEFAULTS);
CREATE TEMP TABLE subtitle_metadata_stage (dataset_key TEXT,payload JSONB);
''')
        counts = {}
        counts['archives'] = append_copy(stream, 'subtitle_archive_stage',
            ['dataset_key','pack_id','source','zip_relative_path','sha256','member_count'],
            ((DATASET, *r) for r in db.execute('SELECT pack_id,source,zip_relative_path,sha256,member_count FROM packaged_archives_v6 ORDER BY pack_id')))
        file_columns = ['dataset_key','pid','source','pack_id','zip_relative_path','member_path','sha256','bundle_id','target_source','target_id',
            'review_status','reason','work_title_ko','season_format','work_format','reanime_id','language_review','quality_notes','content_extension','work_confirmed']
        counts['subtitle_files'] = append_copy(stream, 'subtitle_file_stage', file_columns,
            ((DATASET, *r[:-1], bool(r[-1])) for r in db.execute('SELECT * FROM subtitle_runtime_files ORDER BY pid')))
        counts['file_origins'] = append_copy(stream, 'subtitle_origin_stage', ['dataset_key','pid','file_id'],
            ((DATASET,*r) for r in db.execute('''SELECT o.pid,o.file_id FROM subtitle_file_origins o
                JOIN subtitle_runtime_files f USING(pid) ORDER BY o.pid,o.file_id''')))
        counts['confirmed_episodes'] = append_copy(stream, 'subtitle_episode_stage',
            ['dataset_key','map_id','bundle_id','file_id','target_source','target_id','source_episode','target_episode','status'],
            ((DATASET,*r) for r in db.execute('SELECT * FROM subtitle_confirmed_episodes ORDER BY map_id')))
        counts['work_reanime_links'] = append_copy(stream, 'subtitle_work_link_stage',
            ['dataset_key','target_source','target_id','reanime_id','link_status','automatic_eligible'],
            ((DATASET,*r[:-1],bool(r[-1])) for r in db.execute('SELECT * FROM subtitle_work_reanime_links ORDER BY target_source,target_id,reanime_id')))
        append_copy(stream, 'subtitle_metadata_stage', ['dataset_key','payload'], [(DATASET,json.dumps(metadata,ensure_ascii=False))])
        if counts['subtitle_files'] != 55530 or counts['confirmed_episodes'] != 12055:
            raise ValueError('Export counts do not match completed review')
        stream.write('''
INSERT INTO anime_subtitle_archives SELECT * FROM subtitle_archive_stage WHERE true
ON CONFLICT(dataset_key,pack_id) DO UPDATE SET
 source=EXCLUDED.source,zip_relative_path=EXCLUDED.zip_relative_path,
 sha256=EXCLUDED.sha256,member_count=EXCLUDED.member_count;
INSERT INTO anime_subtitle_files SELECT * FROM subtitle_file_stage WHERE true
ON CONFLICT(dataset_key,pid) DO UPDATE SET
 source=EXCLUDED.source,pack_id=EXCLUDED.pack_id,zip_relative_path=EXCLUDED.zip_relative_path,
 member_path=EXCLUDED.member_path,sha256=EXCLUDED.sha256,bundle_id=EXCLUDED.bundle_id,
 target_source=EXCLUDED.target_source,target_id=EXCLUDED.target_id,
 review_status=EXCLUDED.review_status,reason=EXCLUDED.reason,work_title_ko=EXCLUDED.work_title_ko,
 season_format=EXCLUDED.season_format,work_format=EXCLUDED.work_format,
 reanime_id=EXCLUDED.reanime_id,language_review=EXCLUDED.language_review,
 quality_notes=EXCLUDED.quality_notes,content_extension=EXCLUDED.content_extension,
 work_confirmed=EXCLUDED.work_confirmed WHERE NOT anime_subtitle_files.is_manual;
INSERT INTO anime_subtitle_file_origins SELECT * FROM subtitle_origin_stage WHERE true
ON CONFLICT(dataset_key,pid,file_id) DO NOTHING;
INSERT INTO anime_subtitle_confirmed_episodes SELECT * FROM subtitle_episode_stage WHERE true
ON CONFLICT(dataset_key,map_id) DO NOTHING;
DELETE FROM anime_subtitle_work_reanime_links WHERE dataset_key='reviewed_v6_1109';
INSERT INTO anime_subtitle_work_reanime_links SELECT * FROM subtitle_work_link_stage;
INSERT INTO anime_subtitle_dataset_metadata(dataset_key,payload)
 SELECT * FROM subtitle_metadata_stage WHERE true
ON CONFLICT(dataset_key) DO UPDATE SET payload=EXCLUDED.payload,imported_at=CURRENT_TIMESTAMP;
COMMIT;
''')
        return stream.getvalue(), counts
    finally:
        db.close()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database'); parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--sql', type=pathlib.Path, help='Save generated SQL without applying it')
    args = parser.parse_args()
    sql, counts = prepare(args.database)
    if args.sql:
        args.sql.write_text(sql, encoding='utf-8')
    if args.dry_run or args.sql:
        print(json.dumps({'applied':False,'validated':counts,'sql_bytes':len(sql.encode())},ensure_ascii=False))
        return
    command = ['docker','compose','exec','-T','db','sh','-c',
               'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"']
    result = subprocess.run(command, input=sql, text=True, check=False)
    if result.returncode:
        raise SystemExit('Import failed; transaction rolled back.')
    print(json.dumps({'applied':True,'imported':counts,'dataset':DATASET},ensure_ascii=False))

if __name__ == '__main__':
    main()
