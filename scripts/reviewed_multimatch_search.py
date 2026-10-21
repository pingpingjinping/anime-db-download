"""Expose each reviewed work separately, without joining a shared video container."""
import json
from collections import defaultdict


def extend_projection(db, works, aliases, normalize):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='multimatch_search_works_20261021' AND type='table'").fetchone():
        return works, aliases
    records=[dict(r) for r in db.execute('SELECT * FROM multimatch_search_works_20261021 ORDER BY work_id')]
    existing={r[0]:r for r in works};aliases=set(aliases)
    owners=defaultdict(set)
    for r in records:
        for member in json.loads(r['member_ids_json']):owners[member].add(r['work_id'])
    selected={}
    for r in records:
        candidates=[m for m in json.loads(r['member_ids_json']) if m in existing and len(owners[m])==1]
        selected[r['work_id']]=min(candidates,key=lambda x:(0 if x.startswith('re_') else 1 if x.startswith('ani_') else 2,x)) if candidates else r['work_id']
    assigned=defaultdict(set)
    for wid,target in selected.items():assigned[target].add(wid)
    for r in records:
        wid=r['work_id'];target=selected[wid]
        if len(assigned[target])>1:target=wid
        title=r['title_ko'];previous=existing.get(target)
        if previous:
            existing[target]=(previous[0],title,*previous[2:]);old=normalize(previous[1])
            if old:aliases.add((target,old))
        else:
            if target.startswith('db_AniList_'):source,rid='AniList',target[11:];url='/catalog/AniList/'+rid
            elif target.startswith('re_'):source,rid='ReAnime',target[3:];url='/anime/'+target
            elif target.startswith('ak_'):source,rid='Anikoto',target[3:];url='/anime/'+target
            else:raise ValueError('Unexpected reviewed search ID: '+target)
            year=None
            if source!='Anikoto':
                row=db.execute('SELECT year FROM entries WHERE source=? AND source_record_id=?',(source,rid)).fetchone()
                if not row:raise ValueError('Reviewed search record absent: '+target)
                year=row[0]
            else:
                if not db.execute('SELECT 1 FROM anikoto_catalog WHERE source_record_id=?',(rid,)).fetchone():raise ValueError('Reviewed Anikoto record absent')
            suffix=' (가칭)' if r['confidence']=='낮음' else ''
            existing[target]=(target,title,year,source+suffix,0,url,'')
        for name in [title,*json.loads(r['aliases_json'])]:
            value=normalize(name)
            if value and len(value)<=300:aliases.add((target,value))
    return [existing[k] for k in sorted(existing)],sorted(aliases)
