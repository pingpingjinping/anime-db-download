#!/usr/bin/env python3
"""Publish tested DBs with pinned-input comparison and rollback copies."""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from split_db_v1_release import sha256

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'other-work-output'


def gh(*args):
    subprocess.run(['gh',*args,'--repo',os.environ['GITHUB_REPOSITORY']],check=True)


def main():
    plan=json.loads((ROOT/'data/matching/other-work-20261014.json').read_text())
    validation=json.loads((OUT/'other-work-validation-20261014.json').read_text())
    for name,digest in validation['sha256'].items():
        if sha256(OUT/name)!=digest: raise ValueError('Validated output changed')
    with tempfile.TemporaryDirectory(prefix='other-work-release-') as tmp:
        tmp=Path(tmp)
        gh('release','download','db-v1','--pattern','anime_four_sources_multilingual_prepared.zip','--pattern','anime_reviewed_subtitle_registry_v1.zip','--dir',str(tmp))
        for name,digest in plan['expected_assets'].items():
            if sha256(tmp/name)!=digest: raise ValueError('Current release changed; refusing replacement')
        manifest={p.name:sha256(p) for p in OUT.iterdir() if p.is_file() and p.suffix!='.db'}
        (OUT/'other-work-sha256-20261014.json').write_text(json.dumps(manifest,indent=2)+'\n')
        assets=[str(p) for p in OUT.iterdir() if p.is_file() and p.suffix!='.db']
        gh('release','create','other-work-20261014',*assets,'--target',os.environ['GITHUB_SHA'],
           '--title','Other-work matching corrections 2026-10-14','--notes-file',str(ROOT/'docs/other-work-20261014.md'),'--latest=false')
        backups=[]
        for name in plan['expected_assets']:
            backup=tmp/(name[:-4]+'.before-other-work-20261014.zip'); shutil.copyfile(tmp/name,backup); backups.append(str(backup))
        gh('release','upload','db-v1',*backups)
        try:
            gh('release','upload','db-v1',*[str(OUT/name) for name in plan['expected_assets']],'--clobber')
            verify=tmp/'verify'; verify.mkdir()
            gh('release','download','db-v1','--pattern','anime_four_sources_multilingual_prepared.zip','--pattern','anime_reviewed_subtitle_registry_v1.zip','--dir',str(verify))
            for name,digest in validation['sha256'].items():
                if sha256(verify/name)!=digest: raise ValueError('Published release hash mismatch')
            gh('release','upload','db-v1',*[str(OUT/name) for name in ('other-work-validation-20261014.json','other-work-20261014.sql','other-work-sha256-20261014.json')])
        except Exception:
            gh('release','upload','db-v1',*[str(tmp/name) for name in plan['expected_assets']],'--clobber')
            raise
    print('Published and redownloaded verified other-work corrections')


if __name__=='__main__': main()
