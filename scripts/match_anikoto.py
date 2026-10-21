#!/usr/bin/env python3
"""Match saved Anikoto listings to a read-only reviewed catalog, keeping evidence."""
import argparse
import hashlib
import json
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from match_new_sources import season, part
from prepare_anikoto_catalog import DEFAULT_DB, ROOT, validate_catalog
from crawl_anikoto import catalog_rows

RULE_VERSION = 2
RUNTIME_VERSION = 2
FIELDS = ('title_ko', 'title_native', 'title_romaji', 'title_english', 'title', 'title_jp_raw')
RUNTIME_CATALOG_FIELDS = ('source_record_id', 'title', 'title_native', 'title_romaji',
                          'format', 'genres', 'poster_url', 'sub_episodes', 'dub_episodes', 'total_episodes')


def runtime_catalog(listing, link):
    """Only accepted works and fields used for display/filtering/playback enter the DB."""
    result = {name: listing[name] for name in RUNTIME_CATALOG_FIELDS if listing.get(name) not in (None, '', [])}
    result.update({'source_record_id':link['anikoto_id'], 'year':link.get('year'),
                   'korean_title':link.get('korean_title','')})
    return {name:value for name,value in result.items() if value not in (None,'',[])}


def key(title):
    value = unicodedata.normalize('NFKC', title or '').casefold().replace('×', 'x')
    # Stars, plus signs, digits and sequel words distinguish works and must survive.
    return ''.join(ch for ch in value if ch.isalnum() or ch in '☆★+∞!?♭♯')


def names(row):
    return sorted({str(row.get(field) or '').strip() for field in FIELDS if row.get(field)})


def keys(row):
    return {key(title) for title in names(row) if len(key(title)) >= 4}


def positive_int(value):
    return int(str(value)) if re.fullmatch(r'[1-9]\d*', str(value or '')) else None


def metadata(row):
    return json.loads(row.get('source_metadata_json') or '{}')


def medium(row):
    value = str(row.get('format') or '').upper().replace(' ', '_')
    if not value and row.get('source') == 'Onnada':
        value = {'TV 시리즈': 'TV', '극장판': 'MOVIE', 'OVA': 'OVA', 'ONA': 'ONA',
                 '스페셜': 'SPECIAL'}.get(metadata(row).get('onnada_category'), '')
    return value if value in {'TV', 'TV_SHORT', 'MOVIE', 'OVA', 'ONA', 'SPECIAL', 'TV_SPECIAL', 'MUSIC'} else None


def episodes(row):
    if row.get('source') == 'Anikoto':
        return positive_int(row.get('total_episodes'))
    meta = metadata(row)
    fields = ('onnada_episodes', 'episodes_total', 'episodes') if row.get('source') == 'Onnada' else ('episodes', 'episodes_total')
    return next((n for field in fields
                 if (n := positive_int(meta.get(field))) is not None), None)


def labels(row, parser):
    return {n for title in names(row) if (n := parser(title)) is not None}


def compatible(a, b, require_counts=False):
    reasons = []
    af, bf = medium(a), medium(b)
    if af and bf and af != bf:
        reasons.append('format_conflict')
    elif require_counts and (not af or not bf):
        reasons.append('format_missing')
    for name, parser in [('season', season), ('part', part)]:
        left, right = labels(a, parser), labels(b, parser)
        if len(left) > 1 or len(right) > 1:
            reasons.append(name + '_inconsistent')
        elif left != right and (left or right):
            if name != 'season' or (left | right) != {1}:
                reasons.append(name + '_conflict')
    ae, be = episodes(a), episodes(b)
    if ae and be and ae != be:
        reasons.append('episode_count_conflict')
    elif require_counts and (not ae or not be):
        reasons.append('episode_count_missing')
    ay, by = positive_int(a.get('year')), positive_int(b.get('year'))
    if ay and by and ay != by:
        reasons.append('year_conflict')
    return reasons


def read_catalog(path):
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Baseline database integrity check failed')
        rows = [dict(row) for row in db.execute('SELECT * FROM entries')]
    sources = Counter(row['source'] for row in rows)
    if any(not sources[source] for source in ('AniList', 'ReAnime', 'Onnada', 'Anissia')):
        raise ValueError('Expected the reviewed four-source baseline database')
    return rows


