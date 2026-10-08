#!/usr/bin/env python3
"""Read-only V4 replay for unlinked Onnada entries and reviewed subtitle holds.

The deployed matcher is Anissia-specific. This adapter supplies temporary
Anissia-shaped identities in memory only; it never updates catalog/registry rows.
Rule-eligible results are proposals requiring review, not confirmed corrections.
"""
import argparse
import csv
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from audit_new_source_matches import replay
from work_identity import medium

EXTENSIONS = {'.ass','.ssa','.srt','.smi','.vtt','.sub'}


def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk := stream.read(1024*1024): h.update(chunk)
    return h.hexdigest()


def onnada_inputs(db):
    rows = [dict(row) for row in db.execute("SELECT * FROM entries WHERE source='Onnada'")]
    anikoto = defaultdict(set)
    for row in db.execute('SELECT * FROM anikoto_work_link_targets'):
        anikoto[(row['target_source'],row['target_id'])].add(row['anikoto_id'])
        for ref in json.loads(row['payload_json']).get('source_refs',[]):
            if ref.get('source') == 'Onnada': anikoto[('Onnada',str(ref['id']))].add(row['anikoto_id'])
    pending, protected = [], []
    for row in rows:
        cross = json.loads(row['cross_source_ids_json'])
        refs = [('Onnada',row['source_record_id'])] + [('AniList',str(x)) for x in cross.get('anilist_ids',[])] + [('ReAnime',str(x)) for x in cross.get('reanime_ids',[])]
        ids = sorted({aid for ref in refs for aid in anikoto.get(ref,[])})
        item = {'source':'Onnada','id':row['source_record_id'],'title':row['title_ko'],
                'old_status':row['existing_match_status'],'existing_reanime_ids':cross.get('reanime_ids',[]),
                'existing_anikoto_ids':ids,'file_count':0,'row':row}
        (protected if item['existing_reanime_ids'] or ids else pending).append(item)
    return pending, protected


def subtitle_inputs(db):
    groups = defaultdict(list); protected = Counter(); total = Counter()
    query = '''SELECT p.*,d.work_title_ko,d.identified_work,d.season_format
      FROM packaged_files_v6 p LEFT JOIN subtitle_review_details d USING(pid) WHERE p.action='kept' '''
    for record in db.execute(query):
        row = dict(record)
        if not any(Path(str(row.get(f) or '')).suffix.lower() in EXTENSIONS for f in ('original_path','member_path')): continue
        total[row['source']] += 1
        if row['target_id']:
            protected[row['source']] += 1
            continue
        groups[(row['source'],row['bundle_id'],row['work_title_ko'],row['identified_work'],row['season_format'])].append(row)
    pending = []
    for (source,bundle,ko,identified,scope), files in sorted(groups.items(),key=lambda x:str(x[0])):
        years = set(re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)',scope or ''))
        formats = set(re.findall(r'\b(?:TV|MOVIE|OVA|ONA|SPECIAL|TVSP)\b',(scope or '').upper()))
        known_format = next(iter(formats)) if len(formats)==1 else ''
        row = {'year':int(next(iter(years))) if len(years)==1 else None,
          'title_ko':ko or '', 'title_native':identified if identified and re.search('[\u3040-\u9fff]',identified) else '',
          'title_romaji':'', 'title_english':identified if identified and not re.search('[\u3040-\u9fff]',identified) else '',
          'format':known_format,'source_metadata_json':'{}','cross_source_ids_json':'{}',
          'year_basis':'existing_review_metadata','source_year_raw':scope or ''}
        pending.append({'source':source,'id':bundle,'title':ko or identified or '',
          'old_status':' | '.join(sorted({f['review_status'] for f in files})),
          'existing_reanime_ids':[],'existing_anikoto_ids':[], 'file_count':len(files),
          'filenames':[f['original_path'] for f in files], 'file_ids':[f['pid'] for f in files],
          'review_scope':scope or '', 'row':row})
    return pending, dict(total), dict(protected)


