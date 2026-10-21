"""Exact-baseline catalog patch. All validation completes before release publishing."""
import argparse,base64,gzip,hashlib,json,shutil,sqlite3,zipfile,collections,copy
from pathlib import Path

CAT='anime_four_sources_multilingual_prepared.zip'
REG='anime_reviewed_subtitle_registry_v1.zip'
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
encode=lambda x:json.dumps(x,ensure_ascii=False,separators=(',',':'))
def connect(path):
    c=sqlite3.connect(path);c.row_factory=sqlite3.Row;return c
def row_for(c,op):
    return c.execute('SELECT * FROM "'+op['table']+'" WHERE '+' AND '.join('"'+k+'" IS ?' for k in op['key']),tuple(op['key'].values())).fetchone()
def apply(c,plan,reverse=False):
    c.execute('BEGIN IMMEDIATE')
    try:
        if not reverse:
            for table,sql in plan['new_tables'].items():
                if c.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone():raise ValueError('Patch already applied or incompatible schema')
                c.execute(sql)
        operations=list(reversed(plan['operations'])) if reverse else plan['operations']
        for op in operations:
            before=op['after'] if reverse else op['before'];after=op['before'] if reverse else op['after'];actual=row_for(c,op)
            if (before is None and actual is not None) or (before is not None and (actual is None or any(actual[k]!=v for k,v in before.items()))):raise ValueError('Exact row precondition failed: '+op['table']+str(op['key']))
            if before is None:
                c.execute('INSERT INTO "'+op['table']+'" ('+','.join('"'+k+'"' for k in after)+') VALUES('+','.join('?' for _ in after)+')',tuple(after.values()))
            elif after is None:
                c.execute('DELETE FROM "'+op['table']+'" WHERE '+' AND '.join('"'+k+'" IS ?' for k in op['key']),tuple(op['key'].values()))
            else:
                c.execute('UPDATE "'+op['table']+'" SET '+','.join('"'+k+'"=?' for k in after)+' WHERE '+' AND '.join('"'+k+'" IS ?' for k in op['key']),(*after.values(),*op['key'].values()))
        if reverse:
            for table in reversed(plan['new_tables']):c.execute('DROP TABLE "'+table+'"')
        if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or c.execute('PRAGMA foreign_key_check').fetchall():raise ValueError('SQLite validation failed')
        c.commit()
    except Exception:c.rollback();raise

def state(c):
    result={}
    for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        cols=[r[1] for r in c.execute('PRAGMA table_info("'+name+'")')];h=hashlib.sha256()
        for row in c.execute('SELECT * FROM "'+name+'" ORDER BY '+','.join('"'+x+'"' for x in cols)):
            h.update(encode(list(row)).encode());h.update(b'\n')
        result[name]=h.hexdigest()
    return result

def sql_literal(v):
    if v is None:return 'NULL'
    if isinstance(v,(int,float)):return str(v)
    return "'"+str(v).replace("'","''")+"'"
def sql_script(plan,reverse=False):
    out=['BEGIN IMMEDIATE;','CREATE TEMP TABLE multimatch_assert (ok INTEGER);',"CREATE TEMP TRIGGER multimatch_assert_check BEFORE INSERT ON multimatch_assert WHEN NEW.ok!=1 BEGIN SELECT RAISE(ROLLBACK,'Multimatch exact-state precondition failed'); END;"]
    if not reverse:out.extend(plan['new_tables'].values())
    for op in (list(reversed(plan['operations'])) if reverse else plan['operations']):
        before=op['after'] if reverse else op['before'];after=op['before'] if reverse else op['after'];t='"'+op['table']+'"';where=' AND '.join('"'+k+'" IS '+sql_literal(v) for k,v in op['key'].items())
        condition=where+(' AND '+' AND '.join('"'+k+'" IS '+sql_literal(v) for k,v in before.items()) if before else '')
        expected=0 if before is None else 1
        out.append('INSERT INTO multimatch_assert SELECT CASE WHEN (SELECT COUNT(*) FROM '+t+' WHERE '+condition+')='+str(expected)+' THEN 1 ELSE 0 END;')
        if before is None:out.append('INSERT INTO '+t+' ('+','.join('"'+k+'"' for k in after)+') VALUES ('+','.join(sql_literal(v) for v in after.values())+');')
        elif after is None:out.append('DELETE FROM '+t+' WHERE '+where+';')
        else:out.append('UPDATE '+t+' SET '+','.join('"'+k+'"='+sql_literal(v) for k,v in after.items())+' WHERE '+where+';')
    if reverse:out.extend('DROP TABLE "'+t+'";' for t in reversed(plan['new_tables']))
    out+=['DROP TRIGGER multimatch_assert_check;','DROP TABLE multimatch_assert;','COMMIT;'];return '\n'.join(x.rstrip(';')+';' for x in out)+'\n'

