#!/usr/bin/env python3
"""Apply the reviewed body audit to copies of the two pinned release assets."""
import argparse,copy,json,sqlite3,shutil
from pathlib import Path
from split_db_v1_release import extract_database,pack_database,sha256,table_digest,validate_catalog_db,validate_registry_db
from reviewed_matching_guard import read_rules,verify_role

ROOT=Path(__file__).resolve().parents[1]
PLAN=ROOT/'data/matching/body-audit-20261011.json'
CAT='anime_four_sources_multilingual_prepared.zip'
REG='anime_reviewed_subtitle_registry_v1.zip'
def js(v):return json.dumps(v,ensure_ascii=False,separators=(',',':'))
def row(db,t,key):
 r=db.execute('SELECT * FROM '+t+' WHERE '+' AND '.join(k+'=?' for k in key),tuple(key.values())).fetchone()
 return dict(r) if r else None
def insert(db,t,v):
 db.execute('INSERT INTO '+t+' ('+','.join(v)+') VALUES ('+','.join('?' for _ in v)+')',tuple(v.values()))
def update(db,t,key,v):
 db.execute('UPDATE '+t+' SET '+','.join(k+'=?' for k in v)+' WHERE '+' AND '.join(k+'=?' for k in key),tuple(v.values())+tuple(key.values()))
def pin(rules,db,role,t,key,cols=None):
 definition=rules['roles'][role].setdefault(t,{'primary_key':list(key),'rows':[]})
 previous=next((r for r in definition['rows'] if r['key']==key),None)
 v=row(db,t,key);assert v is not None,(t,key)
 expected={k:json.loads(val) if k.endswith('_json') and isinstance(val,str) else val for k,val in v.items() if k not in key and (cols is None or k in cols or previous and k in previous.get('expected',{}))}
 rule={'key':key,'expected':expected,'reason':'Explicit original-body review body-audit-20261011'}
 if previous:definition['rows'][definition['rows'].index(previous)]=rule
 else:definition['rows'].append(rule)

