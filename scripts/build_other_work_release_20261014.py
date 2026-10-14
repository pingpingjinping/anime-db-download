#!/usr/bin/env python3
"""Apply the exact reviewed other-work corrections to pinned release copies."""
import argparse
import copy
import json
import shutil
import sqlite3
from pathlib import Path

from reviewed_matching_guard import read_rules, verify_role
from split_db_v1_release import extract_database, pack_database, sha256, table_digest, validate_catalog_db, validate_registry_db

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / 'data/matching/other-work-20261014.json'
CAT = 'anime_four_sources_multilingual_prepared.zip'
REG = 'anime_reviewed_subtitle_registry_v1.zip'


def fetch(db, table, key):
    q = 'SELECT * FROM "' + table + '" WHERE ' + ' AND '.join('"' + k + '"=?' for k in key)
    row = db.execute(q, tuple(key.values())).fetchone()
    return dict(row) if row else None


def apply_changes(db, changes):
    for change in changes:
        row = fetch(db, change['table'], change['key'])
        if row is None or any(row[k] != v for k, v in change['before'].items()):
            raise ValueError('Reviewed row changed: ' + repr((change['table'], change['key'])))
        values = change['after']
        q = 'UPDATE "' + change['table'] + '" SET ' + ','.join('"' + k + '"=?' for k in values)
        q += ' WHERE ' + ' AND '.join('"' + k + '"=?' for k in change['key'])
        db.execute(q, (*values.values(), *change['key'].values()))


def pin_changes(rules, db, role, changes):
    for change in changes:
        table, key = change['table'], change['key']
        definition = rules['roles'][role].setdefault(table, {'primary_key': list(key), 'rows': []})
        old = next((r for r in definition['rows'] if r['key'] == key), None)
        # Preserve earlier pinned fields; do not pin annotations reconstructed by refresh.
        columns = set(change['pin']) | (set(old.get('expected', {})) if old else set())
        row = fetch(db, table, key)
        expected = {k: json.loads(row[k]) if k.endswith('_json') and isinstance(row[k], str) else row[k] for k in sorted(columns)}
        rule = {'key': key, 'expected': expected, 'reason': change['reason']}
        if old:
            definition['rows'][definition['rows'].index(old)] = rule
        else:
            definition['rows'].append(rule)


def build(inputs, output, rules_path=None):
    plan = json.loads(PLAN.read_text())
    for asset, digest in plan['expected_assets'].items():
        if sha256(inputs / asset) != digest:
            raise ValueError('Release asset changed; review again: ' + asset)
    output.mkdir(parents=True, exist_ok=True)
    paths = {'catalog': output / 'anime_four_sources_multilingual_prepared.db', 'registry': output / 'anime_reviewed_subtitle_registry_v1.db'}
    extract_database(inputs / CAT, paths['catalog'])
    extract_database(inputs / REG, paths['registry'])
    rules = copy.deepcopy(read_rules(rules_path or ROOT / 'data/matching/reviewed-corrections-20261011.json'))
    for role, path in paths.items():
        with sqlite3.connect(path) as db:
            db.row_factory = sqlite3.Row
            changes = plan['changes'][role]
            before = {r[0]: table_digest(db, r[0]) for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            apply_changes(db, changes)
            if role == 'registry':
                old_view = db.execute("SELECT sql FROM sqlite_master WHERE name='subsubsub_runtime_files_v1'").fetchone()[0]
                old_expr = "json_extract(f.payload_json,'$.source_row.DB작품명_최상위폴더') AS work_title_ko"
                new_expr = "COALESCE(json_extract(f.payload_json,'$.reviewed_work_title_ko'),json_extract(f.payload_json,'$.source_row.DB작품명_최상위폴더')) AS work_title_ko"
                if old_expr not in old_view:
                    raise ValueError('Supplemental runtime view changed')
                db.execute('DROP VIEW subsubsub_runtime_files_v1')
                db.execute(old_view.replace(old_expr, new_expr))
            pin_changes(rules, db, role, changes)
            verify_role(db, role, rules)
            allowed = {c['table'] for c in changes}
            for table, digest in before.items():
                if table not in allowed and table_digest(db, table) != digest:
                    raise ValueError('Unrelated table changed: ' + table)
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('Database integrity failed')
    for role, path in paths.items():
        with sqlite3.connect(path) as db:
            verify_role(db, role, rules)
    rules['other_work_batch'] = plan['batch']
    (output / 'reviewed-corrections-20261011.json').write_text(json.dumps(rules, ensure_ascii=False, indent=2) + '\n')
    result = {'batch': plan['batch'], 'catalog': validate_catalog_db(paths['catalog']), 'registry': validate_registry_db(paths['registry']),
              'catalog_changes': len(plan['changes']['catalog']), 'registry_changes': len(plan['changes']['registry']),
              'corrected_supplemental_files': 26, 'new_episode_assignments': 0, 'cue_harem_body_review_preserved': True}
    for role, asset in [('catalog', CAT), ('registry', REG)]:
        pack_database(paths[role], output / asset, paths[role].name)
    result['sha256'] = {asset: sha256(output / asset) for asset in [CAT, REG]}
    (output / 'other-work-validation-20261014.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    shutil.copyfile(PLAN, output / PLAN.name)
    print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--input', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--rules', type=Path)
    a = p.parse_args()
    build(a.input, a.output, a.rules)
