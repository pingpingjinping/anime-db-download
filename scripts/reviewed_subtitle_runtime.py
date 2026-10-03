"""Maintain exact-ID runtime lookups without changing reviewed file/episode rows."""
import hashlib
import json
from collections import defaultdict

def refresh_runtime_work_links(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='packaged_files_v6'").fetchone():
        return False
    old_factory = db.row_factory
    import sqlite3
    db.row_factory = sqlite3.Row
    try:
        db.execute('''CREATE TABLE IF NOT EXISTS subtitle_work_reanime_links (
          target_source TEXT NOT NULL,target_id TEXT NOT NULL,reanime_id TEXT NOT NULL,
          link_status TEXT NOT NULL,automatic_eligible INTEGER NOT NULL CHECK(automatic_eligible IN (0,1)),
          PRIMARY KEY(target_source,target_id,reanime_id))''')
        old = [tuple(r) for r in db.execute('SELECT * FROM subtitle_work_reanime_links ORDER BY target_source,target_id,reanime_id')]
        entries = {(r['source'],r['source_record_id']):dict(r) for r in db.execute('SELECT * FROM entries')}
        inverse = defaultdict(set)
        for (source,record_id),entry in entries.items():
            if source == 'ReAnime':
                for aid in json.loads(entry['cross_source_ids_json']).get('anilist_ids',[]):
                    if ('AniList',str(aid)) in entries:
                        inverse[str(aid)].add(record_id)
        rows = []
        for (source,record_id),entry in entries.items():
            cross = json.loads(entry['cross_source_ids_json'])
            ids = {record_id} if source == 'ReAnime' else {str(x) for x in cross.get('reanime_ids',[])}
            if source == 'AniList':
                ids.update(inverse.get(record_id,set()))
            elif entry['existing_match_status'] in ('strong_exact','strong_onnada_bridge'):
                for aid in cross.get('anilist_ids',[]):
                    ids.update(inverse.get(str(aid),set()))
            status = ('direct_record' if source == 'ReAnime' else 'ReAnime_declared_AniList_id' if source == 'AniList'
                      else entry['existing_match_status'] or 'catalog_declared')
            eligible = int(source == 'ReAnime' or len(ids) == 1 and (
                source == 'AniList' or status in ('strong_exact','strong_onnada_bridge')))
            rows.extend((source,record_id,rid,status,eligible) for rid in ids if ('ReAnime',rid) in entries)
        rows.sort()
        if old == rows:
            return False
        db.execute('DELETE FROM subtitle_work_reanime_links')
        db.executemany('INSERT INTO subtitle_work_reanime_links VALUES (?,?,?,?,?)',rows)
        return True
    finally:
        db.row_factory = old_factory

def create_runtime_views(db):
    db.execute('DROP VIEW IF EXISTS subtitle_runtime_files')
    db.execute('''CREATE VIEW subtitle_runtime_files AS
      SELECT f.pid,f.source,f.pack_id,a.zip_relative_path,f.member_path,f.sha256,
        f.bundle_id,f.target_source,f.target_id,f.review_status,f.reason,
        COALESCE(NULLIF(r.work_title_ko,''),e.title_ko) AS work_title_ko,
        r.season_format,e.format AS work_format,
        CASE WHEN f.target_source='ReAnime' THEN f.target_id
             WHEN f.target_id IS NULL THEN NULL
             WHEN r.reanime_id IS NOT NULL THEN r.reanime_id
             ELSE (SELECT w.reanime_id FROM subtitle_work_reanime_links w
                   WHERE w.target_source=f.target_source AND w.target_id=f.target_id
                     AND w.automatic_eligible=1) END AS reanime_id,
        r.language_review,r.quality_notes,
        CASE WHEN lower(f.original_path) LIKE '%.sub' THEN '.sub'
             WHEN lower(f.original_path) LIKE '%.ass' THEN '.ass'
             WHEN lower(f.original_path) LIKE '%.ssa' THEN '.ssa'
             WHEN lower(f.original_path) LIKE '%.srt' THEN '.srt'
             WHEN lower(f.original_path) LIKE '%.smi' THEN '.smi'
             WHEN lower(f.original_path) LIKE '%.vtt' THEN '.vtt' ELSE NULL END AS content_extension,
        CASE WHEN f.target_id IS NULL THEN 0 ELSE 1 END AS work_confirmed
      FROM packaged_files_v6 f JOIN packaged_archives_v6 a USING(pack_id)
      LEFT JOIN entries e ON e.source=f.target_source AND e.source_record_id=f.target_id
      LEFT JOIN subtitle_review_details r USING(pid)
      WHERE f.action='kept' AND (
        lower(f.member_path) LIKE '%.ass' OR lower(f.member_path) LIKE '%.ssa' OR
        lower(f.member_path) LIKE '%.srt' OR lower(f.member_path) LIKE '%.smi' OR
        lower(f.member_path) LIKE '%.vtt' OR lower(f.member_path) LIKE '%.sub' OR
        lower(f.original_path) LIKE '%.ass' OR lower(f.original_path) LIKE '%.ssa' OR
        lower(f.original_path) LIKE '%.srt' OR lower(f.original_path) LIKE '%.smi' OR
        lower(f.original_path) LIKE '%.vtt' OR lower(f.original_path) LIKE '%.sub')''')
    db.execute('DROP VIEW IF EXISTS subtitle_file_episode_links')
    db.execute('''CREATE VIEW subtitle_file_episode_links AS SELECT o.pid,e.map_id,e.file_id,
      e.target_source,e.target_id,e.source_episode,e.target_episode,e.status
      FROM subtitle_file_origins o JOIN subtitle_confirmed_episodes e USING(file_id)''')

def hash_rows(db, table, order):
    h = hashlib.sha256()
    for row in db.execute('SELECT * FROM '+table+' ORDER BY '+order):
        h.update((json.dumps(list(row),ensure_ascii=False,separators=(',',':'))+'\n').encode())
    return h.hexdigest()
