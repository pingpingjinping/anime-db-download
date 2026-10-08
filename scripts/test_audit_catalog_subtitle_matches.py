import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import audit_catalog_subtitle_matches as audit


class CatalogAuditTests(unittest.TestCase):
    def test_existing_onnada_links_are_protected_including_shared_anikoto_identity(self):
        db = sqlite3.connect(':memory:');db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE entries(source,source_record_id,title_ko,existing_match_status,cross_source_ids_json)')
        db.execute('CREATE TABLE anikoto_work_link_targets(target_source,target_id,anikoto_id,payload_json)')
        rows = [('Onnada', '1', 'TV', 'review', {'reanime_ids':['tv-id']}),
                ('Onnada', '2', 'Movie', 'review', {'anilist_ids':[123]}),
                ('Onnada', '3', 'Unlinked', 'review', {})]
        db.executemany('INSERT INTO entries VALUES(?,?,?,?,?)', [(s,i,t,status,json.dumps(cross)) for s,i,t,status,cross in rows])
        db.execute('INSERT INTO anikoto_work_link_targets VALUES(?,?,?,?)', ('AniList','123','ak-movie','{}'))
        pending,protected = audit.onnada_inputs(db)
        self.assertEqual([item['id'] for item in pending], ['3'])
        self.assertEqual([item['id'] for item in protected], ['1','2'])
        self.assertEqual(protected[1]['existing_anikoto_ids'], ['ak-movie'])
        db.close()

    def test_only_unlinked_subtitle_files_replay_without_borrowing_filename_identity(self):
        db = sqlite3.connect(':memory:');db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE packaged_files_v6(pid,source,bundle_id,target_id,original_path,member_path,review_status,action)')
        db.execute('CREATE TABLE subtitle_review_details(pid,work_title_ko,identified_work,season_format)')
        db.executemany('INSERT INTO packaged_files_v6 VALUES(?,?,?,?,?,?,?,?)', [
            ('linked','naver','b','original-tv','episode.smi','episode.smi','confirmed','kept'),
            ('font','naver','b',None,'font.ttf','font.ttf','hold','kept'),
            ('held','aniall','h',None,'Fate Movie 2020.smi','Fate Movie 2020.smi','hold','kept'),
            ('deleted','naver','d',None,'removed.smi','removed.smi','hold','deleted')])
        db.execute('INSERT INTO subtitle_review_details VALUES(?,?,?,?)', ('held','기존 검수 작품','Existing Work','TV 2019'))
        pending,total,protected = audit.subtitle_inputs(db)
        self.assertEqual(total, {'naver':1,'aniall':1})
        self.assertEqual(protected, {'naver':1})
        self.assertEqual(len(pending),1)
        self.assertEqual(pending[0]['row']['year'],2019)
        self.assertEqual(pending[0]['row']['format'],'TV')
        self.assertEqual(pending[0]['row']['title_ko'],'기존 검수 작품')
        self.assertEqual(pending[0]['file_ids'], ['held'])
        db.close()

    def test_disk_inputs_open_readonly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'catalog.db'
            db=sqlite3.connect(path);db.execute('CREATE TABLE entries(id)');db.commit();db.close()
            before=audit.digest(path);readonly=audit.readonly(path)
            with self.assertRaises(sqlite3.OperationalError):readonly.execute('INSERT INTO entries VALUES(1)')
            readonly.close()
            self.assertEqual(before,audit.digest(path))

    def test_rule_eligible_results_remain_unconfirmed_and_keep_original_identity(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE entries(source,source_record_id,title_english,title_native,cross_source_ids_json)')
        db.execute('CREATE TABLE anikoto_work_link_targets(target_source,target_id,anikoto_id)')
        db.execute('INSERT INTO entries VALUES(?,?,?,?,?)', ('ReAnime','tv','Work','','{}'))
        item={'source':'Onnada','id':'original-77','row':{'title_ko':'작품','format':'TV','year':2020}}
        decision={'samples':[{'reanime_id':'tv','candidates':[], 'automatic_evidence':{'rule_version':4}}]}
        with patch.object(audit,'replay',return_value=decision) as replay:
            result=audit.run_group([item],None,db)
        self.assertEqual(result[0]['source'],'Onnada')
        self.assertEqual(result[0]['id'],'original-77')
        self.assertEqual(result[0]['status'],'unique_rule_candidate_review_required')
        self.assertEqual(result[0]['proposed_match_evidence'],{'rule_version':4})
        self.assertNotIn('source',item['row'])
        self.assertEqual(replay.call_args[0][0]['samples'][0]['source'],'Anissia')
        db.close()


if __name__ == '__main__':unittest.main()
