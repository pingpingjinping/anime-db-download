#!/usr/bin/env python3
"""Emit an idempotent PostgreSQL delta; never change an is_manual file."""
import json,sys
from pathlib import Path
def sql(v):
 if v is None:return 'NULL'
 if isinstance(v,bool):return 'TRUE' if v else 'FALSE'
 if isinstance(v,int):return str(v)
 return "'"+str(v).replace("'","''")+"'"
def emit(plan):
 out=['BEGIN;','SET LOCAL lock_timeout = \'15s\';','LOCK TABLE anime_subtitle_files,anime_subtitle_confirmed_episodes IN SHARE ROW EXCLUSIVE MODE;', 'CREATE TEMP TABLE body_audit_eligible(dataset_key TEXT,pid TEXT,PRIMARY KEY(dataset_key,pid));']
 for f in plan['files']:
  a=f['after'];b=f['before'];rb=f['release_before'];key=f"dataset_key={sql(f['dataset_key'])} AND pid={sql(f['pid'])}"
  fixed=' AND '.join(f'{k} IS NOT DISTINCT FROM {sql(a[k])}' for k in ('sha256','pack_id','member_path','zip_relative_path'))
  targets=' OR '.join('('+ ' AND '.join(f'{k} IS NOT DISTINCT FROM {sql(v[k])}' for k in ('target_source','target_id')) +')' for v in [b,a,rb])
  out.append(f"DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM anime_subtitle_files WHERE {key}) THEN RAISE EXCEPTION 'Missing reviewed file {f['pid']}'; END IF; IF EXISTS(SELECT 1 FROM anime_subtitle_files WHERE {key} AND NOT is_manual AND NOT ({fixed} AND ({targets}))) THEN RAISE EXCEPTION 'Reviewed file changed {f['pid']}'; END IF; END $$;")
  out.append(f'INSERT INTO body_audit_eligible SELECT dataset_key,pid FROM anime_subtitle_files WHERE {key} AND NOT is_manual;')
  vals={k:a[k] for k in ('target_source','target_id','review_status','reason','work_format','reanime_id')};vals['work_confirmed']=a['work_confirmed']=='t'
  # Preserve existing localized titles and unrelated user-visible metadata.
  out.append('UPDATE anime_subtitle_files SET '+','.join(k+'='+sql(v) for k,v in vals.items())+f' WHERE {key} AND NOT is_manual;')
 files={(f['dataset_key'],f['pid']):f for f in plan['files']}
 for e in plan['episodes']:
  a=e['after'];key=f"dataset_key={sql(a['dataset_key'])} AND map_id={e['release_map_id']}";eligible=f"EXISTS(SELECT 1 FROM body_audit_eligible WHERE dataset_key={sql(a['dataset_key'])} AND pid={sql(e['owner_pid'])})"
  vals={k:a[k] for k in ('dataset_key','bundle_id','file_id','target_source','target_id','source_episode','status')};vals.update(map_id=e['release_map_id'],target_episode=int(a['target_episode']))
  same=' AND '.join(f'{k} IS NOT DISTINCT FROM {sql(v)}' for k,v in vals.items())
  if e['before']:
   # Before may be the original OVA match, the audited hold, or already corrected.
   fixed=' AND '.join(f'{k} IS NOT DISTINCT FROM {sql(a[k])}' for k in ('bundle_id','file_id','source_episode'))
   alternatives=[]
   for v in (e['before'],e['release_before'],a):
    alternatives.append('('+' AND '.join(k+' IS NOT DISTINCT FROM '+sql(int(v[k]) if k=='target_episode' and v[k] is not None else v[k]) for k in ('target_source','target_id','target_episode','status'))+')')
   out.append(f"DO $$ BEGIN IF {eligible} AND NOT EXISTS(SELECT 1 FROM anime_subtitle_confirmed_episodes WHERE {key} AND {fixed} AND ({' OR '.join(alternatives)})) THEN RAISE EXCEPTION 'Existing episode changed {e['release_map_id']}'; END IF; END $$;")
   out.append('UPDATE anime_subtitle_confirmed_episodes SET '+','.join(k+'='+sql(vals[k]) for k in ('target_source','target_id','target_episode','status'))+f' WHERE {key} AND {eligible};')
  else:
   origin=f"dataset_key={sql(a['dataset_key'])} AND pid={sql(e['owner_pid'])} AND file_id={sql(a['file_id'])}"
   out.append(f"DO $$ BEGIN IF {eligible} AND NOT EXISTS(SELECT 1 FROM anime_subtitle_file_origins WHERE {origin}) THEN RAISE EXCEPTION 'Missing episode origin {e['release_map_id']}'; END IF; IF {eligible} AND EXISTS(SELECT 1 FROM anime_subtitle_confirmed_episodes WHERE {key} AND NOT ({same})) THEN RAISE EXCEPTION 'Map id collision {e['release_map_id']}'; END IF; END $$;")
   out.append('INSERT INTO anime_subtitle_confirmed_episodes ('+','.join(vals)+') SELECT '+','.join(sql(v) for v in vals.values())+f' WHERE {eligible} ON CONFLICT(dataset_key,map_id) DO NOTHING;')
 for v in plan['entries']:
  payload=dict(v)
  for k in ('source_metadata_json','cross_source_ids_json'):payload[k]=json.loads(payload[k])
  out.append('INSERT INTO anime_catalog_entries(source,source_record_id,payload) VALUES ('+','.join([sql(v['source']),sql(v['source_record_id']),sql(json.dumps(payload,ensure_ascii=False))+'::jsonb'])+') ON CONFLICT(source,source_record_id) DO NOTHING;')
 for l in plan['links']:
  v={k:l[k] for k in ('dataset_key','target_source','target_id','reanime_id','link_status')};v['automatic_eligible']=l['automatic_eligible']=='t'
  out.append('INSERT INTO anime_subtitle_work_reanime_links ('+','.join(v)+') VALUES ('+','.join(sql(x) for x in v.values())+') ON CONFLICT(dataset_key,target_source,target_id,reanime_id) DO NOTHING;')
 out+=['SELECT count(*) AS applied_files,54-count(*) AS preserved_manual_files FROM body_audit_eligible;', 'COMMIT;']
 return '\n'.join(out)+'\n'
if __name__=='__main__':
 plan=json.loads(Path(sys.argv[1]).read_text());Path(sys.argv[2]).write_text(emit(plan))
