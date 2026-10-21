import unittest,sqlite3,json
from reviewed_multimatch_search import extend_projection
from apply_multimatch_20261021 import apply,state

class MultiMatchTests(unittest.TestCase):
    def test_publication_rollback_preserves_newer_edit(self):
        from publish_multimatch_20261021 import rollback_action
        self.assertEqual(rollback_action('third-party','old','ours'),'preserve_newer')
        self.assertEqual(rollback_action('ours','old','ours'),'restore_ours')
        self.assertEqual(rollback_action(None,'old','ours'),'restore_missing')
        self.assertEqual(rollback_action('old','old','ours'),'keep')
    def test_shared_source_does_not_collapse_seasons(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        db.executescript('CREATE TABLE entries(source,source_record_id,year); CREATE TABLE multimatch_search_works_20261021(work_id,identity_ref,title_ko,aliases_json,member_ids_json,title_status,confidence,evidence_json);')
        for i in ['1','2']:
            db.execute('INSERT INTO entries VALUES(?,?,?)',('AniList',i,2020))
            db.execute('INSERT INTO multimatch_search_works_20261021 VALUES(?,?,?,?,?,?,?,?)',('db_AniList_'+i,i,'테스트 '+i+'기',json.dumps(['Test season '+i]),json.dumps(['re_combined','db_AniList_'+i]),'제안','중간','{}'))
        works,aliases=extend_projection(db,[('re_combined','Combined',2020,'ReAnime',3,'/anime/re_combined','')],[],lambda x:x.lower().replace(' ',''))
        self.assertEqual({r[0] for r in works},{'re_combined','db_AniList_1','db_AniList_2'})
        self.assertIn(('db_AniList_2','testseason2'),aliases);self.assertEqual(sum(r[4] for r in works),3)
    def test_exact_single_work_preserves_existing_card(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        db.executescript('CREATE TABLE multimatch_search_works_20261021(work_id,identity_ref,title_ko,aliases_json,member_ids_json,title_status,confidence,evidence_json);')
        db.execute('INSERT INTO multimatch_search_works_20261021 VALUES(?,?,?,?,?,?,?,?)',('db_AniList_1','1','확인 제목','["English title"]','["re_one","db_AniList_1"]','기존명','중간','{}'))
        works,aliases=extend_projection(db,[('re_one','Old title',2020,'ReAnime',7,'/anime/re_one','')],[],str.lower)
        self.assertEqual(len(works),1);self.assertEqual(works[0][1],'확인 제목');self.assertEqual(works[0][4],7);self.assertIn(('re_one','english title'),aliases)
    def test_patch_rejects_drift_rolls_back_and_reverses(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row;db.executescript("CREATE TABLE t(id TEXT PRIMARY KEY,v TEXT); INSERT INTO t VALUES('1','old');")
        plan={'new_tables':{'review':'CREATE TABLE review(id TEXT PRIMARY KEY)'},'operations':[{'table':'t','key':{'id':'1'},'before':{'v':'old'},'after':{'v':'new'}}]}
        before=state(db);apply(db,plan);after=state(db)
        with self.assertRaises(ValueError):apply(db,plan)
        self.assertEqual(state(db),after);apply(db,plan,True);self.assertEqual(state(db),before)
        db.execute("UPDATE t SET v='external' WHERE id='1'");db.commit()
        with self.assertRaises(ValueError):apply(db,plan)
        self.assertEqual(db.execute('SELECT v FROM t').fetchone()[0],'external');self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='review'").fetchone())
if __name__=='__main__':unittest.main()
