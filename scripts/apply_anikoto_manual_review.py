#!/usr/bin/env python3
"""Overlay the completed 4,427-row Anikoto manual review onto a matched runtime DB.

First publication reconstructs the review from the current matcher review queue plus
one compact decision vector. Once published, later monthly runs reuse the manual
state and multi-target rows already carried by the release DB, so the historical
queue ordering is never needed again.
"""
import argparse
import base64
import hashlib
import json
import lzma
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

EXPECTED_DECISION_SHA256 = '3e6eaa2cc785bf0fafc6a65533dc3a2362d4870ee86be5a99fbc67da641ded61'
EXPECTED_INITIAL_ORDER_SHA256 = '1ae21dbe3389ce973bb63ee33c44cb98ca01e29cdcd732c26ee72d27008cee4d'
SOURCE_CODES = {'A':'AniList','R':'ReAnime','O':'Onnada','N':'Anissia'}
KIND_CODES = {'r':'range','w':'whole','u':'unknown_order','s':'shared_source_episode'}
RUNTIME_CATALOG_FIELDS = (
    'source_record_id','title','title_native','title_romaji','format','genres',
    'poster_url','sub_episodes','dub_episodes','total_episodes'
)


def compact_catalog(listing, source_record_id, year=None, korean_title=''):
    result={name:listing[name] for name in RUNTIME_CATALOG_FIELDS if listing.get(name) not in (None,'',[])}
    result['source_record_id']=source_record_id
    if year not in (None,''): result['year']=year
    if korean_title: result['korean_title']=korean_title
    return result


def parse_target(value):
    source, sep, identity = str(value).partition(':')
    if not sep or not source or not identity:
        raise ValueError('Invalid target: '+str(value))
    return source,identity


def read_compact_bytes(path):
    path=Path(path)
    if path.exists():
        encoded=path.read_text(encoding='ascii').strip()
    else:
        parts=sorted(path.parent.glob(path.name+'.part*'))
        if not parts:
            raise ValueError('Missing compact manual-review decision asset: '+str(path))
        encoded=''.join(part.read_text(encoding='ascii').strip() for part in parts)
    packed=base64.b64decode(encoded,validate=True)
    digest=hashlib.sha256(packed).hexdigest()
    if digest!=EXPECTED_DECISION_SHA256:
        raise ValueError('Manual-review decision digest mismatch: '+digest)
    return packed


def records_from_initial_queue(review_queue, decision_asset):
    queue=[json.loads(line) for line in Path(review_queue).read_text(encoding='utf-8').splitlines() if line.strip()]
    if len(queue)!=4427:
        raise ValueError(f'Initial manual-review queue must contain 4,427 rows, found {len(queue)}')
    order_hash=hashlib.sha256(('\n'.join(row['anikoto_id'] for row in queue)).encode()).hexdigest()
    if order_hash!=EXPECTED_INITIAL_ORDER_SHA256:
        raise ValueError('Initial manual-review queue ordering changed; refusing positional decisions')
    vector=json.loads(lzma.decompress(read_compact_bytes(decision_asset)).decode('utf-8'))
    if len(vector)!=len(queue):
        raise ValueError('Manual-review decision vector length mismatch')

    def resolve(identity, candidates):
        if isinstance(identity,int):
            if identity<0 or identity>=len(candidates):
                raise ValueError('Candidate index out of range')
            return candidates[identity]['target']
        if isinstance(identity,str) and len(identity)>=2 and identity[0] in SOURCE_CODES:
            return SOURCE_CODES[identity[0]]+':'+identity[1:]
        raise ValueError('Invalid compact target identity: '+repr(identity))

    records=[]
    for order,(row,decision) in enumerate(zip(queue,vector),1):
        candidates=row.get('candidates') or []
        slug=row['anikoto_id']
        if decision=='H':
            records.append({'order':order,'anikoto_id':slug,'status':'hold','targets':[]})
        elif isinstance(decision,(int,str)):
            target=resolve(decision,candidates)
            records.append({'order':order,'anikoto_id':slug,'status':'single','targets':[{
                'target':target,'target_order':1,'mapping_kind':'whole',
                'anikoto_episode_start':None,'anikoto_episode_end':None,
                'target_episode_start':None,'target_episode_end':None}]})
        elif isinstance(decision,list) and len(decision)==2 and decision[0]=='M':
            targets=[]
            for target_order,item in enumerate(decision[1],1):
                if len(item)!=6: raise ValueError('Invalid compact multi target')
                targets.append({'target':resolve(item[0],candidates),'target_order':target_order,
                    'mapping_kind':KIND_CODES.get(item[1],item[1]),
                    'anikoto_episode_start':item[2],'anikoto_episode_end':item[3],
                    'target_episode_start':item[4],'target_episode_end':item[5]})
            records.append({'order':order,'anikoto_id':slug,'status':'multi','targets':targets})
        else:
            raise ValueError('Invalid compact manual-review decision at order '+str(order))
    return records


