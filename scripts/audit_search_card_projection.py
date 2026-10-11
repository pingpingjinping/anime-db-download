#!/usr/bin/env python3
"""Audit every search card using reviewed IDs; source databases stay read-only."""
import argparse
import csv
import importlib.util
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

import export_cloudflare_search_sql as after


def has_korean(value):
    return any('가' <= c <= '힣' for c in value)


def audit(catalog, registry, baseline):
    spec = importlib.util.spec_from_file_location('baseline_export', baseline)
    before = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(before)
    with after.read_only(catalog) as cat, after.read_only(registry) as reg:
        old_works, old_aliases, _, _ = before.build(cat, reg)
        works, aliases, source_counts, _ = after.build(cat, reg)
        entries = {(r['source'], r['source_record_id']): dict(r)
                   for r in cat.execute('SELECT * FROM entries')}
        targets = after.canonical_search_targets(entries, reg)
        registry_rows = after.registry_rows(reg)
        eligible = defaultdict(set)
        if after.table_exists(reg, 'subtitle_work_reanime_links'):
            for source, rid, remote in reg.execute('''SELECT target_source,target_id,reanime_id
                    FROM subtitle_work_reanime_links WHERE automatic_eligible=1'''):
                eligible[(str(source), str(rid))].add(str(remote))
    old_keys = {before.identity(*key)[0]: key for key in entries}
    new_by_id = {row[0]: row for row in works}
    new_aliases = set(aliases)
    groups = defaultdict(list)
    details = []
    destinations = {}
    for old in old_works:
        key = old_keys[old[0]]
        dest = targets.get(key, key)
        wid = after.identity(*dest)[0]
        assert wid in new_by_id, ('Missing destination', old[0], wid)
        assert targets.get(dest, dest) == dest, ('Unstable canonical destination', dest)
        new = new_by_id[wid]
        assert new[5] == after.identity(*dest)[1], ('Route namespace differs', wid)
        assert set(old[3].split(' · ')) <= set(new[3].split(' · ')), ('Provider lost', old[0])
        if key != dest and key[0] != 'ReAnime':
            assert len(eligible[key]) == 1, ('Unreviewed or ambiguous merge', key)
        groups[wid].append(old)
        destinations[old[0]] = wid
        details.append({
            'old_id': old[0], 'new_id': wid, 'old_title': old[1], 'new_title': new[1],
            'old_url': old[5], 'new_url': new[5], 'old_providers': old[3],
            'new_providers': new[3], 'old_caption_count': old[4], 'new_caption_count': new[4],
            'korean_display_added': not has_korean(old[1]) and has_korean(new[1]),
            'catalog_route_replaced': old[5].startswith('/catalog/') and new[5].startswith('/anime/'),
        })
    for old_id, alias in old_aliases:
        assert (destinations[old_id], alias) in new_aliases, ('Alias lost', old_id, alias)
    # Verify every confirmed subtitle PID reaches exactly its intended destination.
    file_sets = defaultdict(set)
    for row in registry_rows:
        key = (row['target_source'], row['target_id'])
        if key not in entries:
            continue
        wid = after.identity(*targets.get(key, key))[0]
        assert wid in new_by_id, ('Confirmed subtitle target dropped', row['pid'], key)
        file_sets[wid].add(row['pid'])
    for wid, files in file_sets.items():
        meta_count = 0
        if wid.startswith('ani_'):
            meta_count = after.number(after.json_obj(entries[('Anissia', wid[4:])].get(
                'source_metadata_json')).get('captionCount'))
        assert new_by_id[wid][4] == len(files) + meta_count, ('Subtitle count differs', wid)
    # Distinct AniList IDs are separate works/seasons, even when names coincide.
    for wid, members in groups.items():
        ids = {old_keys[m[0]][1] for m in members if old_keys[m[0]][0] == 'AniList'}
        assert len(ids) <= 1, ('Multiple AniList works collapsed', wid, sorted(ids))
    assert sum(w[4] for w in old_works) == sum(w[4] for w in works), 'Total caption count changed'

    borrowed = after.reviewed_korean_search_aliases(entries)
    registrations = defaultdict(set)
    for key, ids in eligible.items():
        if key[0] == 'Anissia' and key in entries and len(ids) == 1:
            registrations[next(iter(ids))].add(after.identity(*key)[0])
    ambiguous_registration_ids = set()
    ambiguous_registration_groups = 0
    for rid, ids in registrations.items():
        emitted = (ids | {'re_' + rid}) & set(new_by_id)
        if len(ids) > 1 and len(emitted) > 1:
            ambiguous_registration_groups += 1
            ambiguous_registration_ids.update(emitted)
    review = []
    for row in works:
        wid = row[0]
        keys = {old_keys[m[0]] for m in groups[wid]}
        canonical = ('Anissia', wid[4:]) if wid.startswith('ani_') else (
            ('ReAnime', wid[3:]) if wid.startswith('re_') else old_keys[wid])
        names = set().union(*(borrowed.get(key, set()) for key in keys | {canonical}))
        reasons = []
        if wid in ambiguous_registration_ids:
            reasons.append('multiple_anissia_registrations_kept_separate')
        if row[5].startswith('/catalog/'):
            reasons.append('catalog_preserved_no_unique_eligible_video_link')
        if not has_korean(row[1]):
            if len({after.normalized_title(v) for v in names}) > 1:
                reasons.append('multiple_reviewed_korean_names')
            elif re.search(r'[\u3040-\u30ff\u3400-\u9fff]', row[1]):
                reasons.append('original_script_no_unambiguous_reviewed_korean_title')
            else:
                reasons.append('latin_or_numeric_title_not_automatically_an_error')
        if reasons:
            review.append({'id': wid, 'title': row[1], 'year': row[2], 'url': row[5],
                           'providers': row[3], 'caption_count': row[4],
                           'reason': ';'.join(reasons),
                           'reviewed_korean_names': ' | '.join(sorted(names))})
    summary = {
        'source_entries': source_counts, 'before_cards': len(old_works), 'after_cards': len(works),
        'redundant_cards_removed': len(old_works) - len(works),
        'merged_work_groups': sum(len(v) > 1 for v in groups.values()),
        'original_group_sizes': dict(sorted(Counter(len(v) for v in groups.values()).items())),
        'old_cards_gaining_korean_display': sum(r['korean_display_added'] for r in details),
        'old_catalog_routes_replaced': sum(r['catalog_route_replaced'] for r in details),
        'remaining_catalog_cards': sum(w[5].startswith('/catalog/') for w in works),
        'ambiguous_anissia_registration_groups_preserved': ambiguous_registration_groups,
        'remaining_titles_without_hangul': sum(not has_korean(w[1]) for w in works),
        'review_reasons': dict(Counter(reason for r in review for reason in r['reason'].split(';'))),
        'caption_count_before': sum(w[4] for w in old_works),
        'caption_count_after': sum(w[4] for w in works),
        'confirmed_distinct_subtitle_pids': len({r['pid'] for r in registry_rows}),
        'old_alias_associations': len(old_aliases), 'new_alias_associations': len(aliases),
        'all_prior_alias_associations_preserved_on_destination': True,
        'all_confirmed_subtitle_pids_preserved_on_destination': True,
        'provider_labels_preserved': True, 'canonical_routes_stable': True,
        'different_anilist_ids_never_collapsed': True,
    }
    return summary, details, review


def write_csv(path, rows):
    with path.open('w', encoding='utf-8-sig', newline='') as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0]) if rows else ['id'])
        writer.writeheader()
        writer.writerows(rows)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--catalog', type=Path, required=True)
    p.add_argument('--registry', type=Path, required=True)
    p.add_argument('--baseline', type=Path, required=True)
    p.add_argument('--output-dir', type=Path)
    a = p.parse_args()
    summary, details, review = audit(a.catalog, a.registry, a.baseline)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if a.output_dir:
        a.output_dir.mkdir(parents=True, exist_ok=True)
        (a.output_dir / 'search_card_audit_summary.json').write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        write_csv(a.output_dir / 'search_card_changes.csv', details)
        write_csv(a.output_dir / 'search_card_review.csv', review)
