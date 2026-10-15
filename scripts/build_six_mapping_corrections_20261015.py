#!/usr/bin/env python3
"""Apply the six user-approved work corrections to the pinned latest release.

Only work identity, edition metadata and video episode ranges are changed.
Every subtitle file/content/confirmed episode row must remain byte-for-byte equal.
"""
import argparse
import json
from pathlib import Path
import sqlite3

from split_db_v1_release import (extract_database, pack_database, sha256,
    table_digest, validate_catalog_db, validate_registry_db)

CAT = 'anime_four_sources_multilingual_prepared.zip'
REG = 'anime_reviewed_subtitle_registry_v1.zip'
BASELINE = {
    CAT: '55f3caa3c1224d25efeb8301c0ca5f404cb7de9e4dbaa3b194c7e71f29fd0eab',
    REG: 'f45473d2be3535abd8aff323fa4a35fd5bfb474b7956e1d7ca6fa804c5123b71',
}
BATCH = 'six-mapping-corrections-20261015'
SILVER = 'gintama-silver-soul-arc-xu1do'
SPECIAL = 'cells-at-work-the-common-cold-dupbvr'
IRON = 'shin-tetsujin-28-qhqtp6'
ANISSIA = {
    '405': ('356', 'fate-stay-night-4kv357', '1398', 24),
    '808': ('21455', 'new-game-5f897r', '3438', 12),
    '1074': ('98292', 'new-game-4jx87a', '3793', 12),
    '1316': ('100977', 'cells-at-work-fexf69', '3956', 13),
    '1848': ('108631', 'cells-at-work-w7y74y', '4167', 8),
    '76': ('13601', 'psycho-pass-pywk3h', '2447', 22),
    '1410': ('105662', SPECIAL, None, 1),
}
ENTRY_KEYS = {('Anissia', n) for n in (*ANISSIA, '355')} | {
    ('Onnada', '384'), ('Onnada', '3019'), ('ReAnime', SPECIAL)}
ALLOWED = {
    'entries': ('source || char(58) || source_record_id', {s+':'+n for s,n in ENTRY_KEYS}),
    'anissia_year_match': ('CAST(anime_no AS TEXT)', set(ANISSIA) | {'355'}),
    'anissia_year_candidates': ('CAST(anime_no AS TEXT)', set(ANISSIA) | {'355'}),
    'onnada_review_state_v1': ('onnada_id', {'384','3019','3828'}),
    'onnada_review_work_targets_v1': ('onnada_id', {'384','3019'}),
    'onnada_review_anikoto_paths_v1': ('onnada_id', {'384','3019','3828'}),
    'anikoto_catalog': ('source_record_id', {'cells-at-work-special-nwvlt'}),
    'anikoto_work_links': ('anikoto_id', {SILVER}),
    'anikoto_work_link_targets': ('anikoto_id', {SILVER}),
}

def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))

def row(db, source, rid):
    result=db.execute('SELECT * FROM entries WHERE source=? AND source_record_id=?', (source,rid)).fetchone()
    if result is None: raise ValueError(f'Missing source: {source}:{rid}')
    return dict(result)

def update_entry(db, source, rid, cross=None, meta=None, **fields):
    old=row(db,source,rid)
    if cross is not None: fields['cross_source_ids_json']=dump(cross)
    if meta is not None:
        metadata=json.loads(old['source_metadata_json'])
        metadata[BATCH]=meta
        fields['source_metadata_json']=dump(metadata)
    db.execute('UPDATE entries SET '+','.join(k+'=?' for k in fields)+
        ' WHERE source=? AND source_record_id=?', (*fields.values(),source,rid))

