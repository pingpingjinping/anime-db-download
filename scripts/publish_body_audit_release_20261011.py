#!/usr/bin/env python3
"""Publish verified outputs, preserve pinned inputs, restore inputs on failed replacement."""
import hashlib,json,os,subprocess,tempfile,shutil
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'body-output'
PLAN=json.loads((ROOT/'data/matching/body-audit-20261011.json').read_text())
def gh(*args):return subprocess.run(['gh',*args,'--repo',os.environ['GITHUB_REPOSITORY']],check=True)
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
manifest={p.name:digest(p) for p in OUT.iterdir() if p.is_file() and p.suffix!='.db'}
(OUT/'body-audit-sha256.json').write_text(json.dumps(manifest,indent=2))
notes=ROOT/'docs/body-audit-20261011.md'
assets=[str(p) for p in OUT.iterdir() if p.is_file() and p.suffix!='.db']
gh('release','create','body-audit-20261011',*assets,'--target',os.environ['GITHUB_SHA'],'--title','Original-body subtitle corrections 2026-10-11','--notes-file',str(notes),'--latest=false')
with tempfile.TemporaryDirectory(prefix='body-audit-cas-') as tmp:
 tmp=Path(tmp)
 gh('release','download','db-v1','--pattern','anime_four_sources_multilingual_prepared.zip','--pattern','anime_reviewed_subtitle_registry_v1.zip','--dir',str(tmp))
 for asset,old in PLAN['expected_assets'].items():
  if digest(tmp/asset)!=old:raise RuntimeError('db-v1 changed after inspection; versioned release available, current release unchanged')
 backups=[]
 for asset in PLAN['expected_assets']:
  backup=tmp/(asset[:-4]+'.before-body-audit-20261011.zip');shutil.copyfile(tmp/asset,backup);backups.append(str(backup))
 gh('release','upload','db-v1',*backups)
 try:
  gh('release','upload','db-v1',*[str(OUT/a) for a in PLAN['expected_assets']],'--clobber')
  verified=tmp/'verify';verified.mkdir()
  gh('release','download','db-v1','--pattern','anime_four_sources_multilingual_prepared.zip','--pattern','anime_reviewed_subtitle_registry_v1.zip','--dir',str(verified))
  for asset in PLAN['expected_assets']:
   if digest(verified/asset)!=manifest[asset]:raise RuntimeError('Published release hash mismatch')
  gh('release','upload','db-v1',*[str(OUT/a) for a in ('body-audit-20261011.sql','body-audit-sha256.json','body-audit-validation.json','body-audit-20261011.json')],'--clobber')
 except Exception:
  gh('release','upload','db-v1',*[str(tmp/a) for a in PLAN['expected_assets']],'--clobber')
  raise
print('PUBLISHED_AND_REDOWNLOADED',json.dumps(manifest))
