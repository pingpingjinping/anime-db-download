"""Re-evaluate pending works while preserving the reviewed catalog."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date

from work_identity import (
    base_identity, base_keys, compatibility, declared_anilist_ids, identity, indexed_keys, installment,
    medium, native_keys, part, season, titles, validated_reference, year,
)

MIN_CANDIDATE_SCORE = 70
MIN_AUTOMATIC_SCORE = 95
RULE_VERSION = 2


def metadata_compatible(a, b):
    conflicts, missing = compatibility(a, b)
    return not conflicts and not any(x in missing for x in ('season_missing', 'part_missing'))


def load_rows(db, source):
    old_factory = db.row_factory
    db.row_factory = __import__('sqlite3').Row
    try:
        return {row['source_record_id']: dict(row) for row in db.execute(
            'SELECT * FROM entries WHERE source=?', (source,))}
    finally:
        db.row_factory = old_factory


def prepare_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS monthly_match_pending (
        source TEXT NOT NULL, source_record_id TEXT NOT NULL,
        PRIMARY KEY(source, source_record_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS monthly_auto_matches (
        reanime_id TEXT PRIMARY KEY, anissia_no INTEGER NOT NULL UNIQUE,
        korean_title TEXT NOT NULL, evidence_json TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS monthly_match_candidates (
        anissia_no INTEGER NOT NULL, reanime_id TEXT NOT NULL,
        score INTEGER NOT NULL, reason TEXT NOT NULL, evidence_json TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        PRIMARY KEY(anissia_no, reanime_id))""")


def reviewed_ids(db, anissia):
    """The initial reviewed links, including rejected alternatives, stay reserved."""
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    ani = {str(r[0]) for r in db.execute("SELECT anime_no FROM anissia_year_match")} if 'anissia_year_match' in tables else set()
    reserved = set()
    for aid in ani:
        row = anissia.get(aid)
        if row:
            reserved.update(str(x) for x in json.loads(row['cross_source_ids_json']).get('reanime_ids', []))
    if 'anissia_year_candidates' in tables and 'anissia_year_match' in tables:
        reserved.update(str(r[0]) for r in db.execute(
            "SELECT reanime_id FROM anissia_year_candidates WHERE reanime_id!='' "
            "AND anime_no IN (SELECT anime_no FROM anissia_year_match)"))
    return ani, reserved


def target_index(reanime, anilist, reserved):
    """Return title index plus validated aliases and work identities."""
    by_title, aliases, groups, references = defaultdict(set), {}, {}, {}
    invalid_ids = {}
    for rid, row in reanime.items():
        if rid in reserved:
            continue
        declared = declared_anilist_ids(row)
        valid = sorted(aid for aid in declared if aid in anilist and validated_reference(row, anilist[aid]))
        invalid_ids[rid] = sorted(declared - set(valid))
        references[rid] = valid
        groups[rid] = 'AniList:' + valid[0] if len(valid) == 1 else 'ReAnime:' + rid
        aliases[rid] = [row] + [anilist[aid] for aid in valid]
        for alias in aliases[rid]:
            for key in base_keys(alias):
                by_title[key].add(rid)
    return by_title, aliases, groups, references, invalid_ids


