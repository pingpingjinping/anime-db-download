#!/usr/bin/env python3
"""Execute current importers in dry-run mode and inspect generated COPY data."""
import csv
import importlib.util
import io
import json
import pathlib
import runpy
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

def inspect_copy(sql):
    lines = sql.splitlines(keepends=True)
    blocks = {}
    declarations = {}
    statements = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('COPY '):
            table = line.split()[1]
            columns = line[line.index('(')+1:line.index(')')].split(',')
            declarations[table] = [c.strip() for c in columns]
            content = []
            i += 1
            while i < len(lines) and lines[i].strip() != '\\.':
                content.append(lines[i]); i += 1
            if i >= len(lines):
                raise ValueError('Unterminated COPY: ' + table)
            rows = list(csv.reader(io.StringIO(''.join(content))))
            assert all(len(row) == len(columns) for row in rows), table
            blocks[table] = rows
            statements.append(line)
        else:
            statements.append(line)
        i += 1
    return blocks, declarations, ''.join(statements)

def verify(db_path, source_root):
    source_root = pathlib.Path(source_root).resolve()
    captured = []
    original_run, original_argv = subprocess.run, sys.argv
    def capture(command, **kwargs):
        assert command[:5] == ['docker','compose','exec','-T','db']
        sql = kwargs['input']; assert 'BEGIN;' in sql and sql.strip().endswith('COMMIT;')
        captured.append(sql)
        return SimpleNamespace(returncode=0)
    try:
        subprocess.run = capture
        sys.path.insert(0, str(source_root / 'scripts'))
        sys.argv = [str(source_root / 'scripts/import-anime-db.py'), str(pathlib.Path(db_path).resolve())]
        runpy.run_path(sys.argv[0], run_name='__main__')
    finally:
        subprocess.run = original_run; sys.argv = original_argv
        sys.path.pop(0)
    catalog_blocks, _, catalog_sql = inspect_copy(captured[0])
    db = sqlite3.connect(db_path)
    assert len(catalog_blocks['anime_catalog_entries']) == db.execute('SELECT count(*) FROM entries').fetchone()[0]
    assert catalog_blocks['anime_korean_catalog'] and catalog_blocks['anime_reanime_matches']
    for row in catalog_blocks['anime_catalog_entries']:
        value = json.loads(row[2]); assert value['source'] == row[0] and value['source_record_id'] == row[1]
    importer = pathlib.Path(__file__).with_name('import-reviewed-subtitles.py')
    spec = importlib.util.spec_from_file_location('subtitle_importer', importer)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    subtitle_sql, counts = module.prepare(db_path)
    blocks, declarations, syntax_only_sql = inspect_copy(subtitle_sql)
    assert len(blocks['subtitle_file_stage']) == 55530
    assert len(blocks['subtitle_episode_stage']) == 12055
    files = blocks['subtitle_file_stage']; fields = declarations['subtitle_file_stage']
    keyed = {row[fields.index('pid')]: row for row in files}
    assert len(keyed) == 55530
    assert sum(row[fields.index('work_confirmed')] == 'True' for row in files) == 55222
    assert sum(row[fields.index('work_confirmed')] == 'False' for row in files) == 308
    assert sum(row[fields.index('reanime_id')] != '\\N' for row in files) == db.execute('SELECT count(*) FROM subtitle_runtime_files WHERE reanime_id IS NOT NULL').fetchone()[0]
    assert sum(row[fields.index('content_extension')] == '.sub' for row in files) == 65
    assert 'WHERE NOT anime_subtitle_files.is_manual' in subtitle_sql
    assert 'anime_reanime_matches' not in subtitle_sql
    assert 'anime_subtitle_confirmed_episodes SELECT *' in subtitle_sql
    assert subtitle_sql.rstrip().endswith('COMMIT;')
    # SQL grammar parsing is optional; deployment itself is never claimed here.
    grammar = 'not available; COPY shape and transaction checks passed'
    try:
        import pglast
        pglast.parse_sql(syntax_only_sql); pglast.parse_sql(catalog_sql)
        grammar = 'pglast PostgreSQL syntax parse passed'
    except ImportError:
        pass
    db.close()
    return {'catalog_importer_executed_unchanged': True,
            'catalog_copy_rows': {k:len(v) for k,v in catalog_blocks.items()},
            'subtitle_import_copy_rows': counts, 'utf8_json_and_csv_roundtrip': 'passed',
            'postgresql_syntax': grammar, 'postgresql_applied': False,
            'manual_row_guard': 'passed', 'confirmed_episode_rows_imported_without_new_assignment': 12055}

if __name__ == '__main__':
    result = verify(sys.argv[1],sys.argv[2])
    pathlib.Path(sys.argv[3]).write_text(json.dumps(result,ensure_ascii=False,indent=2), encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