def build_groups(rows):
    groups = {}
    index = defaultdict(set)
    for row in rows:
        if row['source'] == 'AniList':
            identity = 'AniList:' + row['source_record_id']
            groups[identity] = {'primary': row, 'members': [row], 'keys': keys(row)}
            for title in keys(row):
                index[title].add(identity)
    # ReAnime's recorded AniList IDs are corroborated before becoming aliases.
    for row in rows:
        if row['source'] != 'ReAnime':
            continue
        references = json.loads(row['cross_source_ids_json']).get('anilist_ids', [])
        candidates = {'AniList:' + str(r) for r in references if 'AniList:' + str(r) in groups}
        if not candidates:
            candidates = set().union(*(index.get(k, set()) for k in keys(row)))
        eligible = [identity for identity in candidates
                    if keys(row) & groups[identity]['keys']
                    and not compatible(row, groups[identity]['primary'], require_counts=True)
                    and positive_int(row.get('year'))
                    and row.get('year') == groups[identity]['primary'].get('year')]
        if len(eligible) == 1:
            group = groups[eligible[0]]
            group['members'].append(row)
            group['keys'].update(keys(row))
        else:
            # Keep the unresolved identity separate; never merge distinct AniList works.
            identity = 'ReAnime:' + row['source_record_id']
            groups[identity] = {'primary': row, 'members': [row], 'keys': keys(row)}
    conflicts = []
    for source in ('Onnada', 'Anissia'):
        for row in rows:
            if row['source'] != source:
                continue
            cross = json.loads(row['cross_source_ids_json'])
            refs = {'AniList:' + str(r) for r in cross.get('anilist_ids', [])}
            for identity in refs & groups.keys():
                group, primary = groups[identity], groups[identity]['primary']
                problems = compatible(row, primary)
                if problems:
                    conflicts.append({'source': source, 'source_id': row['source_record_id'],
                                      'target': identity, 'reasons': problems})
                    continue
                if not positive_int(row.get('year')) or row.get('year') != primary.get('year'):
                    continue
                aliases = group['keys'] | {key(member.get('title_ko')) for member in group['members']
                                           if member.get('title_ko')}
                if keys(row) & aliases:
                    group['members'].append(row)
                    # Korean aliases never replace the canonical foreign titles.
    return groups, conflicts


