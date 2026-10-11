"""Re-evaluate pending works while preserving the reviewed catalog."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date

from work_identity import (
    base_identity, base_keys, compatibility, declared_anilist_ids, identity, indexed_keys, installment,
    medium, native_keys, part, season, titles, validated_reference, year,
)

MIN_CANDIDATE_SCORE = 70
MIN_AUTOMATIC_SCORE = 95
RULE_VERSION = 4


def reading_head(row):
    """A bracketed reading may accompany a short kanji title; review only."""
    value = row.get('title_native') or ''
    match = re.fullmatch(r'\s*(.+?)\s*[〈《<（(【]\s*[^〉》>）)】]+[〉》>）)】]\s*', value)
    if match:
        head = identity(match.group(1))
        if len(head) >= 3 and re.search(r'[\u3400-\u9fff]', head):
            return head
    return None


def contains_title(left, right):
    short, long = sorted((left, right), key=len)
    start = long.find(short)
    if start < 0:
        return False
    end = start + len(short)
    # Do not find installment 3 inside 30 (or an embedded title digit).
    return not (short[0].isdigit() and start and long[start - 1].isdigit()
                or short[-1].isdigit() and end < len(long) and long[end].isdigit())


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
    # Today's explicit corrections may have a hold with no prior
    # anissia_year_match row. The embedded review marker is authoritative:
    # do not send these works through automatic matching again.
    for aid, row in anissia.items():
        metadata = json.loads(row.get('source_metadata_json') or '{}')
        if metadata.get('reviewed_matching_20261011'):
            ani.add(str(aid))
    reserved = set()
    # Administrator work-ID overrides are authoritative, even where there is
    # no historical anissia_year_match row. Preserve an Anikoto-only choice
    # against speculative new ReAnime matching until the admin resets it.
    if "pi_manual_work_match_overrides" in tables:
        for rid, aid in db.execute(
                "SELECT reanime_id,anissia_no FROM pi_manual_work_match_overrides"):
            ani.add(str(aid))
            reserved.add(str(rid))
    if "pi_manual_anikoto_work_overrides" in tables:
        ani.update(str(r[0]) for r in db.execute(
            "SELECT anissia_no FROM pi_manual_anikoto_work_overrides"))
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
    # These aliases expand review recall only, never automatic title equality.
    short_native = defaultdict(set)
    for rid, rows in aliases.items():
        for row in rows:
            key = identity(row.get('title_native'))
            if key:
                short_native[key].add(rid)
            if head := reading_head(row):
                short_native[head].add(rid)

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
        if ('Anissia', aid) in pending:
            for key in {identity(a.get('title_native')), reading_head(a)} - {None, ''}:
                for rid in short_native.get(key, ()):
                    if 'exact_title' not in evidence_by_re[rid]:
                        evidence_by_re[rid].add('bracket_reading_alias')
            # Specials often replace OVA with a bonus subtitle. Keep literal
            # season digits and require the same known type and release year.
            if medium(a) in ('OVA', 'SPECIAL'):
                for native in source_native:
                    if len(native) < 8:
                        continue
                    for rid, rows in aliases.items():
                        if medium(reanime[rid]) != medium(a) or year(a) != year(reanime[rid]):
                            continue
                        for row in rows:
                            for key in native_keys(row):
                                if key.startswith(native) and key != native and not key[len(native)].isdigit():
                                    evidence_by_re[rid].add('special_subtitle_alias')
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
                    if min(len(native), len(key)) >= 8 and contains_title(native, key):
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
            # The native field sometimes contains an English release title.
            # Only differing Japanese-script titles require a native alias.
            japanese = lambda t: bool(re.search(r'[\u3040-\u30ff\u3400-\u9fff]', t or ''))
            native_difference = (japanese(a.get('title_native')) and japanese(r.get('title_native'))
                                 and source_native and native_keys(r) and not source_native & native_keys(r))
            # An identical complete native title in the same year can identify
            # a named sequel whose English alias supplies the season number.
            # This does not turn a missing season into S1 or erase conflicts.
            if (year(a) and year(a) == year(r)
                    and identity(a.get('title_native')) == identity(r.get('title_native'))
                    and identity(a.get('title_native'))
                    and not installment(a)[0] and installment(r)[0]):
                missing = [m for m in missing if m != 'season_missing']
                evidence.add('full_native_installment')
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
                       'source_seasons': sorted(installment(a)[0]), 'target_seasons': sorted(installment(r)[0]),
                       'source_parts': sorted(installment(a)[1]), 'target_parts': sorted(installment(r)[1]),
                       'full_native_exact': bool(identity(a.get('title_native'))) and
                           identity(a.get('title_native')) == identity(r.get('title_native')),
                       'evidence': sorted(evidence), 'conflicts': sorted(set(conflicts)),
                       'missing': sorted(set(missing)), 'work_identity': groups[rid],
                       'validated_anilist_ids': references[rid], 'unverified_anilist_ids': invalid_ids[rid],
                       'season_compatible': not conflicts and not any(x.endswith('_missing') and x.startswith(('season', 'part')) for x in missing),
                       'onnada_year_support': bridge, 'automatic_eligible': title_confirmed and not conflicts and not missing}
            possible[aid][rid] = (score, details)
        if ('Anissia', aid) in pending and not possible.get(aid):
            no_candidates.add(aid)

    # Rank complete, corroborated candidates above incomplete alternatives.
    # Weak candidates remain visible, but cannot veto a unique strong match.
    viable = {}
    for aid, options in possible.items():
        strong = {rid for rid, (score, d) in options.items()
                  if d['automatic_eligible'] and score >= MIN_AUTOMATIC_SCORE}
        viable[aid] = strong or {rid for rid, (_, d) in options.items() if not d['conflicts']}
        for rid, (_, details) in options.items():
            details['candidate_role'] = ('work_candidate' if rid in viable[aid] else
                                         'conflicting' if details['conflicts'] else 'weaker_candidate')
    by_work = defaultdict(set)
    strong_by_work = defaultdict(set)
    for aid, ids in viable.items():
        for rid in ids:
            by_work[groups[rid]].add(aid)
            score, d = possible[aid][rid]
            if d['automatic_eligible'] and score >= MIN_AUTOMATIC_SCORE:
                strong_by_work[groups[rid]].add(aid)
    for group, ids in strong_by_work.items():
        by_work[group] = ids
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
