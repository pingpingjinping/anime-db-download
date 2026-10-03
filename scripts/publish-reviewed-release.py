#!/usr/bin/env python3
"""Publish only after local migration, input preservation and importer validation."""
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import zipfile

ROOT=pathlib.Path(os.environ['RUNNER_TEMP'])/'reviewed-release'
ROOT.mkdir(parents=True,exist_ok=True)
REPO=os.environ['GITHUB_REPOSITORY']
ASSET='anime_four_sources_multilingual_prepared.zip'
EXPECTED='592ae7461afc5fed2d33844f5f863fadf25aae022fe5acf8b3778f7ed94b27e8'
CAPSULE='948dce27d9ff32421ded31930167f4b548da0932f9aed283ef468b1a9287b6d5'
STREAM_COMMIT='f97eed1798c10f2870d6bde04627a7779f892e01'

def gh(*args):
    return subprocess.check_output(['gh',*args],text=True)
def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for part in iter(lambda:f.read(1024*1024),b''):h.update(part)
    return h.hexdigest()
def metadata():return json.loads(gh('api',f'repos/{REPO}/releases/tags/db-v1'))
def asset(meta,name):
    matches=[a for a in meta['assets'] if a['name']==name]
    if len(matches)!=1:raise ValueError('Expected one asset: '+name)
    return matches[0]

before=metadata();current=asset(before,ASSET)
assert current['digest']=='sha256:'+EXPECTED,'Current catalog release changed; rerun from its new catalog'
previous=ROOT/'previous';previous.mkdir()
gh('release','download','db-v1','--repo',REPO,'--pattern',ASSET,'--dir',str(previous))
assert digest(previous/ASSET)==EXPECTED
capsule=ROOT/'reviewed.sqlite.gz'
parts=sorted(pathlib.Path('release-inputs').glob('part_*.bin'))
assert len(parts)==11
with capsule.open('wb') as out:
    for part in parts:
        with part.open('rb') as src:shutil.copyfileobj(src,out)
assert digest(capsule)==CAPSULE
output=ROOT/'candidate';output.mkdir()
database=output/'anime_four_sources_multilingual_prepared.db'
subprocess.run([sys.executable,'scripts/merge-reviewed-runtime.py',str(previous/ASSET),str(capsule),str(database),
 '--expected-latest',EXPECTED,'--expected-capsule',CAPSULE],check=True)

# Exercise the unchanged catalog importer from the exact validated streaming revision.
check_root=ROOT/'import-check';(check_root/'scripts').mkdir(parents=True)
for name in ('import-anime-db.py','catalog_core.py'):
    import base64
    data=json.loads(gh('api',f'repos/pingpingjinping/streaming-project/contents/scripts/{name}?ref={STREAM_COMMIT}'))
    (check_root/'scripts'/name).write_bytes(base64.b64decode(data['content']))
subprocess.run([sys.executable,'validation/verify_import_compatibility.py',str(database),str(check_root),str(output/'import_compatibility.json')],check=True)
report=json.loads(database.with_suffix('.merge.json').read_text())
assert report['current_catalog_rows_preserved']==43348
assert report['existing_catalog_rows_modified']==0
assert report['reviewed_file_rows_sha256']=='62bd9820264c2960ffb938b4ca7f63e3d8c4088922659f570c6346300074a9fd'
assert report['confirmed_episode_rows_sha256']=='2f8de82daffe0269d0ffd58a5c6df487562d9dfe7a8b6ecb1e08910b15ffd6df'
shutil.copyfile('REVIEWED_SUBTITLES.md',output/'README_runtime.md')
archive=output/ASSET
with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
    for path in (database,output/'README_runtime.md',database.with_suffix('.merge.json'),output/'import_compatibility.json'):
        z.write(path,arcname=path.name)
assert archive.stat().st_size<128*1024*1024
new_digest=digest(archive)

# Keep the prior canonical asset as a separate, verified restore point.
backup_name='anime_four_sources_multilingual_prepared_before_review_20261003.zip'
backup=previous/backup_name;shutil.copyfile(previous/ASSET,backup)
matching=[a for a in metadata()['assets'] if a['name']==backup_name]
if matching:
    assert len(matching)==1 and matching[0]['digest']=='sha256:'+EXPECTED
else:
    gh('release','upload','db-v1',str(backup),'--repo',REPO)
assert asset(metadata(),backup_name)['digest']=='sha256:'+EXPECTED
assert asset(metadata(),ASSET)['digest']=='sha256:'+EXPECTED,'Catalog changed before publish'
gh('release','upload','db-v1',str(archive),'--repo',REPO,'--clobber')
after=metadata();published=asset(after,ASSET)
assert published['digest']=='sha256:'+new_digest
assert published['size']==archive.stat().st_size
readback=ROOT/'readback';readback.mkdir()
gh('release','download','db-v1','--repo',REPO,'--pattern',ASSET,'--dir',str(readback))
assert digest(readback/ASSET)==new_digest
final={**report,'release_url':after['html_url'],'published_asset_id':published['id'],
 'published_zip_sha256':new_digest,'published_zip_bytes':archive.stat().st_size,
 'backup_asset_id':asset(after,backup_name)['id'],'release_readback_verified':True,
 'streaming_commit':STREAM_COMMIT,'postgresql_applied':False,'pi_deployed':False}
validation=output/'reviewed_subtitle_release_validation.json'
validation.write_text(json.dumps(final,ensure_ascii=False,indent=2),encoding='utf-8')
gh('release','upload','db-v1',str(validation),'--repo',REPO,'--clobber')
deliver=pathlib.Path('deliverables');deliver.mkdir()
shutil.copyfile(validation,deliver/validation.name)
shutil.copyfile(output/'import_compatibility.json',deliver/'import_compatibility.json')
with archive.open('rb') as f:
    index=0
    while part:=f.read(16*1024*1024):
        (deliver/f'part_{index:02}').mkdir()
        (deliver/f'part_{index:02}'/f'release.zip.part{index:02}').write_bytes(part)
        index+=1
assert index<=3
print(json.dumps(final,ensure_ascii=False,indent=2))