def build(inputs,out):
 plan=json.loads(PLAN.read_text());out.mkdir(parents=True,exist_ok=True)
 for asset,digest in plan['expected_assets'].items():
  assert sha256(inputs/asset)==digest,('Release asset changed',asset)
 catpath=out/'anime_four_sources_multilingual_prepared.db';regpath=out/'anime_reviewed_subtitle_registry_v1.db'
 extract_database(inputs/CAT,catpath);extract_database(inputs/REG,regpath)
 rules=copy.deepcopy(read_rules(ROOT/'data/matching/before-body-audit-20261011.json'));cats=sqlite3.connect(catpath);regs=sqlite3.connect(regpath)
 for db in (cats,regs):db.row_factory=sqlite3.Row
 verify_role(cats,'catalog',rules);verify_role(regs,'registry',rules)
 before={role:{r[0]:table_digest(db,r[0]) for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} for role,db in [('catalog',cats),('registry',regs)]}
 with cats:
  for v in plan['entries']:
   assert row(cats,'entries',{'source':v['source'],'source_record_id':v['source_record_id']}) is None
   insert(cats,'entries',v);pin(rules,cats,'catalog','entries',{'source':v['source'],'source_record_id':v['source_record_id']})
  cats.execute('DELETE FROM year_summary');cats.execute('INSERT INTO year_summary SELECT year,source,count(*) FROM entries GROUP BY year,source')
  cats.execute("INSERT OR REPLACE INTO metadata VALUES ('body_audit_20261011',?)",(js({'catalog_backfills':4,'not_fresh_provider_fetch':True}),))
  cats.execute("UPDATE metadata SET value=? WHERE key='entry_count'",(js(cats.execute('SELECT count(*) FROM entries').fetchone()[0]),))
 with regs:
  regs.execute('''CREATE TABLE body_audit_confirmed_episodes_v1(dataset_key TEXT NOT NULL,map_id INTEGER NOT NULL,owner_pid TEXT NOT NULL,bundle_id TEXT,file_id TEXT NOT NULL,target_source TEXT NOT NULL,target_id TEXT NOT NULL,source_episode TEXT,target_episode INTEGER NOT NULL,status TEXT NOT NULL,evidence_case TEXT NOT NULL,PRIMARY KEY(dataset_key,map_id))''')
  for f in plan['files']:
   t='packaged_files_v6' if f['dataset_key']=='reviewed_v6_1109' else 'subsubsub_file_registry_v1';key={'pid':f['pid']};before_row=row(regs,t,key)
   assert before_row==f['release_before'],('Unexpected file state',key)
   a=f['after'];updates={k:a[k] for k in ('target_source','target_id','review_status')}
   if t=='packaged_files_v6':
    updates['reason']=a['reason'];e=json.loads(before_row['evidence_json']);e['body_audit_20261011']={'case':f['case'],'reason':a['reason'],'old_target':[before_row['target_source'],before_row['target_id']]};updates['evidence_json']=js(e)
    update(regs,t,key,updates)
    ent=row(cats,'entries',{'source':a['target_source'],'source_record_id':a['target_id']});assert ent is not None
    detail=row(regs,'subtitle_review_details',key)
    detail_updates={'work_title_ko':a['work_title_ko'] or ent['title_ko'],'identified_work':ent['title_romaji'] or ent['title_native'],'season_format':a['work_format'],'reanime_id':a['reanime_id'],'reanime_status':'body_confirmed','quality_notes':a['reason']}
    if detail:update(regs,'subtitle_review_details',key,detail_updates)
    else:insert(regs,'subtitle_review_details',dict(key,**detail_updates,bundle_order=None,candidate_keys_json='[]',search_urls_json='[]',language_review='Original body reviewed in the preceding audit'))
    pin(rules,regs,'registry','subtitle_review_details',key)
   else:
    updates.update(reanime_id=a['reanime_id'],work_confirmed=1)
    payload=json.loads(before_row['payload_json']);payload['body_audit_20261011']={'case':f['case'],'reason':a['reason'],'confirmed_target':[a['target_source'],a['target_id']],'rea­nime_id':a['reanime_id'],'explicit_episode_mapping_table':'body_audit_confirmed_episodes_v1'};updates['payload_json']=js(payload);update(regs,t,key,updates)
    # Candidate hints remain historical, with their status explicitly retired.
    for old in regs.execute("SELECT * FROM subsubsub_file_targets_v1 WHERE pid=? AND relation='recommended_candidate'",(f['pid'],)).fetchall():
     tk={'pid':f['pid'],'target_order':old['target_order']};update(regs,'subsubsub_file_targets_v1',tk,{'relation':'superseded_candidate'});pin(rules,regs,'registry','subsubsub_file_targets_v1',tk)
    n=regs.execute('SELECT coalesce(max(target_order),0) FROM subsubsub_file_targets_v1 WHERE pid=?',(f['pid'],)).fetchone()[0]
    for source,rid in [(a['target_source'],a['target_id']),('ReAnime',a['reanime_id'])]:
     n+=1;tk={'pid':f['pid'],'target_order':n};insert(regs,'subsubsub_file_targets_v1',dict(tk,target_source=source,target_id=rid,relation='body_confirmed',mapping_kind='work_only',automatic_episode_assignment=0,payload_json=js({'case':f['case'],'batch':plan['batch']})));pin(rules,regs,'registry','subsubsub_file_targets_v1',tk)
   pin(rules,regs,'registry',t,key)
  for e in plan['episodes']:
   a=e['after'];key={'map_id':e['release_map_id']}
   if e['before']:
    current=row(regs,'subtitle_confirmed_episodes',key)
    assert all(current[k]==v for k,v in e['release_before'].items()),('Unexpected episode state',key)
    update(regs,'subtitle_confirmed_episodes',key,{k:a[k] if k!='target_episode' else int(a[k]) for k in ('target_source','target_id','target_episode','status')});pin(rules,regs,'registry','subtitle_confirmed_episodes',key)
   else:
    assert row(regs,'subtitle_confirmed_episodes',key) is None
    v={k:a[k] for k in ('dataset_key','bundle_id','file_id','target_source','target_id','source_episode','status')};v.update(map_id=e['release_map_id'],owner_pid=e['owner_pid'],target_episode=int(a['target_episode']),evidence_case=e['case']);insert(regs,'body_audit_confirmed_episodes_v1',v);pin(rules,regs,'registry','body_audit_confirmed_episodes_v1',{'dataset_key':v['dataset_key'],'map_id':v['map_id']})
  for l in plan['links']:
   v={k:l[k] for k in ('target_source','target_id','reanime_id','link_status')};v['automatic_eligible']=int(l['automatic_eligible']=='t');insert(regs,'subtitle_work_reanime_links',v);pin(rules,regs,'registry','subtitle_work_reanime_links',{k:v[k] for k in ('target_source','target_id','reanime_id')})
  regs.execute("INSERT OR REPLACE INTO subtitle_runtime_metadata VALUES ('body_audit_20261011',?)",(js({'files':54,'legacy_episode_target_updates':24,'explicit_manual_episode_rows':60,'automatic_episode_assignments':0,'requires_delta_import':True}),))
 rules['manual_episode_assignments']=60;rules['body_audit_batch']=plan['batch']
 verify_role(cats,'catalog',rules);verify_role(regs,'registry',rules)
 for role,db,changed in [('catalog',cats,{'entries','year_summary','metadata'}),('registry',regs,{'packaged_files_v6','subtitle_review_details','subtitle_confirmed_episodes','subsubsub_file_registry_v1','subsubsub_file_targets_v1','subtitle_work_reanime_links','subtitle_runtime_metadata'})]:
  for t,digest in before[role].items():
   if t not in changed:assert table_digest(db,t)==digest,('Unrelated table changed',role,t)
 assert regs.execute('SELECT count(*) FROM body_audit_confirmed_episodes_v1').fetchone()[0]==60
 assert not regs.execute('PRAGMA foreign_key_check').fetchall()
 cats.close();regs.close();result={'catalog':validate_catalog_db(catpath),'registry':validate_registry_db(regpath),'body_audit_files':54,'legacy_episode_target_updates':24,'explicit_manual_episode_rows':60,'automatic_episode_assignments':0}
 for path,asset in [(catpath,CAT),(regpath,REG)]:pack_database(path,out/asset,path.name)
 (out/'reviewed-corrections-20261011.json').write_text(json.dumps(rules,ensure_ascii=False,indent=2))
 shutil.copyfile(PLAN,out/PLAN.name)
 result['sha256']={asset:sha256(out/asset) for asset in (CAT,REG)}
 (out/'body-audit-validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();build(a.input,a.output)
