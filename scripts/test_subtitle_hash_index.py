"""Persisted work-local hashes must avoid reads without trusting stale files."""
import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import subtitle_hash_index as cache
from anissia_zip_storage import ExistingZipIndex, prepare_anissia_files
from subtitle_work_storage import preserve_download, register_sources, track_download_writes


class SubtitleHashIndexTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.target = Path(tmp.name)
        self.work = self.target / 'anissia_subtitles' / 'A'
        self.work.mkdir(parents=True)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def file(self, relative, data):
        path = self.work / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def incoming(self, relative, data):
        destination = self.work / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.target / 'incoming.part'
        temporary.write_bytes(data)
        return preserve_download(temporary, destination)

    def archive(self, relative, members):
        path = self.work / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in members:
                archive.writestr(name, data)
        return path

    def index_json(self):
        return json.loads((self.work / cache.INDEX_NAME).read_text())

    def test_next_run_reuses_old_file_hash_without_reading_its_content(self):
        old = self.file('1화/original.ass', b'one')
        self.assertEqual(self.incoming('2화/renamed.smi', b'one'), old)
        self.assertEqual(self.index_json()['files']['1화/original.ass']['sha256'], hashlib.sha256(b'one').hexdigest())
        real_hash = cache.sha256_file
        with patch.object(cache, 'sha256_file', wraps=real_hash) as hashed:
            self.assertEqual(self.incoming('3화/different.srt', b'one'), old)
        self.assertNotIn(old, [c.args[0] for c in hashed.call_args_list])
        self.assertNotIn(cache.INDEX_NAME, self.index_json()['files'])

    def test_next_run_reuses_zip_member_hashes_without_opening_zip(self):
        old = self.archive('old.zip', [('01.ass', b'one'), ('02.smi', b'two')])
        incoming = self.target / 'new.ass'
        incoming.write_bytes(b'one')
        self.assertEqual(ExistingZipIndex(self.work, set()).match(incoming), old)
        self.assertEqual(len(self.index_json()['files']['old.zip']['zip_members']), 2)
        with patch.object(cache.zipfile, 'ZipFile', side_effect=AssertionError('unchanged ZIP reopened')):
            self.assertEqual(ExistingZipIndex(self.work, set()).match(incoming), old)

    def test_same_size_edit_with_restored_mtime_invalidates_hash_using_ctime(self):
        old = self.file('1화/old.ass', b'one')
        index = cache.WorkHashIndex(self.work)
        original_digest = index.digest(old)
        index.save()
        before = old.stat()
        old.write_bytes(b'two')
        os.utime(old, ns=(before.st_atime_ns, before.st_mtime_ns))
        with patch.object(cache, 'sha256_file', wraps=cache.sha256_file) as hashed:
            current = cache.WorkHashIndex(self.work)
            self.assertNotEqual(current.digest(old), original_digest)
            hashed.assert_called_once_with(old)

    def test_only_changed_file_is_rehashed(self):
        first = self.file('01.ass', b'one')
        second = self.file('02.ass', b'two')
        index = cache.WorkHashIndex(self.work)
        index.digest(first)
        index.digest(second)
        index.save()
        second.write_bytes(b'new')
        with patch.object(cache, 'sha256_file', wraps=cache.sha256_file) as hashed:
            fresh = cache.WorkHashIndex(self.work)
            fresh.digest(first)
            fresh.digest(second)
        self.assertEqual([c.args[0] for c in hashed.call_args_list], [second])

    def test_changed_zip_rebuilds_member_index(self):
        old = self.archive('old.zip', [('01.ass', b'one')])
        incoming = self.target / 'incoming.ass'
        incoming.write_bytes(b'one')
        self.assertEqual(ExistingZipIndex(self.work, set()).match(incoming), old)
        self.archive('old.zip', [('01.ass', b'two')])
        self.assertIsNone(ExistingZipIndex(self.work, set()).match(incoming))
        incoming.write_bytes(b'two')
        self.assertEqual(ExistingZipIndex(self.work, set()).match(incoming), old)

    def test_deleted_and_renamed_files_are_reconciled(self):
        old = self.file('old.ass', b'one')
        index = cache.WorkHashIndex(self.work)
        index.digest(old)
        index.save()
        renamed = old.with_name('renamed.ass')
        old.rename(renamed)
        fresh = cache.WorkHashIndex(self.work)
        fresh.digest(renamed)
        fresh.save()
        self.assertEqual(set(self.index_json()['files']), {'renamed.ass'})
        renamed.unlink()
        cache.WorkHashIndex(self.work).save()
        self.assertEqual(self.index_json()['files'], {})

    def test_corrupt_or_malformed_index_rebuilds_from_files(self):
        old = self.file('old.ass', b'one')
        for text in ('broken json', '{"version": 99, "files": {}}',
                     '{"version": 1, "files": {"../outside.ass": {"signature": []}}}'):
            with self.subTest(text=text):
                (self.work / cache.INDEX_NAME).write_text(text)
                index = cache.WorkHashIndex(self.work)
                self.assertEqual(index.digest(old), hashlib.sha256(b'one').hexdigest())
                index.save()
                self.assertEqual(set(self.index_json()['files']), {'old.ass'})

    def test_partial_downloads_metadata_and_symlinks_are_not_indexed(self):
        self.file('download.part', b'data')
        self.file('.subtitle-sha256-index.stale.tmp', b'data')
        outside = self.target / 'outside.ass'
        outside.write_bytes(b'data')
        (self.work / 'link.ass').symlink_to(outside)
        (self.work / 'linked-dir').symlink_to(self.target)
        self.file('valid.ass', b'data')
        cache.WorkHashIndex(self.work).save()
        self.assertEqual(set(self.index_json()['files']), {'valid.ass'})

    def test_extracted_zip_and_rollback_paths_are_not_left_in_persistent_index(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, 'w') as archive:
            archive.writestr('03.ass', b'three')
        with track_download_writes() as writes:
            path = self.incoming('3화/new.zip', payload.getvalue())
            files = prepare_anissia_files([path], writes)
        self.assertFalse(path.exists())
        rows = self.index_json()['files']
        self.assertEqual(set(rows), {'3화/new/03.ass'})
        self.assertEqual(rows['3화/new/03.ass']['sha256'], hashlib.sha256(b'three').hexdigest())
        self.assertEqual(files, [self.work / '3화/new/03.ass'])

    def test_nested_download_scopes_share_one_index_and_save_at_outer_completion(self):
        old = self.file('old.ass', b'one')
        with track_download_writes():
            self.incoming('2화/one.ass', b'one')
            with track_download_writes():
                self.incoming('3화/one.smi', b'one')
            self.assertFalse((self.work / cache.INDEX_NAME).exists())
        self.assertEqual(set(self.index_json()['files']), {'old.ass'})
        self.assertTrue(old.exists())

    def test_cache_save_failure_does_not_fail_a_successful_download(self):
        with patch.object(cache.WorkHashIndex, 'save', side_effect=OSError('cache unavailable')):
            kept = self.incoming('3화/new.ass', b'three')
        self.assertEqual(kept.read_bytes(), b'three')

    def test_source_registration_also_reuses_persisted_hash(self):
        old = self.incoming('1화/old.ass', b'one')
        item = {'source': 'anissia', 'anime_no': '123', 'anime_title': 'A', 'episode': '1'}
        with patch.object(cache, 'sha256_file', wraps=cache.sha256_file) as hashed:
            register_sources(item, [old], self.target)
        hashed.assert_not_called()


if __name__ == '__main__':
    unittest.main()
