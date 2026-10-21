"""Publish only the validated catalog; retain a hash-named original backup."""
import argparse,json,os,subprocess,shutil
from pathlib import Path
from apply_multimatch_20261021 import CAT,REG,sha
REPO='pingpingjinping/anime-db-download'
def gh(*args):return subprocess.run(['gh',*args],check=True,capture_output=True,text=True).stdout
def assets():return {a['name']:a for a in json.loads(gh('api','repos/'+REPO+'/releases/tags/db-v1'))['assets']}
def digest(a):return a.get('digest','').removeprefix('sha256:') if a else None
def check(expected):
    a=assets()
    for n,h in expected.items():
        if digest(a.get(n))!=h:raise ValueError('Concurrent release change: '+n)
    return a
def rollback_action(current,original,replacement):
    return 'keep' if current==original else 'restore_ours' if current==replacement else 'restore_missing' if current is None else 'preserve_newer'
def run(input_dir,output):
    input_dir,output=Path(input_dir),Path(output);report=json.loads((output/'multimatch-20261021-report.json').read_text())
    baseline={CAT:report['base_catalog_sha256'],REG:report['base_registry_sha256']};wanted={CAT:report['catalog_sha256'],REG:baseline[REG]}
    assert sha(output/CAT)==wanted[CAT] and sha(input_dir/CAT)==baseline[CAT] and sha(input_dir/REG)==baseline[REG]
    assert report['rollback_and_reapply_verified'] and report['sql_forward_and_rollback_verified'] and report['search']['all_881_korean_labels_tested']
    current=check(baseline);backup_name=CAT.replace('.zip','.before-multimatch-20261021-'+baseline[CAT][:12]+'.zip');backup=output/backup_name;shutil.copyfile(input_dir/CAT,backup)
    if backup_name in current:assert digest(current[backup_name])==baseline[CAT]
    else:gh('release','upload','db-v1','--repo',REPO,str(backup))
    check(baseline)
    try:
        gh('release','upload','db-v1','--repo',REPO,str(output/CAT),'--clobber');check(wanted)
        verify=output/'redownloaded';verify.mkdir(exist_ok=True)
        gh('release','download','db-v1','--repo',REPO,'--dir',str(verify),'--pattern',CAT,'--clobber')
        assert sha(verify/CAT)==wanted[CAT];check(wanted)
    except Exception:
        action=rollback_action(digest(assets().get(CAT)),baseline[CAT],wanted[CAT])
        if action=='restore_ours' and digest(assets().get(CAT))==wanted[CAT]:gh('release','upload','db-v1','--repo',REPO,str(input_dir/CAT),'--clobber')
        elif action=='restore_missing' and CAT not in assets():gh('release','upload','db-v1','--repo',REPO,str(input_dir/CAT))
        raise
    report.update(production_published=True,published_and_redownload_verified=True,registry_unchanged=True,backup_asset=backup_name)
    (output/'multimatch-20261021-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    gh('release','upload','db-v1','--repo',REPO,str(output/'multimatch-20261021-report.json'))
    print(json.dumps(report,ensure_ascii=False,indent=2))
if __name__=='__main__':
    if os.environ.get('GITHUB_REPOSITORY')!=REPO:raise SystemExit('Restricted publisher repository')
    p=argparse.ArgumentParser();p.add_argument('--input',required=True);p.add_argument('--output',required=True);a=p.parse_args();run(a.input,a.output)