def restrict_anissia(db, n, aid, rid, oid, episodes):
    old=row(db,'Anissia',n)
    cross=json.loads(old['cross_source_ids_json'])
    cross.update(anilist_ids=[aid],reanime_ids=[rid],onnada_ids=[oid] if oid else [])
    meta={'decision':'user-approved work identity; no subtitle episode assignment',
          'work_episodes':episodes,'original_source_year_preserved':True}
    fields={}
    if n=='1410':
        fields.update(title_ko='일하는 세포 특별편')
        meta['original_title']=old['title_ko']
    update_entry(db,'Anissia',n,cross,meta,existing_match_status='strong_exact',**fields)
    # Reviewed work correction prevents catalog_core from re-appending old
    # same-year title candidates (Fate's source schedule says 2014, work is 2006).
    status='exact_title_same_year' if n=='1410' else 'reviewed_work_correction'
    db.execute('''UPDATE anissia_year_match SET subject=?,same_year_match_status=?,
        previous_combined_status='strong_exact',previous_anilist_id=?,previous_reanime_id=?,
        review_label='검토된 사항',review_bucket=?,exact_match_count=1
        WHERE anime_no=?''', (fields.get('title_ko',old['title_ko']),status,aid,rid,BATCH,int(n)))
    db.execute('DELETE FROM anissia_year_candidates WHERE anime_no=?',(int(n),))
    db.execute('INSERT INTO anissia_year_candidates VALUES (?,?,?,?,?,?,?,?,?)',
        (int(n),1,'ReAnime',rid,aid,rid,100,'user_confirmed',dump(meta)))

