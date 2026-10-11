"""Publish the user-requested db-v1 correction after exact baseline validation."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys
import traceback
import zipfile

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
from apply_release_plan import ASSETS,digest

REPO='pingpingjinping/anime-db-download'
TAG='db-v1'
def gh(*arguments):
    return subprocess.run(['gh',*arguments,'--repo',REPO],check=True,capture_output=True,text=True).stdout

def download(destination):
    destination.mkdir(parents=True,exist_ok=True)
    for filename in ASSETS.values():gh('release','download',TAG,'--pattern',filename,'--dir',str(destination),'--clobber')

def check_baseline(destination,request):
    for filename,expected in request['baseline_assets'].items():
        assert digest(destination/filename)==expected, f'Current Release baseline changed: {filename}'

def publish():
    assert os.environ.get('GITHUB_REPOSITORY')==REPO
    assert os.environ.get('GITHUB_REF')=='refs/heads/codex/publish-matching-20261011'
    request=json.loads((HERE/'publish-request.json').read_text())
    assert request['release_tag']==TAG and request['new_episode_assignments']==0
    assert digest(HERE/'release-plan.json')==request['plan_sha256']
    root=Path(os.environ['RUNNER_TEMP'])/'matching-20261011'
    current=root/'current';output=root/'output';preflight=root/'preflight';verified=root/'verified'
    download(current);check_baseline(current,request)
    subprocess.run([sys.executable,str(HERE/'apply_release_plan.py'),'--plan',str(HERE/'release-plan.json'),
                    '--input',str(current),'--output',str(output)],check=True)
    manifest=json.loads((output/'matching-release-validation.json').read_text())
    backup=root/'anime_matching_20261011_original_release_backup.zip'
    with zipfile.ZipFile(backup,'w',compression=zipfile.ZIP_STORED) as archive:
        for filename in ASSETS.values():archive.write(current/filename,filename)
        archive.writestr('baseline-sha256.json',json.dumps(request['baseline_assets'],indent=2)+'\n')
    names={item['name'] for item in json.loads(gh('release','view',TAG,'--json','assets'))['assets']}
    if backup.name in names:
        existing=root/'existing-backup';existing.mkdir(exist_ok=True)
        gh('release','download',TAG,'--pattern',backup.name,'--dir',str(existing))
        with zipfile.ZipFile(existing/backup.name) as archive:
            for filename,expected in request['baseline_assets'].items():
                assert hashlib.sha256(archive.read(filename)).hexdigest()==expected,'Existing backup differs'
    else:
        gh('release','upload',TAG,str(backup))
    # The repository's other db-v1 writers share this workflow's concurrency
    # group. Recheck immediately before replacing either active asset.
    download(preflight);check_baseline(preflight,request)
    modified=False
    try:
        for filename in ASSETS.values():
            modified=True
            gh('release','upload',TAG,str(output/filename),'--clobber')
        download(verified)
        for filename in ASSETS.values():
            assert digest(verified/filename)==manifest['assets'][filename]['after_sha256'],f'Uploaded asset verification failed: {filename}'
        gh('release','upload',TAG,str(output/'matching-release-validation.json'),str(HERE/'anime_matching_review_20261011.zip'),'--clobber')
    except BaseException:
        if modified:
            try:
                for filename in ASSETS.values():gh('release','upload',TAG,str(current/filename),'--clobber')
                rollback=root/'rollback';download(rollback);check_baseline(rollback,request)
                status='failed_rolled_back'
            except BaseException:
                status='failed_rollback_uncertain'
            (HERE/'publication.json').write_text(json.dumps({'status':status,'run_id':os.environ.get('GITHUB_RUN_ID')},indent=2)+'\n')
        raise
    result={'status':'published_verified','repository':REPO,'release_tag':TAG,
            'run_id':os.environ.get('GITHUB_RUN_ID'),'backup_asset':backup.name,
            'new_episode_assignments':0,'assets':manifest['assets']}
    (HERE/'publication.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':
    (HERE/'publication.json').unlink(missing_ok=True)
    try:publish()
    except BaseException as exc:
        if not (HERE/'publication.json').exists():
            (HERE/'publication.json').write_text(json.dumps({'status':'failed_before_active_asset_changes',
              'run_id':os.environ.get('GITHUB_RUN_ID'),'error':str(exc)},ensure_ascii=False,indent=2)+'\n')
        traceback.print_exc()
        sys.exit(1)