def run_group(items, database, db):
    samples = []
    for index,item in enumerate(items,1):
        row = dict(item['row']);row.update(source='Anissia',source_record_id=str(index),existing_match_status='review')
        row['format'] = medium(row) or ''
        samples.append(row)
    result = replay({'samples':samples},database) if samples else {'samples':[],'summary':{}}
    targets = {row['source_record_id']:dict(row) for row in db.execute("SELECT * FROM entries WHERE source='ReAnime'")}
    anikoto = defaultdict(set)
    for row in db.execute('SELECT * FROM anikoto_work_link_targets'):
        anikoto[(row['target_source'],row['target_id'])].add(row['anikoto_id'])
    output = []
    for item, decision in zip(items,result['samples']):
        ids = [decision['reanime_id']] if decision['reanime_id'] else []
        candidates = decision['candidates']
        ids += [c['reanime_id'] for c in candidates if c['reanime_id']]
        ak = set()
        for rid in ids:
            ak.update(anikoto[('ReAnime',rid)])
            for aid in json.loads(targets[rid]['cross_source_ids_json']).get('anilist_ids',[]):ak.update(anikoto[('AniList',str(aid))])
        output.append({k:v for k,v in item.items() if k!='row'} | {
          'status':'unique_rule_candidate_review_required' if decision['reanime_id'] else 'hold',
          'proposed_reanime_id':decision['reanime_id'] or '', 'candidate_reanime_ids':sorted(set(ids)),
          'candidate_anikoto_ids':sorted(ak), 'candidate_titles':{rid:targets[rid].get('title_english') or targets[rid].get('title_native') for rid in ids},
          'year':item['row'].get('year'),'format':medium(item['row']), 'candidates':candidates,
          'proposed_match_evidence':decision.get('automatic_evidence'),
          'basis':'V4 new-ingestion replay; historical reservations disabled in memory only; no internet validation',
          'input_metadata_origin':'catalog' if item['source']=='Onnada' else 'existing manual-review hints; filenames not used to invent identities'})
    return output


def write_csv(path, rows):
    fields = ['source','id','title','old_status','status','proposed_reanime_id','candidate_reanime_ids','candidate_anikoto_ids','candidate_titles','year','format','file_count','file_ids','filenames','review_scope','candidates','proposed_match_evidence','basis','input_metadata_origin']
    with path.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore');writer.writeheader()
        for row in rows:writer.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else v for k,v in row.items()})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,required=True);parser.add_argument('--registry',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True);args=parser.parse_args()
    hashes={'catalog':digest(args.database),'registry':digest(args.registry)}
    db=readonly(args.database);registry=readonly(args.registry)
    on,protected=onnada_inputs(db);sub,totals,protected_files=subtitle_inputs(registry)
    outputs={}
    for name,items in [('Onnada',on),('aniall',[x for x in sub if x['source']=='aniall']),('naver',[x for x in sub if x['source']=='naver'])]:
        outputs[name]=run_group(items,args.database,db)
    db.close();registry.close()
    assert hashes=={'catalog':digest(args.database),'registry':digest(args.registry)},'Input hash changed'
    args.output_dir.mkdir(parents=True,exist_ok=True)
    for name,rows in outputs.items():write_csv(args.output_dir/(name+'_unlinked_candidates.csv'),rows)
    summary={'applied':False,'rule_version':4,'internet_review_completed':0,'input_hashes_unchanged':True,'input_sha256':hashes,
      'onnada_total':len(on)+len(protected),'onnada_existing_links_protected':len(protected),'onnada_unlinked':len(on),
      'subtitle_files_total':totals,'subtitle_existing_links_protected':protected_files,
      'unlinked_subtitle_files':{s:totals[s]-protected_files.get(s,0) for s in totals},
      'results':{k:dict(Counter(x['status'] for x in v)) for k,v in outputs.items()},
      'scope':'Only unlinked catalog entries and unlinked subtitle groups replayed. Existing links and episode rows preserved. No updater/Release/Pi writes. Not a sweep of protected match accuracy.'}
    (args.output_dir/'audit.json').write_text(json.dumps({'summary':summary,'results':outputs},ensure_ascii=False,indent=2))
    print(json.dumps(summary,ensure_ascii=False))


if __name__=='__main__': main()
