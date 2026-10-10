import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from export_cloudflare_search_sql import build, emit_sql, normalized_title


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


if __name__=='__main__':
    unittest.main()
