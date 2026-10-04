import json
import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import package_anikoto_release as release


class ReleasePackageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.baseline=self.root/'baseline.db'
        self.matched=self.root/'matched.db'
        self.catalog=self.root/'catalog.sqlite'
        self.evidence=self.root/'evidence'
        self.evidence.mkdir()
        self.output=self.root/'release'
        self.catalog.write_bytes(b'Saved listing fixture')
        for name in ['anikoto-catalog.jsonl','anikoto-crawl-report.json']:
            (self.evidence/name).write_text('{}\n')
        with sqlite3.connect(self.baseline) as db:
            db.execute('CREATE TABLE entries(source TEXT,source_record_id TEXT,title_ko TEXT)')
            db.executemany('INSERT INTO entries VALUES (?,?,?)',[(s,'1',s) for s in ['AniList','ReAnime','Onnada','Anissia']])
            db.executescript("CREATE TABLE protected_episode(file_id TEXT PRIMARY KEY,episode INTEGER); INSERT INTO protected_episode VALUES ('same',1);")
        self.report={'baseline_sha256':release.digest(self.baseline),'anikoto_sha256':release.digest(self.catalog),'crawl_run_id':'run'}
        (self.evidence/'anikoto-match-report.json').write_text(json.dumps(self.report))
        shutil.copyfile(self.baseline,self.matched)
        with sqlite3.connect(self.matched) as db:
            db.executescript('''CREATE TABLE anikoto_catalog(source_record_id TEXT PRIMARY KEY,payload_json TEXT);
                CREATE TABLE anikoto_work_links(anikoto_id TEXT PRIMARY KEY,target_source TEXT,target_id TEXT,status TEXT,payload_json TEXT);
                CREATE TABLE anikoto_match_metadata(key TEXT PRIMARY KEY,value TEXT);
                INSERT INTO anikoto_catalog VALUES ('linked','{"title":"Linked"}');
                INSERT INTO anikoto_work_links VALUES ('linked','AniList','1','automatic','{}');
                INSERT INTO anikoto_match_metadata VALUES ('runtime_format_version','2');''')
            db.execute('INSERT INTO anikoto_match_metadata VALUES (?,?)',('latest_match_report',json.dumps(self.report)))
        self.validation=patch.object(release,'validate_catalog',return_value={'complete':True,'run_id':'run'})
        self.validation.start()
        self.addCleanup(self.validation.stop)

    def package(self):
        return release.package(self.baseline,self.matched,self.catalog,self.evidence,self.evidence,self.output)

    def test_runtime_contains_only_one_db_and_raw_catalog_is_separate(self):
        before=[release.digest(p) for p in [self.baseline,self.matched,self.catalog]]
        result=self.package()
        self.assertEqual(before,[release.digest(p) for p in [self.baseline,self.matched,self.catalog]])
        self.assertEqual(result['runtime_works'],1)
        with zipfile.ZipFile(self.output/release.RUNTIME_ASSET) as archive:
            self.assertEqual(archive.namelist(),['anime_four_sources_multilingual_prepared.db'])
            self.assertEqual(archive.read(archive.namelist()[0]),self.matched.read_bytes())
        with zipfile.ZipFile(self.output/release.CATALOG_ASSET) as archive:
            self.assertEqual(set(archive.namelist()),{'anikoto-catalog.sqlite','anikoto-catalog.jsonl','anikoto-crawl-report.json'})
        self.assertEqual(result['protected_tables']['protected_episode']['rows'],1)

    def test_changed_confirmed_episode_blocks_release_creation(self):
        with sqlite3.connect(self.matched) as db:db.execute('UPDATE protected_episode SET episode=2')
        with self.assertRaisesRegex(ValueError,'Protected baseline rows changed'):
            self.package()
        self.assertFalse(self.output.exists())

    def test_unreviewed_source_record_change_blocks_publication(self):
        with sqlite3.connect(self.matched) as db:db.execute("UPDATE entries SET title_ko='Wrong work' WHERE source='Anissia'")
        with self.assertRaisesRegex(ValueError,'outside the reviewed corrections'):
            self.package()

    def test_unmatched_catalog_or_missing_accepted_catalog_is_rejected(self):
        with sqlite3.connect(self.matched) as db:db.execute("INSERT INTO anikoto_catalog VALUES ('unmatched','{}')")
        with self.assertRaisesRegex(ValueError,'exactly the accepted'):
            self.package()
        with sqlite3.connect(self.matched) as db:db.execute('DELETE FROM anikoto_catalog')
        with self.assertRaisesRegex(ValueError,'exactly the accepted'):
            self.package()

    def test_partial_crawl_or_changed_input_cannot_replace_release(self):
        with patch.object(release,'validate_catalog',return_value={'complete':False}):
            with self.assertRaisesRegex(ValueError,'Incomplete or changed'):
                self.package()
        self.catalog.write_bytes(b'Changed after matching')
        with self.assertRaisesRegex(ValueError,'Incomplete or changed'):
            self.package()

    def test_different_match_report_is_rejected(self):
        with sqlite3.connect(self.matched) as db:
            db.execute("UPDATE anikoto_match_metadata SET value='{}' WHERE key='latest_match_report'")
        with self.assertRaisesRegex(ValueError,'does not correspond'):
            self.package()


if __name__=='__main__':
    unittest.main()