def compare(listings, rows, decisions=None):
    groups, conflicts = build_groups(rows)
    index = defaultdict(set)
    for identity, group in groups.items():
        for title in group['keys']:
            index[title].add(identity)
    outcomes, eligible_by_target = [], defaultdict(list)
    for listing in sorted(listings, key=lambda r: r['source_record_id']):
        candidates = set().union(*(index.get(k, set()) for k in keys(listing)))
        options = []
        for identity in sorted(candidates):
            group = groups[identity]
            primary = group['primary']
            problems = compatible(listing, primary, require_counts=True)
            if not positive_int(primary.get('year')):
                problems.append('baseline_year_missing')
            if str(primary.get('status') or '').upper() == 'CANCELLED':
                problems.append('cancelled_work')
            matching_titles = sorted(keys(listing) & group['keys'])
            options.append({'target': identity, 'title': names(primary), 'year': primary.get('year'),
                            'format': medium(primary), 'episodes': episodes(primary),
                            'matched_title_keys': matching_titles, 'reasons': problems})
        eligible = [option for option in options if not option['reasons']]
        outcome = {'anikoto_id': listing['source_record_id'], 'title': listing['title'],
                   'format': medium(listing), 'episodes': episodes(listing),
                   'status': 'needs_review' if options else 'unmatched', 'candidates': options,
                   'reason': 'multiple_targets' if len(eligible) > 1 else
                             'metadata_conflict_or_missing' if options and not eligible else
                             'no_exact_title' if not options else 'unique_title_and_metadata'}
        if len(eligible) == 1:
            outcome['proposed_target'] = eligible[0]['target']
            eligible_by_target[eligible[0]['target']].append(outcome)
        outcomes.append(outcome)
    for identity, options in eligible_by_target.items():
        for outcome in options:
            if len(options) != 1:
                outcome['reason'] = 'multiple_anikoto_records'
                continue
            outcome['status'], outcome['target'] = 'automatic', identity
    known = {row['anikoto_id']: row for row in outcomes}
    entries = {row['source'] + ':' + row['source_record_id']: row for row in rows}
    memberships = defaultdict(set)
    for identity, group in groups.items():
        for member in group['members']:
            memberships[member['source'] + ':' + member['source_record_id']].add(identity)
    reviewed_targets = set()
    for slug, decision in (decisions or {}).items():
        if slug not in known or not str(decision.get('reason') or '').strip():
            raise ValueError('Reviewed decision requires an existing Anikoto ID and a reason')
        outcome = known[slug]
        if decision['status'] == 'rejected':
            outcome['status'] = 'rejected'
            outcome.pop('target', None)
        elif decision['status'] == 'accepted' and decision.get('target') in entries:
            target = decision['target']
            # Manual targets are source identities, not automatic group keys.
            # A ReAnime member may now live under AniList; an Onnada target
            # may never have had an automatic group. Keep the reviewed target.
            if target not in groups:
                member_groups = memberships[target]
                canonical = groups[next(iter(member_groups))] if len(member_groups) == 1 else None
                groups[target] = {'primary': entries[target],
                                  'members': canonical['members'] if canonical else [entries[target]],
                                  'keys': canonical['keys'] if canonical else keys(entries[target])}
            outcome['status'], outcome['target'] = 'reviewed', target
            reviewed_targets.update(memberships[target] | {target})
        else:
            raise ValueError(f'Invalid reviewed status or target for {slug}: {decision}')
        outcome['reason'] = decision['reason']
    for outcome in outcomes:
        if outcome['status']=='automatic' and outcome['target'] in reviewed_targets:
            outcome['status'],outcome['reason']='needs_review','reviewed_target_already_linked'
            outcome.pop('target')
    links = []
    for outcome in outcomes:
        if outcome['status'] not in ('automatic', 'reviewed'):
            continue
        group = groups[outcome['target']]
        korean = {row['title_ko'] for row in group['members'] if row.get('title_ko')}
        source, source_id = outcome['target'].split(':', 1)
        links.append({'anikoto_id': outcome['anikoto_id'], 'target_source': source, 'target_id': source_id,
                      'status': outcome['status'], 'title': outcome['title'],
                      'korean_title': next(iter(korean)) if len(korean) == 1 else '',
                      'year': group['primary'].get('year'), 'format': outcome['format'],
                      'episodes': outcome['episodes'],
                      'source_refs': sorted([{'source': row['source'], 'id': row['source_record_id']}
                                             for row in group['members']], key=lambda r: (r['source'], r['id'])),
                      'evidence': {'rule': 'reviewed_decision' if outcome['status'] == 'reviewed' else 'unique_exact_title_same_format_and_episode_count',
                                   'matched_title_keys': next((option['matched_title_keys'] for option in outcome['candidates']
                                                               if option['target'] == outcome['target']), []),
                                   'decision_reason': outcome['reason']}})
    return links, outcomes, conflicts


