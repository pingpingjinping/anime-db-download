"""Apply explicitly reviewed source-record corrections with identity guards."""
import copy
import json


def corrected_rows(rows, corrections):
    result = copy.deepcopy(rows)
    indexed = {(row['source'], row['source_record_id']): row for row in result}
    for change in corrections:
        row = indexed.get((change['source'], change['id']))
        if not row or not change.get('reason') or not change.get('urls'):
            raise ValueError('Correction requires an existing identity, reason and sources')
        if any(row.get(field) != value for field, value in change['expected'].items()):
            raise ValueError('Correction identity changed; review it again')
        if any(field not in row or field in ('source', 'source_record_id') for field in change.get('set', {})):
            raise ValueError('Correction cannot change schema or source identity')
        row.update(change.get('set', {}))
        cross = json.loads(row['cross_source_ids_json'])
        cross.update(change.get('cross_source_ids', {}))
        row['cross_source_ids_json'] = json.dumps(cross, ensure_ascii=False, sort_keys=True)
        meta = json.loads(row['source_metadata_json'])
        meta.update(change.get('metadata', {}))
        meta['reviewed_catalog_correction'] = {'reason': change['reason'], 'urls': change['urls']}
        row['source_metadata_json'] = json.dumps(meta, ensure_ascii=False, sort_keys=True)
    return result


def apply(db, corrections):
    import sqlite3
    original_factory = db.row_factory
    db.row_factory = sqlite3.Row
    try:
        rows = [dict(row) for row in db.execute('SELECT * FROM entries')]
        revised = corrected_rows(rows, corrections)
        before = {(row['source'], row['source_record_id']): row for row in rows}
        changed = 0
        for row in revised:
            identity = row['source'], row['source_record_id']
            fields = [name for name in row if row[name] != before[identity][name]]
            if not fields:
                continue
            # Only actual schema columns from the read-only row are used as identifiers.
            db.execute('UPDATE entries SET ' + ','.join(name + '=?' for name in fields)
                       + ' WHERE source=? AND source_record_id=?',
                       (*[row[name] for name in fields], *identity))
            changed += 1
        return changed
    finally:
        db.row_factory = original_factory
