import importlib.util
import json
import pathlib
import sqlite3
import tempfile
import unittest
import zipfile

MODULE_PATH = pathlib.Path(__file__).with_name('split_db_v1_release.py')
spec = importlib.util.spec_from_file_location('splitter', MODULE_PATH)
splitter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(splitter)


class SplitDbV1ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.db_path = self.root / 'combined.db'
        with sqlite3.connect(self.db_path) as db:
            db.execute('CREATE TABLE entries(source TEXT,source_record_id TEXT,PRIMARY KEY(source,source_record_id))')
            db.executemany(
                'INSERT INTO entries VALUES (?,?)',
                [(source, str(i)) for i, source in enumerate(sorted(splitter.EXPECTED_SOURCES), 1)],
            )
            db.execute('CREATE TABLE protected_review(id INTEGER PRIMARY KEY,value TEXT)')
            db.execute("INSERT INTO protected_review VALUES (1,'keep me')")
            for name in splitter.REGISTRY_TABLES:
                if name == 'subtitle_runtime_metadata':
                    db.execute('CREATE TABLE subtitle_runtime_metadata(key TEXT PRIMARY KEY,value TEXT)')
                elif name == 'packaged_files_v6':
                    db.execute('CREATE TABLE packaged_files_v6(pid TEXT PRIMARY KEY)')
                elif name == 'subtitle_confirmed_episodes':
                    db.execute('CREATE TABLE subtitle_confirmed_episodes(map_id INTEGER PRIMARY KEY)')
                else:
                    db.execute(f'CREATE TABLE {name}(id INTEGER PRIMARY KEY)')
            db.executemany(
                'INSERT INTO packaged_files_v6 VALUES (?)',
                ((f'p{i}',) for i in range(55530)),
            )
            db.executemany(
                'INSERT INTO subtitle_confirmed_episodes VALUES (?)',
                ((i,) for i in range(12055)),
            )
            db.executemany(
                'INSERT INTO subtitle_runtime_metadata VALUES (?,?)',
                [
                    ('format_version', json.dumps(1)),
                    ('completed_bundles', json.dumps(1109)),
                ],
            )
            db.execute('CREATE TABLE subtitle_work_reanime_links(id INTEGER PRIMARY KEY)')
            db.execute('CREATE VIEW subtitle_runtime_files AS SELECT pid FROM packaged_files_v6')
            db.execute('CREATE VIEW subtitle_file_episode_links AS SELECT map_id FROM subtitle_confirmed_episodes')
        self.source_zip = self.root / 'combined.zip'
        with zipfile.ZipFile(self.source_zip, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.write(self.db_path, 'anime.db')

    def tearDown(self):
        self.temp.cleanup()

    def test_split_preserves_catalog_and_registry_rows(self):
        out = self.root / 'out'
        report = splitter.split_release(self.source_zip, out)
        self.assertGreater(report['monthly_catalog_download_bytes_saved'], 0)
        validated = splitter.validate_split(
            out / splitter.CATALOG_ASSET,
            out / splitter.REGISTRY_ASSET,
        )
        self.assertEqual(validated['registry']['subtitle_files'], 55530)
        catalog_db = self.root / 'catalog.db'
        splitter.extract_database(out / splitter.CATALOG_ASSET, catalog_db)
        with sqlite3.connect(catalog_db) as db:
            self.assertEqual(
                db.execute('SELECT value FROM protected_review WHERE id=1').fetchone()[0],
                'keep me',
            )
            self.assertFalse(db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='packaged_files_v6'"
            ).fetchone())


if __name__ == '__main__':
    unittest.main()
