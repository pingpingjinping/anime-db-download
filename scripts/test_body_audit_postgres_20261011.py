#!/usr/bin/env python3
"""Exercise the delta against PostgreSQL: apply, rerun, manual edit, conflict rollback."""
import json,subprocess,sys
from pathlib import Path
from body_audit_postgres_20261011 import emit,sql
PLAN=Path(__file__).resolve().parents[1]/'data/matching/body-audit-20261011.json'
def run(s,success=True):
 p=subprocess.run(['psql','-X','-v','ON_ERROR_STOP=1','-At'],input=s,text=True,capture_output=True)
 assert (p.returncode==0)==success,(p.stdout,p.stderr)
 return p.stdout.strip().splitlines()
def setup(plan):
 run('''DROP SCHEMA public CASCADE;CREATE SCHEMA public;
 CREATE TABLE anime_subtitle_files(dataset_key TEXT,pid TEXT,sha256 TEXT,pack_id TEXT,member_path TEXT,zip_relative_path TEXT,target_source TEXT,target_id TEXT,review_status TEXT,reason TEXT,work_format TEXT,reanime_id TEXT,work_confirmed BOOLEAN,is_manual BOOLEAN DEFAULT FALSE,PRIMARY KEY(dataset_key,pid));
 CREATE TABLE anime_subtitle_file_origins(dataset_key TEXT,pid TEXT,file_id TEXT,PRIMARY KEY(dataset_key,pid,file_id));
 CREATE TABLE anime_subtitle_confirmed_episodes(dataset_key TEXT,map_id BIGINT,bundle_id TEXT,file_id TEXT,target_source TEXT,target_id TEXT,source_episode TEXT,target_episode INTEGER,status TEXT,PRIMARY KEY(dataset_key,map_id));
 CREATE TABLE anime_catalog_entries(source TEXT,source_record_id TEXT,payload JSONB,PRIMARY KEY(source,source_record_id));
 CREATE TABLE anime_subtitle_work_reanime_links(dataset_key TEXT,target_source TEXT,target_id TEXT,reanime_id TEXT,link_status TEXT,automatic_eligible BOOLEAN,PRIMARY KEY(dataset_key,target_source,target_id,reanime_id));''')
 s=[]
 for f in plan['files']:
  b=f['before'];v={k:b[k] for k in ('dataset_key','pid','sha256','pack_id','member_path','zip_relative_path','target_source','target_id','review_status','reason','work_format','reanime_id')};v['work_confirmed']=b['work_confirmed']=='t';s.append('INSERT INTO anime_subtitle_files ('+','.join(v)+') VALUES ('+','.join(sql(x) for x in v.values())+');')
 for e in plan['episodes']:
  a=e['after'];s.append('INSERT INTO anime_subtitle_file_origins VALUES ('+','.join(sql(v) for v in [a['dataset_key'],e['owner_pid'],a['file_id']])+') ON CONFLICT DO NOTHING;')
  if e['before']:
   v=dict(e['release_before'],dataset_key=a['dataset_key'],map_id=e['release_map_id']);s.append('INSERT INTO anime_subtitle_confirmed_episodes ('+','.join(v)+') VALUES ('+','.join(sql(x) for x in v.values())+');')
 run('\n'.join(s))
def signature():return run("SELECT md5(string_agg(row_to_json(f)::text,'' ORDER BY pid)) FROM anime_subtitle_files f; SELECT md5(string_agg(row_to_json(e)::text,'' ORDER BY map_id)) FROM anime_subtitle_confirmed_episodes e;")
def main():
 plan=json.loads(PLAN.read_text());delta=emit(plan);setup(plan);run(delta);assert run('SELECT count(*) FROM anime_subtitle_confirmed_episodes;')==['84'];before=signature();run(delta);assert signature()==before
 setup(plan);manual=next(f for f in plan['files'] if f['case']=='C18');run("UPDATE anime_subtitle_files SET is_manual=TRUE,target_id='user_choice' WHERE pid="+sql(manual['pid'])+';');run(delta);assert run('SELECT count(*) FROM anime_subtitle_confirmed_episodes;')==['82'];assert run('SELECT target_id FROM anime_subtitle_files WHERE pid='+sql(manual['pid'])+';')==['user_choice']
 setup(plan);conflict=plan['files'][-1];run("UPDATE anime_subtitle_files SET sha256='changed' WHERE pid="+sql(conflict['pid'])+';');before=signature();run(delta,False);assert signature()==before
 setup(plan);e=next(e for e in plan['episodes'] if not e['before']);run('INSERT INTO anime_subtitle_confirmed_episodes(dataset_key,map_id,file_id,status) VALUES ('+','.join(sql(x) for x in [e['after']['dataset_key'],e['release_map_id'],'conflict','manual'])+');');before=signature();run(delta,False);assert signature()==before
 print('PASS: exact apply, idempotent rerun, preserved manual file and episodes, hash conflict rollback, numeric map collision rollback')
if __name__=='__main__':main()
