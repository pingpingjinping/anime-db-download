#!/usr/bin/env python3
"""Emit only the supplemental file delta; catalog uses its existing importer."""
import argparse
import json
from pathlib import Path
from body_audit_postgres_20261011 import sql

ROOT = Path(__file__).resolve().parents[1]


def emit(plan):
    files = [c for c in plan['changes']['registry'] if c['table'] == 'subsubsub_file_registry_v1']
    out = ["BEGIN;", "SET LOCAL lock_timeout='15s';",
           'LOCK TABLE anime_subtitle_files IN SHARE ROW EXCLUSIVE MODE;',
           'CREATE TEMP TABLE other_work_applied(pid TEXT PRIMARY KEY);']
    for change in files:
        key = "dataset_key='subsubsub_20261009' AND pid=" + sql(change['key']['pid'])
        before = change['before']
        after = {**before, **change['after']}
        fixed = ' AND '.join(k+' IS NOT DISTINCT FROM '+sql(before[k]) for k in ('sha256', 'pack_id', 'member_path'))
        alternatives = []
        for values in (before, after):
            alternatives.append('(' + ' AND '.join(k+' IS NOT DISTINCT FROM '+sql(values[k]) for k in ('target_source', 'target_id', 'reanime_id')) + ')')
        out.append("DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM anime_subtitle_files WHERE " + key + ") THEN RAISE EXCEPTION 'Missing BECK file'; END IF; IF EXISTS(SELECT 1 FROM anime_subtitle_files WHERE " + key + ' AND NOT is_manual AND NOT (' + fixed + ' AND (' + ' OR '.join(alternatives) + "))) THEN RAISE EXCEPTION 'BECK file changed'; END IF; END $$;")
        out.append('INSERT INTO other_work_applied SELECT pid FROM anime_subtitle_files WHERE '+key+' AND NOT is_manual;')
        values = {k: after[k] for k in ('target_source', 'target_id', 'reanime_id', 'review_status')}
        values.update(work_title_ko='BECK', work_format='TV', reason=change['reason'], work_confirmed=True)
        out.append('UPDATE anime_subtitle_files SET '+','.join(k+'='+sql(v) for k,v in values.items())+' WHERE '+key+' AND NOT is_manual;')
    out.extend(['SELECT count(*) AS applied_files,'+str(len(files))+'-count(*) AS preserved_manual_files FROM other_work_applied;', 'COMMIT;'])
    return '\n'.join(out)+'\n'


if __name__ == '__main__':
    p=argparse.ArgumentParser(); p.add_argument('output', type=Path); a=p.parse_args()
    a.output.write_text(emit(json.loads((ROOT/'data/matching/other-work-20261014.json').read_text())))
