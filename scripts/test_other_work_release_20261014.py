import json
import sqlite3
import unittest
from pathlib import Path
from build_other_work_release_20261014 import apply_changes
from other_work_postgres_20261014 import emit
from reviewed_matching_guard import read_rules, verify_role

ROOT=Path(__file__).resolve().parents[1]


class OtherWorkReviewTests(unittest.TestCase):
    def test_decisions_preserve_body_review_and_episode_scope(self):
        plan=json.loads((ROOT/'data/matching/other-work-20261014.json').read_text())
        self.assertEqual(plan['new_episode_assignments'],0)
        self.assertEqual(len(plan['changes']['catalog']),21)
        files=[x for x in plan['changes']['registry'] if x['table']=='subsubsub_file_registry_v1']
        self.assertEqual(len(files),26)
        self.assertTrue(all(x['after']['target_source']=='AniList' and {**x['before'],**x['after']}['target_id']=='57' for x in files))
        self.assertFalse(any(x['table']=='packaged_files_v6' for x in plan['changes']['registry']))
        delta=emit(plan)
        self.assertNotIn('UPDATE anime_subtitle_confirmed_episodes',delta)
        self.assertIn('AND NOT is_manual',delta)

    def test_unexpected_source_state_rolls_back(self):
        with sqlite3.connect(':memory:') as db:
            db.row_factory=sqlite3.Row
            db.execute('CREATE TABLE mapping(id TEXT PRIMARY KEY,target TEXT)')
            db.executemany('INSERT INTO mapping VALUES (?,?)',[('a','old'),('b','changed')]); db.commit()
            changes=[{'table':'mapping','key':{'id':k},'before':{'target':'old'},'after':{'target':'new'}} for k in ['a','b']]
            with self.assertRaises(ValueError):
                with db: apply_changes(db,changes)
            self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM mapping ORDER BY id')],[('a','old'),('b','changed')])

    def test_new_ledger_pins_conan_and_safe_holds(self):
        rules=read_rules()
        if not rules.get('other_work_batch'): self.skipTest('Revised ledger is built with the release')
        table=rules['roles']['catalog']['anikoto_work_link_targets']
        conan=next(x for x in table['rows'] if 'captured-in-her-eyes' in x['key']['anikoto_id'])
        self.assertEqual(conan['expected']['target_id'],'1363')
        entries=rules['roles']['catalog']['entries']['rows']
        for rid in ['2468','3195']:
            entry=next(x for x in entries if x['key']=={'source':'Anissia','source_record_id':rid})
            self.assertEqual(entry['expected']['cross_source_ids_json']['anilist_ids'],[])
            self.assertEqual(entry['expected']['cross_source_ids_json']['reanime_ids'],[])


if __name__=='__main__': unittest.main()
