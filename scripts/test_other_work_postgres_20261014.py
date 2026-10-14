#!/usr/bin/env python3
"""Validate real PostgreSQL apply, rerun, manual overrides, conflict rollback."""
import json
from pathlib import Path
from body_audit_postgres_20261011 import sql
from other_work_postgres_20261014 import emit
from test_body_audit_postgres_20261011 import run, signature

ROOT=Path(__file__).resolve().parents[1]


def setup(plan):
    run('''DROP SCHEMA public CASCADE; CREATE SCHEMA public;
    CREATE TABLE anime_subtitle_files(dataset_key TEXT,pid TEXT,sha256 TEXT,pack_id TEXT,member_path TEXT,target_source TEXT,target_id TEXT,reanime_id TEXT,review_status TEXT,reason TEXT,work_title_ko TEXT,work_format TEXT,work_confirmed BOOLEAN,is_manual BOOLEAN DEFAULT FALSE,PRIMARY KEY(dataset_key,pid));
    CREATE TABLE anime_subtitle_confirmed_episodes(map_id BIGINT PRIMARY KEY,target_id TEXT,target_episode INTEGER);
    INSERT INTO anime_subtitle_confirmed_episodes VALUES(1,'user_episode',13);''')
    for c in plan['changes']['registry']:
        if c['table']!='subsubsub_file_registry_v1': continue
        v={'dataset_key':'subsubsub_20261009','pid':c['key']['pid'], **{k:c['before'][k] for k in ('sha256','pack_id','member_path','target_source','target_id','reanime_id','review_status')}, 'work_confirmed':True}
        run('INSERT INTO anime_subtitle_files ('+','.join(v)+') VALUES ('+','.join(sql(x) for x in v.values())+');')


def main():
    plan=json.loads((ROOT/'data/matching/other-work-20261014.json').read_text()); delta=emit(plan)
    setup(plan); run(delta)
    assert run("SELECT count(*) FROM anime_subtitle_files WHERE target_source='AniList' AND target_id='57' AND work_title_ko='BECK';")==['26']
    assert run('SELECT target_id,target_episode FROM anime_subtitle_confirmed_episodes;')==['user_episode|13']
    before=signature(); run(delta); assert signature()==before
    setup(plan); pid=plan['changes']['registry'][0]['key']['pid']
    run("UPDATE anime_subtitle_files SET is_manual=TRUE,target_id='user_choice',reanime_id='user_stream' WHERE pid="+sql(pid)+';')
    run(delta); assert run('SELECT target_id,reanime_id FROM anime_subtitle_files WHERE pid='+sql(pid)+';')==['user_choice|user_stream']
    setup(plan); run("UPDATE anime_subtitle_files SET sha256='changed' WHERE pid="+sql(pid)+';')
    before=signature(); run(delta,False); assert signature()==before
    setup(plan); run("UPDATE anime_subtitle_files SET target_id='unexpected' WHERE pid="+sql(pid)+';')
    before=signature(); run(delta,False); assert signature()==before
    print('PASS: 26 BECK files, rerun, manual overrides, unchanged episodes, hash/target conflict rollback')


if __name__=='__main__': main()
