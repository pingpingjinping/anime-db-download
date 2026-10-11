import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from export_cloudflare_search_sql import build, emit_sql, normalized_title, reviewed_artwork


class CloudflareSearchExportTests(unittest.TestCase):
    def setUp(self):
        self.cat = sqlite3.connect(':memory:')
        self.reg = sqlite3.connect(':memory:')
        self.cat.row_factory = sqlite3.Row
        self.reg.row_factory = sqlite3.Row
        self.addCleanup(self.cat.close)
        self.addCleanup(self.reg.close)
        self.cat.execute('''CREATE TABLE entries (
          source TEXT,source_record_id TEXT,title_ko TEXT,title_native TEXT,
          title_romaji TEXT,title_english TEXT,year TEXT,source_metadata_json TEXT)''')
        self.reg.execute('CREATE TABLE subtitle_runtime_metadata (key TEXT,value TEXT)')
        self.reg.executemany('INSERT INTO subtitle_runtime_metadata VALUES (?,?)', [
            ('format_version','1'), ('completed_bundles','1109')])
        self.reg.execute('''CREATE TABLE subtitle_runtime_files_export (
          pid TEXT,source TEXT,target_source TEXT,target_id TEXT,
          work_title_ko TEXT,work_confirmed INTEGER)''')
        for i in range(1100):
            self.add('Anissia',str(i+1),f'검수 작품 {i+1}',native=f'日本語{i}',year='2025')
        self.add('Onnada','10','같은 제목',english='Title 1')
        self.add('Onnada','11','같은 제목',english='Title 2')
        self.add('AniList','30','',english='English title')
        self.add('ReAnime','slug1','',english='R-15')

    def add(self,source,id,title,native='',english='',year='2025'):
        self.cat.execute('INSERT INTO entries VALUES (?,?,?,?,?,?,?,?)',
                         (source,id,title,native,'',english,year,json.dumps({'captionCount': 3})))

    def test_keeps_seasons_separate_and_only_confirmed_subtitles(self):
        self.reg.executemany('INSERT INTO subtitle_runtime_files_export VALUES (?,?,?,?,?,?)', [
            ('1','naver','Onnada','10','같은 제목',1),
            ('2','aniall','Onnada','11','같은 제목',1),
            ('3','naver','ReAnime','slug1','R-15',1),
            ('4','naver','AniList','30','거짓 연결',0),
            ('5','naver','Onnada','11','보류',0),
            ('6','naver','Onnada','10','다른 이름 후보',1),
        ])
        works, aliases, source_counts, _ = build(self.cat,self.reg)
        ids = {w[0] for w in works}
        self.assertTrue({'db_Onnada_10','db_Onnada_11','re_slug1','ani_1'} <= ids)
        self.assertNotIn('db_AniList_30',ids)
        self.assertEqual(len([w for w in works if w[1]=='같은 제목']),2)
        on10 = next(w for w in works if w[0]=='db_Onnada_10')
        self.assertEqual(on10[4],2)
        self.assertNotIn(('db_Onnada_11', normalized_title('보류')), aliases)
        self.assertIn(('re_slug1', normalized_title('R-15')), aliases)
        with tempfile.TemporaryDirectory() as temp:
            sqlfile = Path(temp)/'import.sql'
            emit_sql(works, aliases, sqlfile)
            d1 = sqlite3.connect(':memory:')
            d1.executescript('''CREATE TABLE works (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, year INTEGER, source_name TEXT,
                caption_count INTEGER,detail_url TEXT,poster TEXT);
                CREATE TABLE work_aliases (work_id TEXT,alias_norm TEXT,
                PRIMARY KEY(work_id,alias_norm),FOREIGN KEY(work_id) REFERENCES works(id));''')
            d1.executescript(sqlfile.read_text(encoding='utf-8'))
            self.assertEqual(d1.execute('SELECT COUNT(*) FROM works').fetchone()[0],len(works))
            self.assertEqual(d1.execute('SELECT COUNT(*) FROM work_aliases').fetchone()[0],len(aliases))
            d1.close()

    def test_cloudflare_uses_only_reviewed_artwork_by_exact_id_priority(self):
        self.cat.execute("""CREATE TABLE pi_search_poster_cache(
            work_id TEXT PRIMARY KEY,poster TEXT,source TEXT)""")
        self.cat.execute("INSERT INTO pi_search_poster_cache VALUES (?,?,?)",
                         ("db_Onnada_10","https://cdn.example/ani-list.jpg","AniList"))
        self.cat.execute("INSERT INTO pi_search_poster_cache VALUES (?,?,?)",
                         ("re_slug1","https://cdn.example/reanime.jpg","ReAnime"))
        self.cat.execute("INSERT INTO pi_search_poster_cache VALUES (?,?,?)",
                         ("db_Onnada_11","https://cdn.example/another-season.jpg","AniList"))
        self.cat.execute("""CREATE TABLE pi_reviewed_work_groups(
            members_json TEXT)""")
        self.cat.execute("INSERT INTO pi_reviewed_work_groups VALUES(?)",
                         (json.dumps(["db_Onnada_10","re_slug1"]),))
        self.reg.executemany('INSERT INTO subtitle_runtime_files_export VALUES (?,?,?,?,?,?)',[
            ('1','naver','Onnada','10','같은 제목',1),
            ('2','naver','Onnada','11','같은 제목',1),
            ('3','naver','ReAnime','slug1','R-15',1),
        ])
        works,_,_,_=build(self.cat,self.reg)
        by_id={row[0]:row for row in works}
        self.assertEqual(by_id["db_Onnada_10"][-1],"https://cdn.example/reanime.jpg")
        self.assertEqual(by_id["db_Onnada_11"][-1],"https://cdn.example/another-season.jpg")
        with tempfile.TemporaryDirectory() as temp:
            sqlfile=Path(temp)/"import.sql"
            emit_sql(works,[],sqlfile)
            text=sqlfile.read_text(encoding="utf-8")
            self.assertIn("https://cdn.example/reanime.jpg",text)
            self.assertNotIn("http://cdn.example/reanime.jpg",text)

    def test_reject_unsafe_urls_and_do_not_merge_identical_titles(self):
        self.cat.execute("CREATE TABLE pi_search_poster_cache(work_id TEXT,poster TEXT,source TEXT)")
        self.cat.executemany("INSERT INTO pi_search_poster_cache VALUES(?,?,?)",[
            ("db_Onnada_10","javascript:alert(1)","AniList"),
            ("db_Onnada_11","https://img.example/other.jpg","AniList"),
        ])
        entries={(r["source"],r["source_record_id"]):dict(r)
                 for r in self.cat.execute("SELECT * FROM entries")}
        posters=reviewed_artwork(self.cat,entries)
        self.assertNotIn("db_Onnada_10",posters)
        self.assertEqual(posters["db_Onnada_11"],"https://img.example/other.jpg")

    def test_v6_packaged_tables_when_runtime_view_absent(self):
        self.reg.execute('DROP TABLE subtitle_runtime_files_export')
        self.reg.execute('''CREATE TABLE packaged_files_v6 (
          pid TEXT, source TEXT, pack_id TEXT, member_path TEXT,
          original_path TEXT, action TEXT, target_source TEXT,
          target_id TEXT, review_status TEXT)''')
        self.reg.execute('CREATE TABLE packaged_archives_v6 (pack_id TEXT PRIMARY KEY)')
        self.reg.execute('CREATE TABLE subtitle_review_details (pid TEXT PRIMARY KEY, work_title_ko TEXT)')
        self.reg.execute("INSERT INTO packaged_archives_v6 VALUES ('pack1')")
        self.reg.executemany('INSERT INTO packaged_files_v6 VALUES (?,?,?,?,?,?,?,?,?)', [
            ('keep','naver','pack1','subtitles/01.srt','subtitles/01.srt','kept','Onnada','10','confirmed'),
            ('not-subtitle','naver','pack1','cover.jpg','cover.jpg','kept','Onnada','10','confirmed'),
            ('discard','naver','pack1','02.srt','02.srt','removed','Onnada','10','confirmed'),
            ('unmatched','aniall','pack1','03.srt','03.srt','kept','Onnada',None,'hold'),
            ('wrong-source','anissia','pack1','04.srt','04.srt','kept','Onnada','10','confirmed'),
            ('other-season','aniall','pack1','01.ass','01.ass','kept','Onnada','11','confirmed'),
        ])
        self.reg.executemany('INSERT INTO subtitle_review_details VALUES (?,?)', [
            ('keep','검수한 제목'),('other-season','다른 시즌')])
        works, aliases, _, linked = build(self.cat,self.reg)
        by_id = {w[0]:w for w in works}
        self.assertEqual(linked,2)
        self.assertEqual(by_id['db_Onnada_10'][4],1)
        self.assertEqual(by_id['db_Onnada_11'][4],1)
        self.assertIn(('db_Onnada_10',normalized_title('검수한 제목')), aliases)
        self.assertIn(('db_Onnada_11',normalized_title('다른 시즌')), aliases)

    def test_unicode_normalization_matches_worker(self):
        self.assertEqual(normalized_title(' ＡＰＩ 연결 테스트☆ '),'api연결테스트☆')
        self.assertEqual(normalized_title('R-15'),'r15')

    def test_missing_registry_metadata_fails(self):
        self.reg.execute("DELETE FROM subtitle_runtime_metadata WHERE key='completed_bundles'")
        with self.assertRaisesRegex(ValueError,'format or completion'):
            build(self.cat,self.reg)

    def add_reviewed_arrest_fixture(self, status='multi'):
        self.add('Onnada','1073','체포하겠어',native='逮捕しちゃうぞ',year='1996')
        self.add('Onnada','1001','체포하겠어 SECOND SEASON',year='2001')
        self.add('AniList','1372','',native='逮捕しちゃうぞ (TV)',english="You're Under Arrest! (TV)",year='1996')
        self.add('AniList','1577','',native='逮捕しちゃうぞ',year='1994')
        self.add('AniList','2014','',native='逮捕しちゃうぞ SECOND SEASON',year='2001')
        self.cat.execute('ALTER TABLE entries ADD COLUMN cross_source_ids_json TEXT')
        self.cat.execute('ALTER TABLE entries ADD COLUMN existing_match_status TEXT')
        self.cat.execute("UPDATE entries SET cross_source_ids_json=?,existing_match_status='review',source_metadata_json=? WHERE source='Onnada' AND source_record_id='1073'",
                         (json.dumps({'anilist_ids':['1372','1577']}),json.dumps({'onnada_manual_review_v1':{'status':status}})))
        self.cat.execute("UPDATE entries SET cross_source_ids_json=?,existing_match_status='strong_exact' WHERE source='Onnada' AND source_record_id='1001'",
                         (json.dumps({'anilist_ids':['2014']}),))
        self.reg.executemany('INSERT INTO subtitle_runtime_files_export VALUES (?,?,?,?,?,?)',[
            ('tv','naver','AniList','1372',None,1),('ova','naver','AniList','1577',None,1),
            ('s2','naver','AniList','2014',None,1)])

    def test_reviewed_korean_name_reaches_tv_ova_and_separate_second_season(self):
        self.add_reviewed_arrest_fixture()
        before,_,_,_=build(self.cat,self.reg)
        # Main titles, caption counts, links and every work identity remain unchanged.
        self.cat.execute("UPDATE entries SET cross_source_ids_json='{}'")
        without_aliases,_,_,_=build(self.cat,self.reg)
        self.assertEqual([row[:1]+row[2:] for row in before],
                         [row[:1]+row[2:] for row in without_aliases])
        self.cat.execute("UPDATE entries SET cross_source_ids_json=? WHERE source='Onnada' AND source_record_id='1073'",(json.dumps({'anilist_ids':['1372','1577']}),))
        self.cat.execute("UPDATE entries SET cross_source_ids_json=? WHERE source='Onnada' AND source_record_id='1001'",(json.dumps({'anilist_ids':['2014']}),))
        works,aliases,_,_=build(self.cat,self.reg)
        hits={wid for wid,a in aliases if '체포하겠어' in a}
        self.assertEqual(hits,{'db_AniList_1372','db_AniList_1577','db_AniList_2014'})
        self.assertNotIn(('db_AniList_1372',normalized_title('체포하겠어 SECOND SEASON')),aliases)
        self.assertNotIn(('db_AniList_1577',normalized_title('체포하겠어 SECOND SEASON')),aliases)
        self.assertEqual(next(w for w in works if w[0]=='db_AniList_1372')[5],'/catalog/AniList/1372')
        self.assertEqual(next(w for w in works if w[0]=='db_AniList_1372')[1],'체포하겠어')

    def test_unreviewed_or_explicitly_held_links_do_not_lend_korean_names(self):
        self.add_reviewed_arrest_fixture(status='hold')
        self.cat.execute("UPDATE entries SET existing_match_status='strong_exact' WHERE source='Onnada' AND source_record_id='1073'")
        _,aliases,_,_=build(self.cat,self.reg)
        self.assertNotIn(('db_AniList_1372',normalized_title('체포하겠어')),aliases)
        self.assertNotIn(('db_AniList_1577',normalized_title('체포하겠어')),aliases)
        self.cat.execute("UPDATE entries SET existing_match_status='review',source_metadata_json='{}' WHERE source='Onnada' AND source_record_id='1073'")
        _,aliases,_,_=build(self.cat,self.reg)
        self.assertNotIn(('db_AniList_1372',normalized_title('체포하겠어')),aliases)

    def test_unique_reanime_card_opens_playable_detail_with_korean_title(self):
        self.add_reviewed_arrest_fixture()
        self.reg.execute('''CREATE TABLE subtitle_work_reanime_links(
            target_source TEXT,target_id TEXT,reanime_id TEXT,automatic_eligible INTEGER)''')
        self.reg.execute("INSERT INTO subtitle_work_reanime_links VALUES('AniList','1372','slug1',1)")
        works,aliases,_,_=build(self.cat,self.reg)
        card=next(w for w in works if w[0]=='re_slug1')
        self.assertEqual(card[1],'체포하겠어')
        self.assertEqual(card[5],'/anime/re_slug1')
        self.assertNotIn('db_AniList_1372',{w[0] for w in works})
        self.assertIn(('re_slug1',normalized_title("You're Under Arrest! (TV)")),aliases)

    def test_nav_aniall_anissia_cards_merge_only_by_unique_reviewed_ids(self):
        self.add('AniList','97625','',native='つぐもも')
        self.add('AniList','108266','',native='継つぐもも')
        self.add('ReAnime','tsugumomo-first','',native='つぐもも')
        self.add('ReAnime','tsugumomo-second','',native='継つぐもも')
        for rid,title in [('1','츠구모모'),('2','계 츠구모모'),('3','츠구모모 OVA')]:
            self.cat.execute("UPDATE entries SET title_ko=? WHERE source='Anissia' AND source_record_id=?",(title,rid))
        self.reg.execute('''CREATE TABLE subtitle_work_reanime_links(
            target_source TEXT,target_id TEXT,reanime_id TEXT,automatic_eligible INTEGER)''')
        self.reg.executemany('INSERT INTO subtitle_work_reanime_links VALUES(?,?,?,?)',[
            ('Anissia','1','tsugumomo-first',1),('AniList','97625','tsugumomo-first',1),
            ('Anissia','2','tsugumomo-second',1),('AniList','108266','tsugumomo-second',1)])
        self.reg.executemany('INSERT INTO subtitle_runtime_files_export VALUES(?,?,?,?,?,?)',[
            ('a','aniall','AniList','97625',None,1),('b','naver','AniList','97625',None,1),
            ('c','naver','AniList','108266',None,1),
            ('a','aniall','ReAnime','tsugumomo-first',None,1)])
        works,aliases,_,_=build(self.cat,self.reg)
        hits={wid for wid,a in aliases if '츠구모모' in a}
        self.assertEqual(hits,{'ani_1','ani_2','ani_3'})
        first=next(w for w in works if w[0]=='ani_1')
        self.assertEqual(first[1],'츠구모모')
        self.assertEqual(first[3],'AniAll · 네이버 · 애니시아')
        self.assertEqual(first[4],5)  # two unique local pids + three Anissia captions
        self.assertEqual(first[5],'/anime/ani_1')
        self.assertIn(('ani_1',normalized_title('つぐもも')),aliases)
        self.assertIn(('ani_2',normalized_title('継つぐもも')),aliases)

    def test_ambiguous_or_ineligible_links_do_not_collapse_separate_works(self):
        self.add_reviewed_arrest_fixture()
        self.reg.execute('''CREATE TABLE subtitle_work_reanime_links(
            target_source TEXT,target_id TEXT,reanime_id TEXT,automatic_eligible INTEGER)''')
        self.reg.executemany('INSERT INTO subtitle_work_reanime_links VALUES(?,?,?,?)',[
            ('AniList','1372','slug1',1),('AniList','1372','unknown-other-season',1),
            ('AniList','1577','slug1',0)])
        works,_,_,_=build(self.cat,self.reg)
        ids={w[0] for w in works}
        self.assertIn('db_AniList_1372',ids)
        self.assertIn('db_AniList_1577',ids)

    def test_multiple_anissia_registrations_do_not_choose_one_arbitrarily(self):
        self.reg.execute('''CREATE TABLE subtitle_work_reanime_links(
            target_source TEXT,target_id TEXT,reanime_id TEXT,automatic_eligible INTEGER)''')
        self.reg.executemany('INSERT INTO subtitle_work_reanime_links VALUES(?,?,?,?)',[
            ('Anissia','1','slug1',1),('Anissia','2','slug1',1),('AniList','30','slug1',1)])
        self.reg.execute("INSERT INTO subtitle_runtime_files_export VALUES('file','naver','AniList','30',NULL,1)")
        works,_,_,_=build(self.cat,self.reg)
        ids={w[0] for w in works}
        self.assertTrue({'ani_1','ani_2','re_slug1'}<=ids)
        self.assertNotIn('db_AniList_30',ids)


if __name__=='__main__':
    unittest.main()


