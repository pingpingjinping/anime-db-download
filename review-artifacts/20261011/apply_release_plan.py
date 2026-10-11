"""Apply exact reviewed row updates to copied Release assets, or verify rollback."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
# Also support execution alongside a checkout during local verification.
sys.path.insert(0,'/workspace/anime-db-download/scripts')
from split_db_v1_release import extract_database, pack_database, table_digest, validate_catalog_db, validate_registry_db

ASSETS={'catalog':'anime_four_sources_multilingual_prepared.zip','registry':'anime_reviewed_subtitle_registry_v1.zip'}
def quoted(name):return '"'+name.replace('"','""')+'"'
def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1048576),b''):h.update(chunk)
    return h.hexdigest()

def check_tables(db,part,stage):
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    assert tables==set(part['table_digests']), (stage,'table set changed')
    for name,expected in part['table_digests'].items():
        assert table_digest(db,name)==expected[stage],(name,stage,'row digest mismatch')
    assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
    assert not db.execute('PRAGMA foreign_key_check').fetchall()

def apply_database(source,destination,part,reverse=False):
    if not reverse:assert digest(source)==part['baseline_db_sha256'],'SQLite baseline changed'
    shutil.copy2(source,destination)
    db=sqlite3.connect(destination)
    db.row_factory=sqlite3.Row
    start,finish=('after','before') if reverse else ('before','after')
    try:
        check_tables(db,part,start)
        before_schema=[tuple(r) for r in db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')]
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN')
        db.execute('PRAGMA defer_foreign_keys=ON')
        for name,table in part['tables'].items():
            real_columns={r['name'] for r in db.execute('PRAGMA table_info('+quoted(name)+')')}
            primary=[r['name'] for r in sorted(db.execute('PRAGMA table_info('+quoted(name)+')'),key=lambda r:r['pk']) if r['pk']]
            assert primary==table['primary_key']
            for change in table['changes']:
                keys=change['key'];old=change[start];new=change[finish]
                assert set(keys)==set(primary)
                where=' AND '.join(quoted(key)+'=?' for key in keys)
                current=db.execute('SELECT * FROM '+quoted(name)+' WHERE '+where,tuple(keys.values())).fetchone()
                assert (dict(current) if current else None)==old,(name,keys,'old row changed')
                if new is None:
                    assert db.execute('DELETE FROM '+quoted(name)+' WHERE '+where,tuple(keys.values())).rowcount==1
                elif old is None:
                    assert set(new)==real_columns
                    db.execute('INSERT INTO '+quoted(name)+' ('+','.join(quoted(k) for k in new)+') VALUES ('+','.join('?' for _ in new)+')',tuple(new.values()))
                else:
                    assert set(new)==real_columns and all(old[k]==new[k] for k in primary)
                    updated={k:v for k,v in new.items() if old[k]!=v}
                    assignments=','.join(quoted(k)+'=?' for k in updated)
                    assert db.execute('UPDATE '+quoted(name)+' SET '+assignments+' WHERE '+where,(*updated.values(),*keys.values())).rowcount==1
        check_tables(db,part,finish)
        assert before_schema==[tuple(r) for r in db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')]
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:db.close()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    plan=json.loads(args.plan.read_text());assert plan['version']==1 and plan['new_episode_assignments']==0
    args.output.mkdir(parents=True,exist_ok=True)
    manifest={'review_batch':plan['review_batch'],'schema_unchanged':True,'exact_row_rollback_verified':True,'new_episode_assignments':0,'assets':{}}
    for role,filename in ASSETS.items():
        source=args.output/(role+'-baseline.db');target=args.output/Path(filename).with_suffix('.db').name
        extract_database(args.input/filename,source)
        apply_database(source,target,plan['databases'][role])
        rollback=args.output/(role+'-rollback-verification.db')
        apply_database(target,rollback,plan['databases'][role],reverse=True)
        validated=(validate_catalog_db if role=='catalog' else validate_registry_db)(target)
        pack_database(target,args.output/filename,target.name)
        manifest['assets'][filename]={'before_sha256':digest(args.input/filename),'after_sha256':digest(args.output/filename),'counts':validated}
        source.unlink();rollback.unlink();target.unlink()
    (args.output/'matching-release-validation.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(manifest,ensure_ascii=False))

if __name__=='__main__':main()