def patch_catalog(db):
    for n,args in ANISSIA.items(): restrict_anissia(db,n,*args)
    # Keep the actual special as an existing independent Korean catalog work.
    special=row(db,'ReAnime',SPECIAL)
    cross=json.loads(special['cross_source_ids_json'])
    cross['anilist_ids']=['105662']  # reject provider placeholder 0
    update_entry(db,'ReAnime',SPECIAL,cross,{'korean_name':'일하는 세포 특별편'},title_ko='일하는 세포 특별편')
    akid='cells-at-work-special-nwvlt'
    ak=json.loads(db.execute('SELECT payload_json FROM anikoto_catalog WHERE source_record_id=?',(akid,)).fetchone()[0])
    ak['title_ko']='일하는 세포 특별편'
    db.execute('UPDATE anikoto_catalog SET payload_json=? WHERE source_record_id=?',(dump(ak),akid))

    # The 11 episode new edit is a distinct edition, with no verified video ID.
    edition={'edition':'new_edit','folder_hint':'PSYCHO-PASS 신편집판',
        'edition_episodes':11,'parent_work':{'source':'AniList','id':'13601'},
        'automatic_video_fallback':False,'automatic_episode_assignment':False}
    update_entry(db,'Anissia','355',{'anilist_ids':[],'reanime_ids':[],'onnada_ids':['3019']},
                 edition,existing_match_status='review')
    update_entry(db,'Onnada','3019',{'anilist_ids':['13601'],'reanime_ids':[]},
                 edition,existing_match_status='review')
    db.execute("""UPDATE anissia_year_match SET same_year_match_status='reviewed_edition',
        previous_combined_status='review',previous_anilist_id='',previous_reanime_id='',
        review_label='주의',review_bucket=? WHERE anime_no=355""",(BATCH,))
    db.execute('DELETE FROM anissia_year_candidates WHERE anime_no=355')
    db.execute("DELETE FROM onnada_review_work_targets_v1 WHERE onnada_id='3019' AND target_source='ReAnime'")
    db.execute("DELETE FROM onnada_review_anikoto_paths_v1 WHERE onnada_id='3019'")
    db.execute("UPDATE onnada_review_work_targets_v1 SET payload_json=? WHERE onnada_id='3019'",(dump(edition),))

    # The 1980 identity was corrected in the newest release. Replace stale
    # secondary 1963/1965 mappings without rewriting raw Onnada's 97 label.
    iron={'decision':'user-confirmed 1980 TV 51; supersedes old 1963/1965 interpretation',
          'work_episodes':51,'source_episode_label_conflict':'raw Onnada 97 preserved',
          'automatic_episode_assignment':False,'anikoto_verified':False}
    update_entry(db,'Onnada','384',{'anilist_ids':['4208'],'reanime_ids':[IRON]},iron,
                 existing_match_status='review')
    db.execute("DELETE FROM onnada_review_work_targets_v1 WHERE onnada_id='384'")
    db.execute("DELETE FROM onnada_review_anikoto_paths_v1 WHERE onnada_id='384'")
    for source,rid in [('AniList','4208'),('ReAnime',IRON)]:
        db.execute('INSERT INTO onnada_review_work_targets_v1 VALUES (?,?,?,?,?,?,?,?)',
            ('384',1,source,rid,'whole','reviewed',0,dump(iron)))

    # Video composite: 12 episode front + 14 episode back. This is a video
    # range relation, not a new assignment for any subtitle file.
    silver_parts=[]
    for order,aid,rid,start,end in [
        (1,'100784','gintama-silver-soul-arc-cwcse8',1,12),
        (2,'101925','gintama-silver-soul-arc-second-half-war-5xdsk8',13,26)]:
        payload={'source_refs':[{'source':'AniList','id':aid},{'source':'ReAnime','id':rid}],
                 'decision':'26 episode Anikoto composite; front12 + back14',
                 'new_subtitle_episode_assignments':0}
        silver_parts.append((SILVER,order,'AniList',aid,'reviewed','range',
                             start,end,1,end-start+1,dump(payload)))
    db.execute('DELETE FROM anikoto_work_link_targets WHERE anikoto_id=?',(SILVER,))
    db.executemany('INSERT INTO anikoto_work_link_targets VALUES (?,?,?,?,?,?,?,?,?,?,?)',silver_parts)
    db.execute("UPDATE anikoto_work_links SET status='multi',payload_json=? WHERE anikoto_id=?",
        (dump({'targets':[dict(zip(['anikoto_id','target_order','target_source','target_id',
            'link_status','mapping_kind','anikoto_episode_start','anikoto_episode_end',
            'target_episode_start','target_episode_end','payload_json'],part)) for part in silver_parts],
            'legacy_single_target_is_not_whole':True}),SILVER))
    front=dict(zip(['anikoto_id','target_order','target_source','target_id','link_status',
        'mapping_kind','anikoto_episode_start','anikoto_episode_end','target_episode_start',
        'target_episode_end','payload_json'],silver_parts[0]))
    front.update(scope='component_or_range',onnada_source_episode_start=14,
        onnada_source_episode_end=25,absolute_episode_start=342,absolute_episode_end=353,
        automatic_episode_assignment=False)
    db.execute('''UPDATE onnada_review_anikoto_paths_v1 SET mapping_kind='range',
        existing_link_status='reviewed',payload_json=?
        WHERE onnada_id='3828' AND anikoto_id=?''',(dump(front),SILVER))
    # Keep review-state projections in sync, preserving the superseded payload
    # under history, so an active old target cannot be resurrected by a consumer.
    for oid in ('384','3019','3828'):
        old=db.execute('SELECT payload_json FROM onnada_review_state_v1 WHERE onnada_id=?',(oid,)).fetchone()
        state=json.loads(old[0]);before=dict(state)
        targets=[dict(x) for x in db.execute('SELECT * FROM onnada_review_work_targets_v1 WHERE onnada_id=?',(oid,))]
        paths=[json.loads(x[0]) for x in db.execute('SELECT payload_json FROM onnada_review_anikoto_paths_v1 WHERE onnada_id=?',(oid,))]
        state.update(anilist_ids=[x['target_id'] for x in targets if x['target_source']=='AniList'],
            reanime_ids=[x['target_id'] for x in targets if x['target_source']=='ReAnime'],
            anikoto_ids=sorted({x['anikoto_id'] for x in paths}),targets=targets,
            existing_anikoto_paths=paths,target_mapping_kinds={x['target_source']+':'+x['target_id']:x['mapping_kind'] for x in targets})
        if oid=='384':
            state.update(status='single',note=iron['decision'],scope=iron,pending_components=[],
                         missing_local_catalog_records=[],source_episode_label='97 (raw); reviewed work 51')
        if oid=='3019':state.update(scope=edition)
        state['superseded_review_history']={BATCH:before}
        db.execute('UPDATE onnada_review_state_v1 SET status=?,payload_json=? WHERE onnada_id=?',
                   (state['status'],dump(state),oid))
        if oid in ('384','3019'):
            e=row(db,'Onnada',oid);meta=json.loads(e['source_metadata_json'])
            prior=meta.get('onnada_manual_review_v1')
            meta['onnada_manual_review_v1']={k:v for k,v in state.items() if k!='superseded_review_history'}
            meta[BATCH]['superseded_manual_review']=prior
            db.execute('UPDATE entries SET source_metadata_json=? WHERE source=? AND source_record_id=?',
                       (dump(meta),'Onnada',oid))

