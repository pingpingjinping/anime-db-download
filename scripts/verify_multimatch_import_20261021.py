"""Exercise the pinned app importer with its only live write intercepted."""
import argparse,contextlib,io,json,runpy,sys,hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

def run(catalog,app_scripts,output):
    catalog=Path(catalog).resolve();scripts=Path(app_scripts).resolve();captured=[];argv=sys.argv[:];digest=hashlib.sha256(catalog.read_bytes()).hexdigest()
    def capture(cmd,**kwargs):
        if cmd[:4]!=['docker','compose','exec','-T']:raise ValueError('Unexpected external importer action')
        text=kwargs['input'];assert text.startswith('\nBEGIN;') and 'COMMIT;' in text
        captured.append({'sql_bytes':len(text.encode()),'sql_sha256':hashlib.sha256(text.encode()).hexdigest()});return SimpleNamespace(returncode=0)
    sys.path.insert(0,str(scripts));sys.argv=[str(scripts/'import-anime-db.py'),str(catalog)]
    try:
        with patch('subprocess.run',capture),contextlib.redirect_stdout(io.StringIO()):data=runpy.run_path(str(scripts/'import-anime-db.py'),run_name='__main__')
    finally:sys.argv=argv;sys.path.remove(str(scripts))
    assert len(captured)==1 and hashlib.sha256(catalog.read_bytes()).hexdigest()==digest
    targets=data['anikoto_targets'];titles=data['manual_titles'];assert len(titles)==2298
    mini=[r for r in targets if r['anikoto_id']=='mini-link-click-nmgts'];assert len(mini)==1 and mini[0]['anikoto_episode_start']==1 and mini[0]['anikoto_episode_end']==3
    ranma=[r for r in targets if r['anikoto_id']=='ranma-1-2-wy2ns' and r['target_id']=='149939'];assert len(ranma)==11
    mapped={}
    for r in ranma:
        for n in range(r['target_episode_start'],r['target_episode_end']+1):
            assert n not in mapped;mapped[n]=r['anikoto_episode_start']+n-r['target_episode_start']
    assert set(mapped)==set(range(1,144)) and set(mapped.values())==set(range(19,162)) and mapped[1]==22 and mapped[7]==19
    for sid in ['jojo-s-bizarre-adventure-battle-tendency-a53pi','rakshasa-street-v9ork']:
        assert all(r['mapping_kind']=='unknown_order' for r in targets if r['anikoto_id']==sid)
    result={'actual_importer_executed_with_live_write_intercepted':True,'live_postgres_connected':False,'catalog_unchanged':True,'source_records':len(data['entries']),'imported_anikoto_target_rows':len(targets),'imported_manual_title_rows':len(titles),'ranma_all_143_episodes_bijective':True,'mini_only_current_three_episodes':True,'recaps_not_whole_episode_links':True,**captured[0]}
    Path(output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--catalog',required=True);p.add_argument('--app-scripts',required=True);p.add_argument('--output',required=True);a=p.parse_args();run(a.catalog,a.app_scripts,a.output)
