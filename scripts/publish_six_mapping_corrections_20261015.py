#!/usr/bin/env python3
"""Back up BOTH latest assets, compare again, publish, then redownload verify."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from build_six_mapping_corrections_20261015 import BASELINE,BATCH,CAT,REG
from split_db_v1_release import sha256

REPO='pingpingjinping/anime-db-download'

def gh(*args):
    return subprocess.run(['gh',*args],check=True,capture_output=True,text=True).stdout

def release_assets():
    release=json.loads(gh('api',f'repos/{REPO}/releases/tags/db-v1'))
    result={}
    for a in release['assets']:
        if a['name'] in result:raise ValueError('Duplicate release asset name')
        result[a['name']]=a
    return result

def digest(asset):
    return (asset or {}).get('digest','').removeprefix('sha256:')

def assert_current(expected):
    assets=release_assets()
    for name,wanted in expected.items():
        if digest(assets.get(name))!=wanted:
            raise ValueError(f'Newer/different release detected; refusing overwrite: {name}')
    return assets

def rollback_action(current,original,replacement):
    """Never replace a third party's newer edit, even on partial upload failure."""
    if current==original:return 'keep'
    if current==replacement:return 'restore_ours'
    if current is None:return 'restore_missing'
    return 'preserve_newer'

def publish(output):
    output=Path(output).resolve()
    report=json.loads((output/(BATCH+'.json')).read_text(encoding='utf8'))
    if report['baseline_sha256']!=BASELINE:raise ValueError('Unexpected baseline report')
    wanted=report['result_sha256']
    if set(wanted)!=set(BASELINE):raise ValueError('Both asset outputs are required')
    for name,expected in wanted.items():
        if sha256(output/name)!=expected:raise ValueError(f'Output changed: {name}')
    assert_current(BASELINE)
    with tempfile.TemporaryDirectory(prefix=BATCH+'-') as td:
        td=Path(td);original=td/'original';original.mkdir()
        gh('release','download','db-v1','--repo',REPO,'--dir',str(original),
           '--pattern',CAT,'--pattern',REG)
        for name,expected in BASELINE.items():
            if sha256(original/name)!=expected:raise ValueError('Baseline changed during download')
        # Immutable backup filenames include the actual baseline hash.
        for name,expected in BASELINE.items():
            backup_name=name.replace('.zip',f'.before-six-mapping-20261015-{expected[:12]}.zip')
            assets=release_assets()
            if backup_name in assets:
                if digest(assets[backup_name])!=expected:raise ValueError('Backup name collision')
            else:
                backup=td/backup_name;shutil.copyfile(original/name,backup)
                gh('release','upload','db-v1','--repo',REPO,str(backup))
        # Backups may take time. Guard against edits made during that time.
        assert_current(BASELINE)
        try:
            gh('release','upload','db-v1','--repo',REPO,str(output/CAT),str(output/REG),'--clobber')
            assert_current(wanted)
            downloaded=td/'verified';downloaded.mkdir()
            gh('release','download','db-v1','--repo',REPO,'--dir',str(downloaded),
               '--pattern',CAT,'--pattern',REG)
            for name,expected in wanted.items():
                if sha256(downloaded/name)!=expected:raise ValueError('Published content hash mismatch')
        except Exception:
            notes=[]
            for name in BASELINE:
                assets=release_assets();asset=assets.get(name)
                action=rollback_action(digest(asset) if asset else None,BASELINE[name],wanted[name])
                if action=='restore_ours':
                    # Recheck immediately before destructive clobber.
                    if digest(release_assets().get(name))==wanted[name]:
                        gh('release','upload','db-v1','--repo',REPO,str(original/name),'--clobber')
                elif action=='restore_missing':
                    # Without --clobber, a concurrent upload is never replaced.
                    if name not in release_assets():
                        gh('release','upload','db-v1','--repo',REPO,str(original/name))
                elif action=='preserve_newer':notes.append(f'Preserved newer external update: {name}')
            if notes:print('\n'.join(notes))
            raise
        gh('release','upload','db-v1','--repo',REPO,str(output/(BATCH+'.json')),
           str(output/(BATCH+'-audit.json')))
        assert_current(wanted)
    print(json.dumps({'release':f'https://github.com/{REPO}/releases/tag/db-v1',
        'published_and_redownload_verified':wanted,'both_baseline_assets_backed_up':True}))

if __name__=='__main__':
    if os.environ.get('GITHUB_REPOSITORY')!=REPO:raise SystemExit('Publisher is restricted to the authorized repository')
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    publish(p.parse_args().output)