def patch_registry(db):
    for n,(_,rid,_,_) in ANISSIA.items():
        db.execute("DELETE FROM subtitle_work_reanime_links WHERE target_source='Anissia' AND target_id=?",(n,))
        db.execute('INSERT INTO subtitle_work_reanime_links VALUES (?,?,?,?,?)',
                   ('Anissia',n,rid,'user_reviewed_work_identity',0))
    for source,rid in [('Anissia','355'),('Onnada','3019'),('Onnada','384')]:
        db.execute('DELETE FROM subtitle_work_reanime_links WHERE target_source=? AND target_id=?',(source,rid))
    db.execute('INSERT INTO subtitle_work_reanime_links VALUES (?,?,?,?,?)',
               ('Onnada','384',IRON,'user_reviewed_work_identity',0))

def frozen(db, allowed):
    """Snapshot every row outside the explicit work-key allowlist."""
    result={}
    for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        if table not in allowed:
            result[table]=table_digest(db,table)
            continue
        expr,keys=allowed[table];params=sorted(keys)
        rows=db.execute(f'SELECT * FROM "{table}" WHERE {expr} NOT IN ({",".join("?" for _ in params)}) ORDER BY rowid',params)
        import hashlib
        h=hashlib.sha256();count=0
        for r in rows:h.update((dump(list(r))+'\n').encode());count+=1
        result[table]={'rows':count,'sha256':h.hexdigest()}
    return result

REG_ALLOWED={'subtitle_work_reanime_links':
    ('target_source || char(58) || target_id',
     {'Anissia:'+n for n in (*ANISSIA,'355')} | {'Onnada:3019','Onnada:384'})}

def validate_cases(cat,reg):
    for n,(aid,rid,oid,_) in ANISSIA.items():
        c=json.loads(row(cat,'Anissia',n)['cross_source_ids_json'])
        assert c['anilist_ids']==[aid] and c['reanime_ids']==[rid]
        assert c['onnada_ids']==([oid] if oid else [])
        candidates=cat.execute('SELECT anilist_id,reanime_id FROM anissia_year_candidates WHERE anime_no=?',(n,)).fetchall()
        assert all(tuple(x)==(aid,rid) for x in candidates)
    assert row(cat,'Anissia','1410')['title_ko']=='일하는 세포 특별편'
    for source,n in [('Anissia','355'),('Onnada','3019')]:
        c=json.loads(row(cat,source,n)['cross_source_ids_json'])
        assert c['reanime_ids']==[]
        assert not reg.execute('SELECT 1 FROM subtitle_work_reanime_links WHERE target_source=? AND target_id=?',(source,n)).fetchone()
    assert not cat.execute("SELECT 1 FROM onnada_review_anikoto_paths_v1 WHERE onnada_id IN ('384','3019')").fetchone()
    assert {tuple(x) for x in cat.execute("SELECT target_source,target_id FROM onnada_review_work_targets_v1 WHERE onnada_id='384'")}=={('AniList','4208'),('ReAnime',IRON)}
    parts=cat.execute('SELECT target_id,anikoto_episode_start,anikoto_episode_end,target_episode_start,target_episode_end FROM anikoto_work_link_targets WHERE anikoto_id=? ORDER BY target_order',(SILVER,)).fetchall()
    assert [tuple(x) for x in parts]==[('100784',1,12,1,12),('101925',13,26,1,14)]
    assert {x[0] for x in cat.execute("SELECT target_id FROM onnada_review_work_targets_v1 WHERE onnada_id='3828' AND target_source='AniList'")}=={'99714','100784'}
    front=json.loads(cat.execute("SELECT payload_json FROM onnada_review_anikoto_paths_v1 WHERE onnada_id='3828' AND anikoto_id=?",(SILVER,)).fetchone()[0])
    assert front['anikoto_episode_end']==12 and front['onnada_source_episode_end']==25
    # Independent UBW seasons must retain their own identities.
    for oid,aid in [('2968','19603'),('3134','20792')]:
        assert json.loads(row(cat,'Onnada',oid)['cross_source_ids_json'])['anilist_ids']==[aid]
    assert cat.execute('SELECT count(*) FROM multiwork_review_decisions_20261015').fetchone()[0]==236

