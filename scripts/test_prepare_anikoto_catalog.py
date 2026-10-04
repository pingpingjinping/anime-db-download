import contextlib
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import crawl_anikoto as crawler
import prepare_anikoto_catalog as saved
from test_crawl_anikoto import BASE, page_html


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SavedCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / 'output' / 'catalog.sqlite'

    def fixture(self):
        source = self.root / 'source.sqlite'
        with contextlib.redirect_stdout(io.StringIO()):
            summary = crawler.crawl(source, BASE, fetch=lambda url: page_html(['original'], 1))
        archive = self.root / 'seed.zip'
        with zipfile.ZipFile(archive, 'w') as out:
            out.write(source, 'anikoto-catalog.sqlite')
        manifest = self.root / 'seed.json'
        manifest.write_text(json.dumps({
            'archive': archive.name, 'archive_sha256': digest(archive),
            'database_sha256': digest(source), **summary,
        }))
        return manifest, archive

    def test_actual_snapshot_restores_and_exports_all_records_without_network(self):
        output = self.root / 'export'
        arguments = ['prepare', '--db', str(self.target), '--export-dir', str(output)]
        with patch('requests.Session.request', side_effect=AssertionError('Network forbidden')), \
                patch('sys.argv', arguments), contextlib.redirect_stdout(io.StringIO()) as stdout:
            saved.main()
        result = json.loads(stdout.getvalue())
        self.assertEqual((result['database_origin'], result['network_requests']), ('seed', 0))
        self.assertTrue(result['complete'])
        self.assertEqual((result['pages_collected'], result['records_in_run']), (299, 8949))
        manifest = json.loads(saved.DEFAULT_SEED.read_text())
        self.assertEqual(digest(self.target), manifest['database_sha256'])
        with zipfile.ZipFile(saved.DEFAULT_SEED.parent / manifest['archive']) as original:
            self.assertEqual((output / 'anikoto-catalog.jsonl').read_bytes(), original.read('anikoto-catalog.jsonl'))
        rows = [json.loads(line) for line in (output / 'anikoto-catalog.jsonl').read_text().splitlines()]
        self.assertEqual(len({row['source_record_id'] for row in rows}), 8949)
        self.assertTrue(all(row['match_status'] == 'unmatched' for row in rows))
        before = digest(self.target)
        with patch('requests.Session.request', side_effect=AssertionError('Network forbidden')):
            origin, summary = saved.prepare(self.target)
        self.assertEqual(origin, 'existing')
        self.assertEqual(summary['run_id'], manifest['run_id'])
        self.assertEqual(digest(self.target), before)

    def test_existing_partial_crawl_is_preserved_instead_of_replaced_by_seed(self):
        with contextlib.redirect_stdout(io.StringIO()):
            partial = crawler.crawl(self.target, BASE, fetch=lambda url: page_html(['newer']), max_pages=1)
        before = digest(self.target)
        origin, summary = saved.prepare(self.target, self.root / 'absent-manifest.json')
        self.assertEqual(origin, 'existing')
        self.assertEqual(summary['run_id'], partial['run_id'])
        self.assertFalse(summary['complete'])
        self.assertEqual(digest(self.target), before)

    def test_invalid_existing_file_is_not_overwritten_or_silently_reset(self):
        self.target.parent.mkdir()
        self.target.write_bytes(b'existing data that must survive')
        with self.assertRaises(sqlite3.DatabaseError):
            saved.prepare(self.target)
        self.assertEqual(self.target.read_bytes(), b'existing data that must survive')

    def test_corrupted_archive_is_rejected_before_database_creation(self):
        manifest, archive = self.fixture()
        with archive.open('ab') as out:
            out.write(b'corruption')
        with self.assertRaisesRegex(ValueError, 'archive checksum'):
            saved.prepare(self.target, manifest)
        self.assertFalse(self.target.exists())

    def test_database_checksum_or_completed_run_mismatch_is_rejected(self):
        manifest, _ = self.fixture()
        original = json.loads(manifest.read_text())
        for field, value, message in [('database_sha256', '0' * 64, 'database checksum'),
                                      ('records_in_run', 99, 'completed crawl manifest')]:
            with self.subTest(field=field):
                manifest.write_text(json.dumps({**original, field: value}))
                with self.assertRaisesRegex(ValueError, message):
                    saved.prepare(self.target, manifest)
                self.assertFalse(self.target.exists())
                self.assertEqual(list(self.target.parent.glob('tmp*.sqlite')) if self.target.parent.exists() else [], [])

    def test_database_created_during_restore_is_not_replaced(self):
        manifest, _ = self.fixture()
        original_link = saved.os.link
        def simultaneous_writer(source, destination):
            Path(destination).write_bytes(b'concurrent database')
            return original_link(source, destination)
        with patch.object(saved.os, 'link', side_effect=simultaneous_writer), self.assertRaises(FileExistsError):
            saved.prepare(self.target, manifest)
        self.assertEqual(self.target.read_bytes(), b'concurrent database')


if __name__ == '__main__':
    unittest.main()
