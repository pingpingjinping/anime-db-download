import json,sqlite3,sys
from pathlib import Path
from split_db_v1_release import extract_database
root=Path(sys.argv[1])
files=json.loads(Path('data/matching/body-audit-20261011.json').read_text())['files']
for role,asset in [('catalog','anime_four_sources_multilingual_prepared.zip'),('registry','anime_reviewed_subtitle_registry_v1.zip')]:
 p=root/(role+'.sqlite');extract_database(root/asset,p)
 c=sqlite3.connect(p);c.row_factory=sqlite3.Row
 schemas={r['name']:r['sql'] for r in c.execute("select name,sql from sqlite_master where type='table' and name not like 'sqlite_%'")}
 print('SCHEMAS',role,json.dumps(schemas,ensure_ascii=False))
 print('COUNTS',role,json.dumps({t:c.execute('select count(*) from "'+t+'"').fetchone()[0] for t in schemas}))
 for table in ['packaged_files_v6','subtitle_review_details','subtitle_file_origins','subsubsub_file_registry_v1','subsubsub_file_targets_v1']:
  if table in schemas:
   for f in files:
    for r in c.execute('select * from '+table+' where pid=?',(f['pid'],)):
     print('AUDIT_ROW',role,table,json.dumps(dict(r),ensure_ascii=False))
 if 'subtitle_runtime_metadata' in schemas:print('METADATA',role,json.dumps([dict(r) for r in c.execute('select * from subtitle_runtime_metadata')],ensure_ascii=False))
 c.close()