def build(inputs, output):
    inputs=Path(inputs);output=Path(output);output.mkdir(parents=True,exist_ok=True)
    for asset,expected in BASELINE.items():
        if sha256(inputs/asset)!=expected:raise ValueError(f'Latest release baseline changed: {asset}')
    changes={};result={}
    paths={}
    for asset,role,allowed,patch in [(CAT,'catalog',ALLOWED,patch_catalog),(REG,'registry',REG_ALLOWED,patch_registry)]:
        dbpath=output/asset.replace('.zip','.db');extract_database(inputs/asset,dbpath)
        paths[role]=dbpath
        with sqlite3.connect(dbpath) as db:
            db.row_factory=sqlite3.Row
            before=frozen(db,allowed)
            # Keep a full audit of all potentially affected rows, without files.
            journal={}
            for table,(expr,keys) in allowed.items():
                p=sorted(keys)
                journal[table]={'before':[dict(x) for x in db.execute(f'SELECT * FROM "{table}" WHERE {expr} IN ({",".join("?" for _ in p)}) ORDER BY rowid',p)]}
            patch(db)
            if frozen(db,allowed)!=before:raise ValueError('Unapproved row or table changed')
            for table,(expr,keys) in allowed.items():
                p=sorted(keys)
                journal[table]['after']=[dict(x) for x in db.execute(f'SELECT * FROM "{table}" WHERE {expr} IN ({",".join("?" for _ in p)}) ORDER BY rowid',p)]
            changes[role]=journal
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            assert not db.execute('PRAGMA foreign_key_check').fetchall()
        result[role]=(validate_catalog_db if role=='catalog' else validate_registry_db)(dbpath)
        pack_database(dbpath,output/asset,dbpath.name)
    with sqlite3.connect(paths['catalog']) as c,sqlite3.connect(paths['registry']) as r:
        c.row_factory=sqlite3.Row;validate_cases(c,r)
    report={'batch':BATCH,'baseline_sha256':BASELINE,
            'result_sha256':{n:sha256(output/n) for n in BASELINE},
            'validation':result,'six_work_corrections':['Fate 2006 TV','NEW GAME seasons',
                'Cells seasons and standalone special','PSYCHO-PASS editions',
                'Silver Soul video ranges','Tetsujin 1980 TV'],
            'new_subtitle_episode_assignments':0,'subtitle_file_rows_unchanged':True,
            'unrelated_work_rows_unchanged':True,'review_ledger_236_unchanged':True,
            'notes':['Fate source schedule year retained; legacy year-based UBW fallback removed.',
                'NEW GAME 13/OVA left outside this correction.',
                'Psycho-Pass new edit has no unverified ordinary/season2 video fallback.',
                'Onnada3828 remains Porori13+SilverSoul front12 only.',
                'Tetsujin raw Onnada97 preserved; reviewed work is1980 TV51; no guessed Anikoto ID.']}
    (output/(BATCH+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    (output/(BATCH+'-audit.json')).write_text(json.dumps(changes,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps(report,ensure_ascii=False))
    return report

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--input',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();build(args.input,args.output)