def records_from_runtime(db):
    has_state=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='anikoto_review_state'").fetchone()
    has_targets=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='anikoto_work_link_targets'").fetchone()
    if not (has_state and has_targets): return None
    states=db.execute('SELECT anikoto_id,review_order,status FROM anikoto_review_state ORDER BY review_order').fetchall()
    if len(states)!=4427: return None
    grouped=defaultdict(list)
    for row in db.execute("""SELECT anikoto_id,target_order,target_source,target_id,mapping_kind,
          anikoto_episode_start,anikoto_episode_end,target_episode_start,target_episode_end
          FROM anikoto_work_link_targets WHERE link_status IN ('reviewed','multi_reviewed')
          ORDER BY anikoto_id,target_order"""):
        grouped[row['anikoto_id']].append({'target':row['target_source']+':'+row['target_id'],
            'target_order':row['target_order'],'mapping_kind':row['mapping_kind'],
            'anikoto_episode_start':row['anikoto_episode_start'],'anikoto_episode_end':row['anikoto_episode_end'],
            'target_episode_start':row['target_episode_start'],'target_episode_end':row['target_episode_end']})
    return [{'order':row['review_order'],'anikoto_id':row['anikoto_id'],'status':row['status'],
             'targets':grouped.get(row['anikoto_id'],[])} for row in states]


def validate_records(records, expected_counts=None):
    if len(records)!=4427: raise ValueError('Expected 4,427 manual-review rows')
    if [r['order'] for r in records]!=list(range(1,4428)):
        raise ValueError('Manual review must preserve order 1..4427')
    if len({r['anikoto_id'] for r in records})!=4427:
        raise ValueError('Manual review contains duplicate Anikoto IDs')
    counts=Counter(r['status'] for r in records)
    if counts!=Counter(expected_counts or {'single':4245,'multi':91,'hold':91}):
        raise ValueError('Unexpected final review counts: '+str(dict(counts)))
    for rec in records:
        n=len(rec.get('targets') or [])
        if rec['status']=='single' and n!=1: raise ValueError('Single review must have one target: '+rec['anikoto_id'])
        if rec['status']=='multi' and n<2: raise ValueError('Multi review must have 2+ targets: '+rec['anikoto_id'])
        if rec['status']=='hold' and n: raise ValueError('Hold review must stay unlinked: '+rec['anikoto_id'])
    return counts