def run(input_dir,output,plan_path,rules):
    input_dir,output=Path(input_dir),Path(output);output.mkdir(parents=True,exist_ok=True)
    plan=json.loads(gzip.decompress(base64.b64decode(Path(plan_path).read_bytes())))
    actual_zip=sha(input_dir/CAT)
    accepted=[{'catalog_sha256':plan['baseline_catalog_zip_sha256'],'database_sha256':plan['baseline_database_sha256']},*plan.get('accepted_rebases',[])]
    baseline_match=next((r for r in accepted if r['catalog_sha256']==actual_zip),None)
    assert baseline_match and sha(input_dir/REG)==plan['baseline_registry_zip_sha256'],'Unreviewed release baseline'
    baseline=output/'baseline.db';candidate=output/'anime_four_sources_multilingual_prepared.db';registry=output/'registry.db'
    for archive,path in [(input_dir/CAT,baseline),(input_dir/REG,registry)]:
        with zipfile.ZipFile(archive) as z:
            files=[n for n in z.namelist() if n.endswith(('.db','.sqlite','.sqlite3'))];assert len(files)==1
            path.write_bytes(z.read(files[0]))
    assert sha(baseline)==baseline_match['database_sha256'];shutil.copyfile(baseline,candidate)
    c=connect(candidate);before=state(c);plan=copy.deepcopy(plan)
    # A known matcher repack can change JSON whitespace/key order. Preserve its
    # exact representation only after checking the reviewed JSON meaning.
    for op in plan['operations']:
        if op['before'] is None or op['table'] in plan['new_tables']:continue
        actual=row_for(c,op)
        if actual is None:raise ValueError('Reviewed old row absent after repack')
        for key,wanted in op['before'].items():
            if actual[key]==wanted:continue
            if (key.endswith('_json') or op['table'] in ['metadata','anikoto_match_metadata'] and key=='value') and isinstance(actual[key],str) and isinstance(wanted,str):
                try:same=json.loads(actual[key])==json.loads(wanted)
                except ValueError:same=False
                if same:op['before'][key]=actual[key];continue
            raise ValueError('Reviewed value changed in repack: '+op['table']+str(op['key'])+' '+key)
    apply(c,plan);after=state(c)
    import reviewed_matching_guard as guard
    checked=guard.verify_role(c,'catalog',guard.read_rules(rules))
    probe=connect(':memory:');c.backup(probe);apply(probe,plan,True);assert state(probe)==before;apply(probe,plan);assert state(probe)==after
    try:apply(probe,plan);raise AssertionError('Reapply must reject')
    except ValueError:pass
    assert state(probe)==after
    forward=sql_script(plan);rollback=sql_script(plan,True)
    sqlcopy=connect(':memory:');b=connect(baseline);b.backup(sqlcopy);b.close();sqlcopy.executescript(forward);assert state(sqlcopy)==after;sqlcopy.executescript(rollback);assert state(sqlcopy)==before;sqlcopy.close()
    for item in plan['removed_links']:
        if item['source']=='Anissia':
            refs=json.loads(c.execute("SELECT cross_source_ids_json FROM entries WHERE source='Anissia' AND source_record_id=?",(item['id'],)).fetchone()[0]);field={'AniList':'anilist_ids','Onnada':'onnada_ids','ReAnime':'reanime_ids'}[item['target_source']];assert item['target_id'] not in refs.get(field,[])
        else:assert not c.execute('SELECT 1 FROM anikoto_work_link_targets WHERE anikoto_id=? AND target_source=? AND target_id=?',(item['id'],item['target_source'],item['target_id'])).fetchone()
    assert not c.execute('SELECT 1 FROM anikoto_work_link_targets t LEFT JOIN entries e ON e.source=t.target_source AND e.source_record_id=t.target_id WHERE e.source IS NULL LIMIT 1').fetchone()
    assert c.execute('SELECT count(*) FROM multimatch_search_works_20261021').fetchone()[0]==881
    import export_cloudflare_search_sql as search
    reg=search.read_only(registry);original=search.read_only(baseline)
    oldworks,oldaliases,oldcounts,oldconfirmed=search.build(original,reg)
    works,aliases,counts,confirmed=search.build(c,reg)
    assert {r[0] for r in oldworks}<={r[0] for r in works} and sum(r[4] for r in oldworks)==sum(r[4] for r in works)
    assert confirmed==oldconfirmed and len(works)+len(aliases)<=35000
    search.emit_sql(works,aliases,output/'search-import.sql')
    # Every reviewed work must remain independently addressable by its Korean label.
    amap=collections.defaultdict(set)
    for wid,label in aliases:amap[label].add(wid)
    checks=[]
    for row in c.execute('SELECT * FROM multimatch_search_works_20261021'):
        hits=amap[search.normalized_title(row['title_ko'])];assert hits, row['work_id']
        checks.append({'identity_ref':row['identity_ref'],'title':row['title_ko'],'work_ids':sorted(hits)})
    # Actual app importer: create its SQL without connecting to any live Postgres.
    for name,text in [('apply.sql',forward),('rollback.sql',rollback)]: (output/name).write_text(text,encoding='utf-8')
    c.close();original.close();reg.close();probe.close()
    archive=output/CAT
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=8) as z:
        info=zipfile.ZipInfo('anime_four_sources_multilingual_prepared.db',(2026,10,21,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;z.writestr(info,candidate.read_bytes())
    report={'batch':plan['batch'],'summary':plan['summary'],'base_catalog_sha256':actual_zip,'base_database_sha256':baseline_match['database_sha256'],'rebase_provenance':baseline_match.get('provenance'),'base_registry_sha256':plan['baseline_registry_zip_sha256'],'catalog_sha256':sha(archive),'database_sha256':sha(candidate),'guard_rows':checked,'operations':len(plan['operations']),'rollback_and_reapply_verified':True,'sql_forward_and_rollback_verified':True,'search':{'works_before':len(oldworks),'works_after':len(works),'aliases_before':len(oldaliases),'aliases_after':len(aliases),'old_cards_preserved':True,'subtitle_counts_preserved':True,'all_881_korean_labels_tested':True,'sql_sha256':sha(output/'search-import.sql')},'live_playback_tested':False,'production_published':False}
    (output/'multimatch-20261021-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (output/'korean-search-checks.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2));return report
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--input',required=True);p.add_argument('--output',required=True);p.add_argument('--plan',required=True);p.add_argument('--rules',required=True);a=p.parse_args();run(a.input,a.output,a.plan,a.rules)
