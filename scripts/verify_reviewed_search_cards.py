#!/usr/bin/env python3
"""Read-only comparison of published release projection before and after this fix."""
import argparse,importlib.util,json
from pathlib import Path
import export_cloudflare_search_sql as after

def run(catalog,registry,baseline):
    spec=importlib.util.spec_from_file_location('baseline_export',baseline)
    before=importlib.util.module_from_spec(spec);spec.loader.exec_module(before)
    cat=after.read_only(catalog);reg=after.read_only(registry)
    old_works,old_aliases,_,_=before.build(cat,reg)
    works,aliases,_,_=after.build(cat,reg)
    entries={(r['source'],r['source_record_id']):dict(r) for r in cat.execute('SELECT * FROM entries')}
    by_old_id={before.identity(*key)[0]:key for key in entries}
    targets=after.canonical_search_targets(entries,reg);new_aliases=set(aliases)
    for old_id,alias in old_aliases:
        key=by_old_id[old_id];dest=targets.get(key,key)
        assert (after.identity(*dest)[0],alias) in new_aliases,('Alias lost',old_id,alias)
    assert sum(w[4] for w in works)==sum(w[4] for w in old_works),'Subtitle count changed'
    by_id={w[0]:w for w in works}
    arrest={wid for wid,a in aliases if after.normalized_title('체포하겠어') in a}
    assert arrest=={
        're_you-re-under-arrest-tv-m29pp3','re_you-re-under-arrest-wb62ra',
        're_you-re-under-arrest-fast-furious-ytbsjm','re_you-re-under-arrest-full-throttle-5eh9pp',
        're_taiho-shichauzo-full-throttle-special-2thjzp'},arrest
    for wid in arrest:
        assert any('가'<=ch<='힣' for ch in by_id[wid][1]),by_id[wid]
        assert by_id[wid][5]=='/anime/'+wid,by_id[wid]
    tsugu={wid for wid,a in aliases if after.normalized_title('츠구모모') in a}
    assert tsugu=={'ani_988','ani_1713','ani_1671'},tsugu
    assert by_id['ani_988'][3]=='AniAll · 네이버 · 애니시아',by_id['ani_988']
    assert by_id['ani_1713'][3]=='네이버 · 애니시아',by_id['ani_1713']
    print(json.dumps({'before_cards':len(old_works),'after_cards':len(works),
        'total_captions_preserved':sum(w[4] for w in works),'all_prior_alias_associations_preserved':True,
        'arrest_cards':[by_id[x][:6] for x in sorted(arrest)],
        'tsugumomo_cards':[by_id[x][:6] for x in sorted(tsugu)]},ensure_ascii=False,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--catalog',type=Path,required=True)
    p.add_argument('--registry',type=Path,required=True);p.add_argument('--baseline',type=Path,required=True)
    a=p.parse_args();run(a.catalog,a.registry,a.baseline)