def apply(database, catalog, decision_asset, review_queue, report):
    database,catalog,report=map(Path,(database,catalog,report))
    with sqlite3.connect(catalog.resolve().as_uri()+'?mode=ro',uri=True) as cat:
        listings={slug:json.loads(value) for slug,value in cat.execute('SELECT source_record_id,payload_json FROM anikoto_catalog')}
    if len(listings)<8949:
        raise ValueError('Expected a complete Anikoto catalog containing the reviewed snapshot')
    report_data=json.loads(report.read_text(encoding='utf-8'))

    with sqlite3.connect(database) as db:
        db.row_factory=sqlite3.Row
        if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':
            raise ValueError('Runtime database failed quick_check before manual review')
        records=records_from_runtime(db)
        source='runtime_state'
        if records is None:
            records=records_from_initial_queue(review_queue,decision_asset)
            source='initial_decision_vector'
        audited = None
        if db.execute("SELECT 1 FROM metadata WHERE key='multimatch_review_20261021'").fetchone():
            audited=json.loads(db.execute("SELECT value FROM metadata WHERE key='multimatch_review_20261021'").fetchone()[0])
            if audited.get('batch')!='multimatch-20261021' or audited.get('manual_review_counts')!={'single':4246,'multi':90,'hold':91}:
                raise ValueError('Unexpected versioned multimatch review state')
        counts=validate_records(records, audited['manual_review_counts'] if audited else None)
        expected_multi_rows=audited['multi_target_rows'] if audited else 253
        missing=[r['anikoto_id'] for r in records if r['anikoto_id'] not in listings]
        if missing: raise ValueError('Manual-review ID missing from catalog: '+missing[0])
        for rec in records:
            for target in rec['targets']:
                ts,tid=parse_target(target['target'])
                if not db.execute('SELECT 1 FROM entries WHERE source=? AND source_record_id=?',(ts,tid)).fetchone():
                    raise ValueError('Manual target missing from entries: '+target['target'])

        db.execute('CREATE TEMP TABLE manual_review_ids(anikoto_id TEXT PRIMARY KEY)')
        db.executemany('INSERT INTO manual_review_ids VALUES (?)',[(r['anikoto_id'],) for r in records])
        extra_reviewed=[dict(r) for r in db.execute("SELECT * FROM anikoto_work_link_targets WHERE link_status IN ('reviewed','multi_reviewed') AND anikoto_id NOT IN (SELECT anikoto_id FROM manual_review_ids) ORDER BY anikoto_id,target_order")]
        db.execute("DELETE FROM anikoto_work_links WHERE anikoto_id IN (SELECT anikoto_id FROM manual_review_ids)")

        db.executescript('''
        DROP TABLE IF EXISTS anikoto_work_link_targets;
        DROP TABLE IF EXISTS anikoto_review_state;
        CREATE TABLE anikoto_work_link_targets (
          anikoto_id TEXT NOT NULL,
          target_order INTEGER NOT NULL,
          target_source TEXT NOT NULL,
          target_id TEXT NOT NULL,
          link_status TEXT NOT NULL,
          mapping_kind TEXT NOT NULL,
          anikoto_episode_start INTEGER,
          anikoto_episode_end INTEGER,
          target_episode_start INTEGER,
          target_episode_end INTEGER,
          payload_json TEXT NOT NULL,
          PRIMARY KEY(anikoto_id,target_order)
        );
        CREATE INDEX idx_anikoto_target_work ON anikoto_work_link_targets(target_source,target_id);
        CREATE TABLE anikoto_review_state (
          anikoto_id TEXT PRIMARY KEY,
          review_order INTEGER NOT NULL UNIQUE,
          status TEXT NOT NULL,
          payload_json TEXT NOT NULL
        );
        ''')
        for row in db.execute("SELECT anikoto_id,target_source,target_id,payload_json FROM anikoto_work_links WHERE status='automatic'").fetchall():
            db.execute('INSERT INTO anikoto_work_link_targets VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (row['anikoto_id'],1,row['target_source'],row['target_id'],'automatic','whole',None,None,None,None,row['payload_json']))

        extra_ids={r['anikoto_id'] for r in extra_reviewed}
        for slug in extra_ids:
            db.execute('DELETE FROM anikoto_work_link_targets WHERE anikoto_id=?',(slug,))
            if not db.execute('SELECT 1 FROM anikoto_catalog WHERE source_record_id=?',(slug,)).fetchone():
                if slug not in listings: raise ValueError('Additional reviewed source absent from saved catalog: '+slug)
                value=compact_catalog(listings[slug],slug)
                db.execute('INSERT INTO anikoto_catalog VALUES(?,?)',(slug,json.dumps(value,ensure_ascii=False,separators=(',',':'))))
        for row in extra_reviewed:
            db.execute('INSERT INTO anikoto_work_link_targets VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))
        for rec in records:
            db.execute('INSERT INTO anikoto_review_state VALUES (?,?,?,?)',
                       (rec['anikoto_id'],rec['order'],rec['status'],'{}'))
            if rec['status']=='hold': continue
            listing=listings[rec['anikoto_id']]
            targets=rec['targets']
            first_source,first_id=parse_target(targets[0]['target'])
            first_entry=db.execute('SELECT year,title_ko FROM entries WHERE source=? AND source_record_id=?',(first_source,first_id)).fetchone()
            year=first_entry['year'] if first_entry else None
            korean=first_entry['title_ko'] if first_entry and first_entry['title_ko'] else ''
            catalog_payload=compact_catalog(listing,rec['anikoto_id'],year,korean if rec['status']=='single' else '')
            db.execute('INSERT OR REPLACE INTO anikoto_catalog VALUES (?,?)',
                       (rec['anikoto_id'],json.dumps(catalog_payload,ensure_ascii=False,separators=(',',':'))))
            if rec['status']=='single':
                target=targets[0]; ts,tid=parse_target(target['target'])
                payload={'manual_review':True,'review_order':rec['order'],'source_refs':[{'source':ts,'id':tid}]}
                db.execute('INSERT INTO anikoto_work_links VALUES (?,?,?,?,?)',
                           (rec['anikoto_id'],ts,tid,'reviewed',json.dumps(payload,ensure_ascii=False,separators=(',',':'))))
            for target in targets:
                ts,tid=parse_target(target['target'])
                payload={'manual_review':True,'review_order':rec['order']}
                db.execute('INSERT INTO anikoto_work_link_targets VALUES (?,?,?,?,?,?,?,?,?,?,?)',(
                    rec['anikoto_id'],target['target_order'],ts,tid,
                    'reviewed' if rec['status']=='single' else 'multi_reviewed',target.get('mapping_kind') or 'whole',
                    target.get('anikoto_episode_start'),target.get('anikoto_episode_end'),
                    target.get('target_episode_start'),target.get('target_episode_end'),
                    json.dumps(payload,separators=(',',':'))))

        db.execute('''DELETE FROM anikoto_catalog WHERE source_record_id NOT IN
                   (SELECT DISTINCT anikoto_id FROM anikoto_work_link_targets)''')
        extra_nonlegacy=db.execute("SELECT count(DISTINCT t.anikoto_id) FROM anikoto_work_link_targets t WHERE t.anikoto_id NOT IN (SELECT anikoto_id FROM manual_review_ids) AND t.anikoto_id NOT IN (SELECT anikoto_id FROM anikoto_work_links WHERE status IN ('automatic','reviewed'))").fetchone()[0]
        auto_count=db.execute("SELECT count(*) FROM anikoto_work_links WHERE status='automatic'").fetchone()[0]
        reviewed_count=db.execute("SELECT count(*) FROM anikoto_work_links WHERE status='reviewed'").fetchone()[0]
        runtime_works=db.execute('SELECT count(*) FROM anikoto_catalog').fetchone()[0]
        target_rows=db.execute('SELECT count(*) FROM anikoto_work_link_targets').fetchone()[0]
        multi_rows=db.execute("SELECT count(*) FROM anikoto_work_link_targets WHERE link_status='multi_reviewed'").fetchone()[0]
        if report_data.get('records')==8949:
            needs_review=unmatched=0
        else:
            unresolved=max(0,int(report_data.get('records') or 0)-auto_count-reviewed_count-counts['multi']-counts['hold']-int(report_data.get('rejected') or 0))
            needs_review,unmatched=unresolved,0
        report_data.update({
          'automatic':auto_count,'reviewed':reviewed_count,'multi_reviewed':counts['multi'],'manual_hold':counts['hold'],
          'needs_review':needs_review,'unmatched':unmatched,
          'manual_review_format_version':1,'manual_review_records':4427,'manual_review_source':source,
          'runtime_works':runtime_works,'legacy_single_links':auto_count+reviewed_count,
          'multi_target_source_records':counts['multi'],'work_link_target_rows':target_rows,'multi_target_rows':multi_rows,
          'manual_review_decision_sha256':EXPECTED_DECISION_SHA256,
        })
        db.execute('INSERT OR REPLACE INTO anikoto_match_metadata VALUES (?,?)',
                   ('latest_match_report',json.dumps(report_data,ensure_ascii=False)))
        db.execute('INSERT OR REPLACE INTO anikoto_match_metadata VALUES (?,?)',('manual_review_format_version','1'))
        db.execute('INSERT OR REPLACE INTO anikoto_match_metadata VALUES (?,?)',
                   ('manual_review_summary',json.dumps({**dict(counts),'records':4427,
                    'decision_sha256':EXPECTED_DECISION_SHA256},separators=(',',':'))))
        db.commit()
        if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':
            raise ValueError('Runtime database failed quick_check after manual review')
        assert reviewed_count==counts['single']
        assert runtime_works==auto_count+counts['single']+counts['multi']+extra_nonlegacy
        assert db.execute('SELECT count(*) FROM anikoto_review_state').fetchone()[0]==4427
        assert db.execute("SELECT count(*) FROM anikoto_review_state WHERE status='multi'").fetchone()[0]==counts['multi']
        assert db.execute("SELECT count(*) FROM anikoto_review_state WHERE status='hold'").fetchone()[0]==counts['hold']
        assert multi_rows==expected_multi_rows
        assert not db.execute('''SELECT t.anikoto_id FROM anikoto_work_link_targets t LEFT JOIN entries e
              ON e.source=t.target_source AND e.source_record_id=t.target_id WHERE e.source IS NULL LIMIT 1''').fetchall()
        assert not db.execute('''SELECT source_record_id FROM anikoto_catalog EXCEPT
              SELECT DISTINCT anikoto_id FROM anikoto_work_link_targets''').fetchall()
        assert not db.execute('''SELECT DISTINCT anikoto_id FROM anikoto_work_link_targets EXCEPT
              SELECT source_record_id FROM anikoto_catalog''').fetchall()
    report.write_text(json.dumps(report_data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return report_data


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--database',type=Path,required=True)
    p.add_argument('--catalog',type=Path,required=True)
    p.add_argument('--decision-asset',type=Path,required=True)
    p.add_argument('--review-queue',type=Path,required=True)
    p.add_argument('--report',type=Path,required=True)
    a=p.parse_args()
    print(json.dumps(apply(a.database,a.catalog,a.decision_asset,a.review_queue,a.report),ensure_ascii=False,indent=2))
if __name__=='__main__': main()
