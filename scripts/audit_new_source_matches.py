#!/usr/bin/env python3
"""Replay saved Anissia samples without changing the source database.

Without --database, use the checked-in, curated target fixture. With --database,
use its complete foreign catalog. Both modes deliberately disable historical
review reservations and isolate the sampled Anissia rows to model new ingestion.
"""
import argparse
import json
import sqlite3
from pathlib import Path

from match_new_sources import prepare_tables, refresh_auto_matches

DEFAULT_SAMPLES = Path(__file__).resolve().parents[1] / 'data/matching/anissia_recent_50_20261008.json'
ENTRY_COLUMNS = ('source', 'source_record_id', 'year', 'year_basis', 'source_year_raw',
                 'title_ko', 'title_native', 'title_romaji', 'title_english', 'format',
                 'status', 'genres', 'cross_source_ids_json', 'existing_match_status', 'source_metadata_json')
ENTRY_SCHEMA = '''CREATE TABLE entries (
  source TEXT,source_record_id TEXT,year INTEGER,year_basis TEXT,source_year_raw TEXT,
  title_ko TEXT,title_native TEXT,title_romaji TEXT,title_english TEXT,format TEXT,
  status TEXT,genres TEXT,cross_source_ids_json TEXT,existing_match_status TEXT,
  source_metadata_json TEXT,PRIMARY KEY(source,source_record_id))'''


def replay(samples, database=None):
    db = sqlite3.connect(':memory:')
    try:
        db.execute(ENTRY_SCHEMA)
        insert = 'INSERT INTO entries VALUES (' + ','.join('?' for _ in ENTRY_COLUMNS) + ')'
        if database is None:
            db.executemany(insert, [tuple(row.get(f) for f in ENTRY_COLUMNS) for row in samples['targets']])
        else:
            path = Path(database).resolve()
            with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as source:
                cursor = source.execute('SELECT '+','.join(ENTRY_COLUMNS)+" FROM entries WHERE source!='Anissia'")
                while batch := cursor.fetchmany(1000):
                    db.executemany(insert, batch)
        db.executemany(insert, [tuple(row.get(f) for f in ENTRY_COLUMNS) for row in samples['samples']])
        prepare_tables(db)
        db.executemany('INSERT INTO monthly_match_pending VALUES (?,?)',
                       [('Anissia',str(row['source_record_id'])) for row in samples['samples']])
        summary = refresh_auto_matches(db)
        automatic = {str(aid):rid for rid,aid in db.execute('SELECT reanime_id,anissia_no FROM monthly_auto_matches')}
        rows = []
        for row in samples['samples']:
            aid = str(row['source_record_id'])
            candidates = [{'reanime_id':rid,'score':score,'reason':reason,'evidence':json.loads(evidence)}
                          for rid,score,reason,evidence in db.execute(
                              'SELECT reanime_id,score,reason,evidence_json FROM monthly_match_candidates WHERE anissia_no=? ORDER BY reanime_id',
                              (int(aid),))]
            rows.append({'anissia_no':int(aid), 'title':row.get('title_ko'),
                         'status':'automatic' if aid in automatic else 'review',
                         'reanime_id':automatic.get(aid), 'candidates':candidates})
        return {'mode':'full_foreign_catalog' if database else 'curated_target_fixture',
                'scope':'New-ingestion replay; historical reservations disabled in memory only',
                'summary':summary, 'samples':rows}
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=Path, default=DEFAULT_SAMPLES)
    parser.add_argument('--database', type=Path, help='Read-only source SQLite catalog, not a ZIP/gzip file')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = replay(json.loads(args.samples.read_text()), args.database)
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
        print(json.dumps({'summary':result['summary'],'output':str(args.output)},ensure_ascii=False))
    else:
        print(encoded, end='')


if __name__ == '__main__':
    main()
