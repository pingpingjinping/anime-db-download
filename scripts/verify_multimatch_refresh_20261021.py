"""Verify that the monthly manual-review replay preserves the new decisions."""
import argparse,sqlite3,shutil,json
from pathlib import Path
from apply_anikoto_manual_review import apply,records_from_runtime,validate_records
import reviewed_matching_guard as guard
def run(catalog,output,rules):
    out=Path(output);out.mkdir(exist_ok=True);original=sqlite3.connect(Path(catalog).as_uri()+'?mode=ro',uri=True);original.row_factory=sqlite3.Row
    replay=out/'refresh-replay.db';shutil.copyfile(catalog,replay)
    listings=out/'refresh-listings.db'
    if listings.exists():listings.unlink()
    c=sqlite3.connect(listings);c.execute('CREATE TABLE anikoto_catalog(source_record_id TEXT PRIMARY KEY,payload_json TEXT)')
    c.executemany('INSERT INTO anikoto_catalog VALUES(?,?)',original.execute('SELECT * FROM anikoto_catalog'))
    records=records_from_runtime(original);assert dict(validate_records(records,{'single':4246,'multi':90,'hold':91}))=={'single':4246,'multi':90,'hold':91}
    for r in records:c.execute('INSERT OR IGNORE INTO anikoto_catalog VALUES(?,?)',(r['anikoto_id'],json.dumps({'source_record_id':r['anikoto_id'],'title':'Replay fixture'})))
    for n in range(8949-c.execute('SELECT count(*) FROM anikoto_catalog').fetchone()[0]):c.execute('INSERT INTO anikoto_catalog VALUES(?,?)',('unused-replay-fixture-'+str(n),'{}'))
    c.commit();c.close()
    report=out/'refresh-replay-report.json';report.write_text('{"records":8949}',encoding='utf-8')
    result=apply(replay,listings,'unused','unused',report)
    new=sqlite3.connect(replay);new.row_factory=sqlite3.Row
    for table in ['anikoto_work_link_targets','anikoto_work_links','anikoto_review_state']:
        cols=[r[1] for r in new.execute('PRAGMA table_info('+table+')') if r[1]!='payload_json'];query='SELECT '+','.join(cols)+' FROM '+table+' ORDER BY '+','.join(cols)
        assert [tuple(r) for r in original.execute(query)]==[tuple(r) for r in new.execute(query)],table
    checked=guard.verify_role(new,'catalog',guard.read_rules(rules));assert checked>=2151
    new.close();original.close();print(json.dumps({'monthly_manual_replay_preserves_all_targets_and_ranges':True,'guard_rows':checked,'counts':{k:result[k] for k in ['reviewed','multi_reviewed','manual_hold','multi_target_rows']}},ensure_ascii=False))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--catalog',required=True);p.add_argument('--output',required=True);p.add_argument('--rules',required=True);a=p.parse_args();run(str(Path(a.catalog).resolve()),a.output,a.rules)