def refresh_auto_matches(db):
    """Store unique links; keep conflicts, missing evidence and URL ambiguity distinct."""
    prepare_tables(db)
    anissia, reanime, onnada, anilist = (load_rows(db, source) for source in ('Anissia', 'ReAnime', 'Onnada', 'AniList'))
    pending = {(str(r[0]), str(r[1])) for r in db.execute('SELECT source,source_record_id FROM monthly_match_pending')}
    old_auto = {tuple(r) for r in db.execute('SELECT * FROM monthly_auto_matches')}
    # Previously generated links remain subject to re-evaluation, unlike manual review.
    for rid, aid, _, _ in old_auto:
        pending.update({('Anissia', str(aid)), ('ReAnime', rid)})
    reviewed_ani, reviewed_re = reviewed_ids(db, anissia)
    by_title, aliases, groups, references, invalid_ids = target_index(reanime, anilist, reviewed_re)
    reserved_works = set()
    for rid in reviewed_re:
        if rid not in reanime:
            continue
        for raw in declared_anilist_ids(reanime[rid]):
            reference = anilist.get(str(raw))
            if reference and validated_reference(reanime[rid], reference):
                reserved_works.add('AniList:' + str(raw))
    for aid in reviewed_ani:
        row = anissia.get(aid)
        if not row:
            continue
        for raw in json.loads(row['cross_source_ids_json']).get('anilist_ids', []):
            reference = anilist.get(str(raw))
            if reference and validated_reference(row, reference):
                reserved_works.add('AniList:' + str(raw))
    on_by_ko = defaultdict(list)
    for row in onnada.values():
        if row.get('title_ko'):
            on_by_ko[identity(row['title_ko'])].append(row)

    possible = defaultdict(dict)
    no_candidates = set()
    for aid, a in anissia.items():
        if aid in reviewed_ani:
            continue
        evidence_by_re = defaultdict(set)
        source_keys, source_native = base_keys(a), native_keys(a)
        for key in source_keys:
            for rid in by_title.get(key, ()):
                evidence_by_re[rid].add('exact_title')
                if key in source_native and any(key in native_keys(alias) for alias in aliases[rid]):
                    evidence_by_re[rid].add('native_exact')
        for on in on_by_ko.get(identity(a.get('title_ko')), ()):
            on_conflicts, on_missing = compatibility(a, on)
            if on_conflicts or any(x in on_missing for x in ('season_missing', 'part_missing', 'year_missing')):
                continue
            foreign_keys = base_keys(on) - {base_identity(on.get('title_ko'))}
            for key in foreign_keys:
                for rid in by_title.get(key, ()):
                    if metadata_compatible(on, reanime[rid]) and year(on) == year(reanime[rid]):
                        evidence_by_re[rid].add('onnada_bridge:' + on['source_record_id'])

        # A containment match is only an alias-review hint. Never an auto link.
        if not evidence_by_re and ('Anissia', aid) in pending:
            for native in source_native:
                if len(native) < 8:
                    continue
                for key, ids in by_title.items():
                    if min(len(native), len(key)) >= 8 and (native in key or key in native):
                        for rid in ids:
                            conflicts, _ = compatibility(a, reanime[rid])
                            if not conflicts:
                                evidence_by_re[rid].add('title_containment')

        for rid, evidence in evidence_by_re.items():
            r = reanime[rid]
            conflicts, missing = compatibility(a, r)
            if groups[rid] in reserved_works:
                conflicts.append('reviewed_work_reserved')
            # Validated reference metadata may complete an omitted target type.
            # Installment labels remain separate from title equality.
            if any(raw in anilist for raw in invalid_ids[rid]):
                missing.append('external_id_unverified')
            native_difference = source_native and native_keys(r) and not source_native & native_keys(r)
            bridge = any(x.startswith('onnada_bridge:') for x in evidence)
            title_confirmed = 'exact_title' in evidence or bridge
            if native_difference and not bridge:
                missing.append('native_alias_needed')
            if not title_confirmed:
                missing.append('title_alias_needed')
            score = min(100, (80 if title_confirmed else 70) + (10 if year(a) and year(a) == year(r) else 0)
                        + (5 if not conflicts and not missing else 0)
                        + (5 if medium(a) and medium(a) == medium(r) else 0))
            details = {'rule_version': RULE_VERSION, 'year': year(r), 'anissia_year': year(a),
                       'evidence': sorted(evidence), 'conflicts': sorted(set(conflicts)),
                       'missing': sorted(set(missing)), 'work_identity': groups[rid],
                       'validated_anilist_ids': references[rid], 'unverified_anilist_ids': invalid_ids[rid],
                       'season_compatible': not conflicts and not any(x.endswith('_missing') and x.startswith(('season', 'part')) for x in missing),
                       'onnada_year_support': bridge, 'automatic_eligible': title_confirmed and not conflicts and not missing}
            possible[aid][rid] = (score, details)
        if ('Anissia', aid) in pending and not possible.get(aid):
            no_candidates.add(aid)

    # Only confirmed conflicts are excluded from uniqueness. Unknown evidence
    # and a second URL for the same validated work still require review.
    viable = {}
    for aid, options in possible.items():
        ids = {rid for rid, (_, d) in options.items() if not d['conflicts']}
        confirmed = any(d['automatic_eligible'] and score >= MIN_AUTOMATIC_SCORE
                        for score, d in options.values())
        ss, ps = installment(anissia[aid])
        for rid in list(ids):
            details = options[rid][1]
            missing = set(details['missing'])
            # An explicitly identified S2/Part2 candidate is stronger evidence
            # than a series-only title. Keep the hint for review; do not call it
            # S1 or a confirmed conflict. Unknown year/type/ID still blocks.
            hint = (confirmed and bool(missing) and missing <= {'season_missing', 'part_missing'}
                    and all(ss if label == 'season_missing' else ps for label in missing))
            if hint:
                ids.remove(rid)
            details['candidate_role'] = 'series_hint' if hint else 'work_candidate'
        viable[aid] = ids
    by_work = defaultdict(set)
    for aid, ids in viable.items():
        for rid in ids:
            by_work[groups[rid]].add(aid)
    accepted, candidates = {}, []
    for aid, options in possible.items():
        ids = viable[aid]
        work_ids = {groups[rid] for rid in ids}
        chosen = next(iter(ids)) if len(ids) == 1 else None
        for rid, (score, details) in sorted(options.items()):
            if ('Anissia', aid) not in pending and ('ReAnime', rid) not in pending:
                continue
            auto = (rid == chosen and details['automatic_eligible'] and score >= MIN_AUTOMATIC_SCORE
                    and len(by_work[groups[rid]]) == 1 and bool(anissia[aid].get('title_ko')))
            if auto:
                accepted[rid] = (int(aid), anissia[aid]['title_ko'], json.dumps(details, ensure_ascii=False, sort_keys=True))
                continue
            if details['conflicts']:
                reason = details['conflicts'][0]
            elif len(ids) > 1 and len(work_ids) == 1:
                reason = 'multiple_urls'
            elif len(work_ids) > 1 or len(by_work[groups[rid]]) > 1:
                reason = 'multiple_work_candidates'
            elif details['missing']:
                reason = details['missing'][0]
            else:
                reason = 'metadata_missing'
            candidates.append((int(aid), rid, score, reason, json.dumps(details, ensure_ascii=False, sort_keys=True)))
    for aid in no_candidates:
        details = {'rule_version': RULE_VERSION, 'anissia_year': year(anissia[aid]), 'evidence': []}
        candidates.append((int(aid), '', 0, 'no_catalog_entry', json.dumps(details, ensure_ascii=False, sort_keys=True)))

    new_auto = {(rid, *values) for rid, values in accepted.items()}
    old_candidates = {tuple(r) for r in db.execute(
        'SELECT anissia_no,reanime_id,score,reason,evidence_json FROM monthly_match_candidates')}
    new_candidates = set(candidates)
    auto_changed, candidates_changed = old_auto != new_auto, old_candidates != new_candidates
    if auto_changed:
        db.execute('DELETE FROM monthly_auto_matches')
        db.executemany('INSERT INTO monthly_auto_matches VALUES (?,?,?,?)', sorted(new_auto))
    if candidates_changed:
        observed = date.today().isoformat()
        db.execute('DELETE FROM monthly_match_candidates')
        db.executemany('INSERT INTO monthly_match_candidates VALUES (?,?,?,?,?,?)',
                       [(*row, observed) for row in sorted(new_candidates)])
    review_works = {r[0] for r in new_candidates} - {r[1] for r in new_auto}
    return {'auto_matched': len(new_auto), 'needs_review': len(new_candidates),
            'review_works': len(review_works), 'changed': auto_changed or candidates_changed}
