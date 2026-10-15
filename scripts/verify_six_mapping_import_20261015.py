#!/usr/bin/env python3
"""Verify actual app projection without executing Docker/Postgres imports."""
import argparse
import ast
from collections import defaultdict
import json
from pathlib import Path
import sqlite3
import sys
import unicodedata

def verify(database, app_scripts):
    sys.path.insert(0,str(app_scripts.resolve()))
    from catalog_core import build_catalog,build_reanime_matches
    tree=ast.parse((app_scripts/'import-anime-db.py').read_text(encoding='utf8'))
    names={'json_object','title_key','title_keys','positive_int','entry_episode_count',
        'entries_compatible','build_equivalent_sources','load_anikoto_rows'}
    functions=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
    assert len(functions)==len(names)
    namespace={'json':json,'unicodedata':unicodedata,'defaultdict':defaultdict}
    exec(compile(ast.Module(body=functions,type_ignores=[]),'<actual-importer-functions>','exec'),namespace)
    with sqlite3.connect(database) as db:
        db.row_factory=sqlite3.Row
        catalog,aliases=build_catalog(db)
        by={(x['source'],x['sourceId']):x for x in catalog}
        expected={
            '405':('356','fate-stay-night-4kv357'),
            '808':('21455','new-game-5f897r'),
            '1074':('98292','new-game-4jx87a'),
            '1316':('100977','cells-at-work-fexf69'),
            '1848':('108631','cells-at-work-w7y74y'),
            '1410':('105662','cells-at-work-the-common-cold-dupbvr'),
            '76':('13601','psycho-pass-pywk3h'),
        }
        for n,(aid,rid) in expected.items():
            item=by[('Anissia',n)]
            assert (item['anilistId'],item['reanimeId'])==(aid,rid),(n,item)
        assert by[('Anissia','1410')]['title']=='일하는 세포 특별편'
        assert by[('Anissia','355')]['reanimeId']==''
        assert by[('Onnada','3019')]['reanimeId']==''
        assert by[('Onnada','384')]['reanimeId']=='shin-tetsujin-28-qhqtp6'
        assert by[('Anissia','2695')]['reanimeId']=='hypnosismic-division-rap-battle-rhyme-anima-plus-qtrm2y'
        entries=list(db.execute('SELECT * FROM entries'))
        groups=namespace['build_equivalent_sources'](entries,catalog)
        for first,second in [('356','19603'),('356','20792'),('21455','98292'),('100977','108631'),('13601','20513')]:
            assert ('AniList',second) not in groups[('AniList',first)],(first,second)
        for key in [('Anissia','355'),('Onnada','3019')]:
            assert not any(source in ('ReAnime','AniList') for source,_ in groups[key]),(key,groups[key])
        _,targets,lookup=namespace['load_anikoto_rows'](db,entries,catalog)
        for n,ak in [('405','fate-stay-night-les6a'),('808','new-game-awd6g'),('1316','cells-at-work-1x5ae'),('1410','cells-at-work-special-nwvlt')]:
            actual={x['anikoto_id'] for x in lookup if (x['lookup_source'],x['lookup_source_id'])==('Anissia',n)}
            assert ak in actual,(n,ak,actual)
        silver=[x for x in lookup if x['lookup_source']=='AniList' and x['lookup_source_id'] in ('100784','101925') and x['anikoto_id']=='gintama-silver-soul-arc-xu1do']
        assert {(x['lookup_source_id'],x['anikoto_episode_start'],x['anikoto_episode_end'],x['target_episode_start'],x['target_episode_end']) for x in silver}=={
            ('100784',1,12,1,12),('101925',13,26,1,14)}
        special=[x for x in build_reanime_matches(db,catalog) if x['reanimeId']=='cells-at-work-the-common-cold-dupbvr']
        assert len(special)==1 and special[0]['koreanTitle']=='일하는 세포 특별편'
        assert not any(x['lookup_source']=='Onnada' and x['lookup_source_id']=='384' and x['anikoto_id']=='gigantor-wcea8' for x in lookup)
    report={'actual_app_projection':'passed','isolated_seasons':True,'new_edit_video_fallback':False,
        'special_title':'일하는 세포 특별편','silver_soul_video_ranges':'1-12 -> front1-12;13-26 -> back1-14',
        'latest_hypnosismic_plus_preserved':True}
    print(json.dumps(report,ensure_ascii=False))
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--catalog',type=Path,required=True);p.add_argument('--app-scripts',type=Path,required=True)
    a=p.parse_args();verify(a.catalog,a.app_scripts)
