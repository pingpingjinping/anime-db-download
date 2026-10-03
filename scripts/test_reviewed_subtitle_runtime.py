import json
import sqlite3
import unittest
from reviewed_subtitle_runtime import refresh_runtime_work_links,hash_rows

class ExactIdentityTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE entries(source TEXT,source_record_id TEXT,cross_source_ids_json TEXT,existing_match_status TEXT)')
        self.db.execute('CREATE TABLE packaged_files_v6(pid TEXT,target_source TEXT,target_id TEXT)')
        self.db.execute("INSERT INTO packaged_files_v6 VALUES ('protected','AniList','1')")
    def add(self,source,record_id,cross,status=''):
        self.db.execute('INSERT INTO entries VALUES (?,?,?,?)',(source,record_id,json.dumps(cross),status))
    def test_single_recorded_anilist_identity_is_usable_and_file_target_stays(self):
        self.add('AniList','1',{'anilist_ids':['1']})
        self.add('ReAnime','slug',{'anilist_ids':['1']})
        before=hash_rows(self.db,'packaged_files_v6','pid')
        self.assertTrue(refresh_runtime_work_links(self.db))
        self.assertEqual(self.db.execute("SELECT reanime_id,automatic_eligible FROM subtitle_work_reanime_links WHERE target_source='AniList'").fetchall(),[('slug',1)])
        self.assertEqual(hash_rows(self.db,'packaged_files_v6','pid'),before)
        self.assertFalse(refresh_runtime_work_links(self.db))
    def test_multiple_reanime_ids_are_candidates(self):
        self.add('AniList','1',{})
        for rid in ('first','second'):self.add('ReAnime',rid,{'anilist_ids':['1']})
        refresh_runtime_work_links(self.db)
        self.assertEqual(self.db.execute("SELECT sum(automatic_eligible),count(*) FROM subtitle_work_reanime_links WHERE target_source='AniList'").fetchone(),(0,2))
    def test_empty_missing_ids_do_not_create_links(self):
        self.add('AniList','1',{})
        self.add('ReAnime','slug',{'anilist_ids':['','0',None,'missing']})
        refresh_runtime_work_links(self.db)
        self.assertEqual(self.db.execute("SELECT count(*) FROM subtitle_work_reanime_links WHERE target_source='AniList'").fetchone()[0],0)
    def test_legacy_database_is_untouched(self):
        self.db.execute('DROP TABLE packaged_files_v6')
        self.assertFalse(refresh_runtime_work_links(self.db))

if __name__=='__main__':unittest.main()