def create_matched_database(baseline, catalog, matches, destination, corrections=None):
    """Copy the baseline and add only source catalog/link tables and reviewed corrections."""
    from catalog_corrections import apply
    from reviewed_subtitle_runtime import refresh_runtime_work_links
    baseline, catalog, destination = map(Path, (baseline, catalog, destination))
    if destination.resolve() in (baseline.resolve(), catalog.resolve()) or destination.exists():
        raise ValueError('Matched database must be a new path separate from the inputs')
    payload = json.loads(Path(matches).read_text(encoding='utf-8'))
    expected = payload['metadata']
    if sha256(baseline) != expected['baseline_sha256'] or sha256(catalog) != expected['anikoto_sha256']:
        raise ValueError('Matching inputs changed; run matching again')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(baseline.resolve().as_uri() + '?mode=ro', uri=True) as source, sqlite3.connect(destination) as target:
        source.backup(target)
    try:
        with sqlite3.connect(destination) as db, sqlite3.connect(catalog.resolve().as_uri() + '?mode=ro', uri=True) as src:
            apply(db, corrections or [])
            refresh_runtime_work_links(db)
            db.execute('CREATE TABLE IF NOT EXISTS anikoto_catalog (source_record_id TEXT PRIMARY KEY,payload_json TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS anikoto_work_links (
                anikoto_id TEXT PRIMARY KEY,target_source TEXT NOT NULL,target_id TEXT NOT NULL,
                status TEXT NOT NULL,payload_json TEXT NOT NULL)''')
            db.execute('CREATE TABLE IF NOT EXISTS anikoto_match_metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            # Reviewed/rejected links already present in the baseline survive new automatic scans.
            preserved = {row[0] for row in db.execute("SELECT anikoto_id FROM anikoto_work_links WHERE status IN ('reviewed','rejected','multi')")}
            db.execute("DELETE FROM anikoto_work_links WHERE status NOT IN ('reviewed','rejected')")
            current_run = expected['crawl_run_id']
            new_links = {link['anikoto_id']:link for link in payload['links']}
            listings = {slug:json.loads(value) for slug,value in catalog_rows(src, current_run)}
            for link in payload['links'] + payload.get('rejections', []):
                if link['anikoto_id'] not in preserved or link['status'] in ('reviewed','rejected'):
                    # Full evidence is exported separately; runtime only needs exact source IDs.
                    content = {'source_refs':link['source_refs']} if link['status'] == 'automatic' else link
                    db.execute('INSERT OR REPLACE INTO anikoto_work_links VALUES (?,?,?,?,?)',
                               (link['anikoto_id'], link.get('target_source',''), link.get('target_id',''), link['status'], json.dumps(content, ensure_ascii=False,separators=(',',':'))))
            # Drop unmatched/stale catalog payloads, retaining old reviewed identities.
            accepted = list(db.execute("SELECT anikoto_id,target_source,target_id,payload_json FROM anikoto_work_links WHERE status IN ('automatic','reviewed')"))
            for slug,source,identity,value in accepted:
                listing = listings.get(slug)
                old = db.execute('SELECT payload_json FROM anikoto_catalog WHERE source_record_id=?',(slug,)).fetchone()
                if listing is None and old:
                    listing = json.loads(old[0])
                if listing is None:
                    raise ValueError('Accepted Anikoto identity has no saved listing: '+slug)
                link = new_links.get(slug)
                if slug in preserved:
                    # An automatic proposal must not change the metadata of a manual target.
                    record = db.execute('SELECT year,title_ko FROM entries WHERE source=? AND source_record_id=?',(source,identity)).fetchone()
                    old_link = json.loads(value)
                    link = {'anikoto_id':slug,'year':record[0] if record else old_link.get('year'),
                            'korean_title':record[1] if record else old_link.get('korean_title','')}
                db.execute('INSERT OR REPLACE INTO anikoto_catalog VALUES (?,?)',
                           (slug,json.dumps(runtime_catalog(listing,link),ensure_ascii=False,separators=(',',':'))))
            db.execute("DELETE FROM anikoto_catalog WHERE source_record_id NOT IN (SELECT anikoto_id FROM anikoto_work_links WHERE status IN ('automatic','reviewed'))")
            db.execute('INSERT OR REPLACE INTO anikoto_match_metadata VALUES (?,?)', ('latest_match_report', json.dumps(expected, ensure_ascii=False)))
            db.execute('INSERT OR REPLACE INTO anikoto_match_metadata VALUES (?,?)', ('runtime_format_version',str(RUNTIME_VERSION)))
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('Matched database failed its integrity check')
        # Reclaim obsolete large payload pages after replacing an older full-data DB.
        with sqlite3.connect(destination) as db:
            db.execute('VACUUM')
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def match(baseline, catalog, output, decisions=None, corrections=None):
    baseline, catalog, output = Path(baseline), Path(catalog), Path(output)
    if baseline.resolve() == catalog.resolve() or output.resolve() in (baseline.resolve(), catalog.resolve()):
        raise ValueError('Keep matching output separate from source databases')
    source_summary = validate_catalog(catalog)
    if not source_summary['complete']:
        raise ValueError('Use a completed crawl for matching; partial snapshots can hide ambiguity')
    before = {'baseline_sha256': sha256(baseline), 'anikoto_sha256': sha256(catalog)}
    rows = read_catalog(baseline)
    if corrections:
        from catalog_corrections import corrected_rows
        rows = corrected_rows(rows, corrections)
    with sqlite3.connect(catalog.resolve().as_uri() + '?mode=ro', uri=True) as db:
        listings = [json.loads(row[1]) for row in catalog_rows(db, source_summary['run_id'])]
    # Carry manual accept/reject decisions into candidate selection as well as DB writes.
    known = {row['source_record_id'] for row in listings}
    retained = {}
    with sqlite3.connect(baseline.resolve().as_uri()+'?mode=ro',uri=True) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='anikoto_work_links'").fetchone():
            for slug,source,identity,status,value in db.execute("SELECT * FROM anikoto_work_links WHERE status IN ('reviewed','rejected')"):
                if slug not in known:
                    continue
                link=json.loads(value)
                reason=link.get('reason') or link.get('evidence',{}).get('decision_reason') or 'Existing reviewed decision'
                retained[slug]={'status':'accepted' if status=='reviewed' else 'rejected','reason':reason}
                if status=='reviewed':retained[slug]['target']=source+':'+identity
    decisions={**retained,**(decisions or {})}
    links, outcomes, conflicts = compare(listings, rows, decisions)
    counts = Counter(row['status'] for row in outcomes)
    summary = {'rule_version': RULE_VERSION, **before, 'crawl_run_id': source_summary['run_id'],
               'records': len(listings), 'automatic': counts['automatic'], 'reviewed': counts['reviewed'],
               'needs_review': counts['needs_review'], 'unmatched': counts['unmatched'], 'rejected': counts['rejected'],
               'existing_source_conflicts': len(conflicts), 'input_databases_unchanged':
               before == {'baseline_sha256': sha256(baseline), 'anikoto_sha256': sha256(catalog)}}
    summary['catalog_corrections'] = len(corrections or [])
    if not summary['input_databases_unchanged']:
        raise ValueError('Input database changed during matching')
    output.mkdir(parents=True, exist_ok=True)
    rejections = [{'anikoto_id':row['anikoto_id'],'status':'rejected','reason':row['reason']}
                  for row in outcomes if row['status'] == 'rejected']
    (output / 'anikoto-matches.json').write_text(json.dumps({'metadata': summary, 'links': links, 'rejections':rejections}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    with (output / 'anikoto-match-review.jsonl').open('w', encoding='utf-8') as out:
        for row in outcomes:
            if row['status'] not in ('automatic', 'reviewed'):
                out.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n')
    (output / 'existing-source-conflicts.json').write_text(json.dumps(conflicts, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (output / 'anikoto-match-report.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--catalog', type=Path, default=DEFAULT_DB)
    parser.add_argument('--output', type=Path, default=ROOT / 'output' / 'anikoto-matches')
    parser.add_argument('--decisions', type=Path, help='Reviewed Anikoto ID -> status, target and reason JSON')
    parser.add_argument('--corrections', type=Path, help='Reviewed baseline corrections JSON')
    parser.add_argument('--corrected-db', type=Path, help='Write a new baseline copy with Anikoto tables and reviewed corrections')
    args = parser.parse_args()
    decisions = json.loads(args.decisions.read_text(encoding='utf-8')) if args.decisions else None
    corrections = json.loads(args.corrections.read_text(encoding='utf-8')) if args.corrections else None
    print(json.dumps(match(args.baseline, args.catalog, args.output, decisions, corrections), ensure_ascii=False, indent=2))
    if args.corrected_db:
        create_matched_database(args.baseline, args.catalog, args.output / 'anikoto-matches.json', args.corrected_db, corrections)


if __name__ == '__main__':
    main()
