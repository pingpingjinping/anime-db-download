"""Hash-only reuse of incoming downloads inside one Anissia work tree."""
import contextlib
import errno
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import pi_download_subtitle_queues as queues
from pi_download_erulabo import finalize_erulabo_download
from subtitle_work_storage import (
    load_manifest, preserve_download, register_sources, track_download_writes,
)
from write_weekly_dashboard import files_from_success
import write_weekly_dashboard as dashboard


class AnissiaHashDedupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.work = self.root / 'anissia_subtitles' / 'A'
        self.work.mkdir(parents=True)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def existing(self, relative, data):
        path = self.work / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def incoming(self, relative, data):
        destination = self.work / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + '.part')
        temporary.write_bytes(data)
        return preserve_download(temporary, destination)

    def item(self, episode='3'):
        return {'id': f'anissia:test-{episode}', 'source': 'anissia', 'anime_no': '123',
                'anime_title': 'A', 'episode': episode, 'creator': 'maker',
                'source_url': 'https://example.test/3', 'handler': 'tistory'}

    def test_only_episode_three_remains_in_new_folder_with_renamed_old_episodes(self):
        first = self.existing('1화/old-maker/old name.ass', b'episode one')
        second = self.existing('2화/deep/folder/another name.smi', b'episode two')
        first_stat = first.stat()
        second_stat = second.stat()
        with track_download_writes() as writes:
            self.assertEqual(self.incoming('3화/maker/renamed 01.srt', b'episode one'), first)
            self.assertEqual(self.incoming('3화/maker/renamed 02.ass', b'episode two'), second)
            third = self.incoming('3화/maker/new 03.ass', b'episode three')
        self.assertEqual(list((self.work / '3화/maker').iterdir()), [third])
        self.assertEqual(writes.created, {third})
        self.assertEqual(writes.reused, {first, second})
        for path, before in ((first, first_stat), (second, second_stat)):
            self.assertEqual(path.stat().st_ino, before.st_ino)
            self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)

    def test_equal_names_with_different_hashes_are_kept(self):
        first = self.existing('1화/maker/subtitle.ass', b'old version')
        new = self.incoming('3화/maker/subtitle.ass', b'new version')
        self.assertNotEqual(first, new)
        self.assertEqual(first.read_bytes(), b'old version')
        self.assertEqual(new.read_bytes(), b'new version')

        # A corrected same-path download also keeps the previous version.
        corrected = self.incoming('3화/maker/subtitle.ass', b'corrected version')
        self.assertNotEqual(corrected, new)
        self.assertEqual(new.read_bytes(), b'new version')

    def test_hidden_subtitle_filename_still_matches_by_hash(self):
        old = self.existing('1화/.renamed.ass', b'subtitle')
        self.assertEqual(self.incoming('3화/different.smi', b'subtitle'), old)

    def test_does_not_compare_other_works_or_aniall(self):
        other = self.root / 'anissia_subtitles' / 'B' / 'same.ass'
        other.parent.mkdir()
        other.write_bytes(b'data')
        new = self.incoming('3화/maker/new.ass', b'data')
        self.assertNotEqual(new, other)
        self.assertTrue(new.exists())
        aniall = self.root / 'aniall_subtitles' / 'A'
        aniall.mkdir(parents=True)
        (aniall / 'first.ass').write_bytes(b'data')
        temporary = aniall / 'new.part'
        temporary.write_bytes(b'data')
        self.assertEqual(preserve_download(temporary, aniall / 'second.ass'), aniall / 'second.ass')

    def test_existing_duplicates_are_never_deleted(self):
        a = self.existing('1화/a.ass', b'same')
        b = self.existing('2화/b.ass', b'same')
        result = self.incoming('3화/c.ass', b'same')
        self.assertIn(result, {a, b})
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())
        self.assertFalse((self.work / '3화/c.ass').exists())

    def test_duplicates_inside_one_new_batch_keep_one_copy(self):
        with track_download_writes() as writes:
            a = self.incoming('3화/a.ass', b'same')
            b = self.incoming('3화/deep/b.smi', b'same')
        self.assertEqual(a, b)
        self.assertEqual(writes.created, {a})
        self.assertEqual(len(list(self.work.rglob('*.ass'))), 1)

    def test_zip_is_compared_as_raw_bytes_without_unpacking(self):
        archive = self.work / '1화/one.zip'
        archive.parent.mkdir()
        with zipfile.ZipFile(archive, 'w') as out:
            out.writestr('01.ass', b'subtitle')
        self.assertEqual(self.incoming('3화/renamed.zip', archive.read_bytes()), archive)
        other = io.BytesIO()
        with zipfile.ZipFile(other, 'w') as out:
            out.writestr('01.ass', b'subtitle')
            out.comment = b'different archive bytes'
        kept = self.incoming('3화/different.zip', other.getvalue())
        self.assertTrue(kept.exists())
        self.assertEqual(list(self.work.rglob('*.ass')), [])

    def test_symlinks_partial_transfers_and_conversion_inputs_are_not_reused(self):
        outside = self.root / 'outside.ass'
        outside.write_bytes(b'data')
        (self.work / 'linked.ass').symlink_to(outside)
        self.existing('old.part', b'data')
        self.existing('old.jmk', b'data')
        new = self.incoming('3화/new.ass', b'data')
        self.assertEqual(new, self.work / '3화/new.ass')
        linked_dir = self.work / 'linked-dir'
        linked_dir.symlink_to(self.root)
        temporary = self.root / 'external.part'
        temporary.write_bytes(b'data')
        with self.assertRaises(ValueError):
            preserve_download(temporary, linked_dir / 'new.ass')
        self.assertTrue(temporary.exists())
        self.assertEqual(outside.read_bytes(), b'data')

    def test_reused_file_retains_both_original_and_new_source_records(self):
        old = self.existing('1화/maker/01.ass', b'one')
        register_sources(self.item('1'), [old], self.root)
        reused = self.incoming('3화/maker/renamed.ass', b'one')
        register_sources(self.item('3'), [reused], self.root)
        record = load_manifest(self.root)['files'][old.relative_to(self.root).as_posix()]
        self.assertEqual([source['episode'] for source in record['sources']], ['1', '3'])
        self.assertEqual(len(load_manifest(self.root)['files']), 1)

    def test_erulabo_duplicate_succeeds_without_keeping_new_copy(self):
        old = self.existing('1화/maker/old.zip', b'old archive')
        staged = self.root / 'staging.zip'
        staged.write_bytes(b'old archive')
        item = self.item()
        state = {'success': {}, 'erulabo_pending': {item['id']: {'item': item}}}
        state_path = self.root / 'state.json'
        result = finalize_erulabo_download(
            state=state, state_path=state_path, item_id=item['id'], item=item,
            downloaded_file=staged, target=self.root,
        )
        self.assertEqual(result, old)
        self.assertFalse(staged.exists())
        self.assertEqual(state['erulabo_pending'], {})
        self.assertEqual(state['success'][item['id']]['new_files'], [])
        self.assertEqual(files_from_success(state['success'][item['id']]), [])
        self.assertEqual(json.loads(state_path.read_text())['erulabo_pending'], {})

    def test_cross_filesystem_staging_is_supported(self):
        temporary = self.root / 'staging.zip'
        temporary.write_bytes(b'new')
        destination = self.work / 'new.zip'
        with patch.object(Path, 'replace', side_effect=OSError(errno.EXDEV, 'different filesystem')):
            self.assertEqual(preserve_download(temporary, destination), destination)
        self.assertEqual(destination.read_bytes(), b'new')
        self.assertFalse(temporary.exists())

    def test_duplicate_only_success_is_not_reported_as_a_new_download(self):
        state_path = self.root / 'state.json'
        before = self.root / 'before.json'
        output = self.root / 'dashboard.json'
        before.write_text('{}')
        state_path.write_text(json.dumps({'success': {
            'duplicate-only': {'files': ['anissia_subtitles/A/1화/old.ass'], 'new_files': []},
            'new': {'files': ['anissia_subtitles/A/1화/old.ass', 'anissia_subtitles/A/3화/new.ass'],
                    'new_files': ['anissia_subtitles/A/3화/new.ass']},
        }}))
        with patch('sys.argv', [
            'write_weekly_dashboard.py', '--state', str(state_path), '--before-state', str(before),
            '--failure-log', str(self.root / 'missing.jsonl'), '--output', str(output),
            '--started-at', '2026-10-27T04:00:00+09:00',
            '--finished-at', '2026-10-27T04:10:00+09:00', '--exit-code', '0',
        ]):
            dashboard.main()
        run = json.loads(output.read_text())['runs'][0]
        self.assertEqual(run['status'], 'success')
        self.assertEqual(run['downloadedItems'], 1)
        self.assertEqual(run['downloadedFiles'], 1)
        self.assertEqual(run['failedItems'], 0)
        self.assertEqual(run['downloads'][0]['id'], 'new')

    def test_regular_download_state_keeps_canonical_references_and_counts_only_new(self):
        old = self.existing('1화/maker/01.ass', b'episode one')
        item = self.item()
        state_path = self.root / 'state.json'
        state = queues.load_state(state_path)

        def download(*_):
            return [self.incoming('3화/maker/renamed.ass', b'episode one'),
                    self.incoming('3화/maker/new 03.ass', b'episode three')]

        with patch.object(queues, 'download_anissia', side_effect=download), \
             patch.object(queues, 'maybe_backfill_history'):
            self.assertTrue(queues.process_item(None, state, state_path, item, self.root))
        row = state['success'][item['id']]
        self.assertIn(old.relative_to(self.root).as_posix(), row['files'])
        self.assertEqual(row['new_files'], ['anissia_subtitles/A/3화/maker/new 03.ass'])
        self.assertEqual(files_from_success(row), row['new_files'])
        self.assertEqual(len(load_manifest(self.root)['files']), 2)
        self.assertEqual(list((self.work / '3화/maker').iterdir()), [self.work / '3화/maker/new 03.ass'])

    def test_drive_folder_cache_can_reuse_a_canonical_file_in_an_episode_subfolder(self):
        old = self.existing('1화/maker/01.ass', b'one')
        folder_id = '1' * 25
        file_id = '2' * 25
        item = {**self.item(), 'source_url': f'https://drive.google.com/drive/folders/{folder_id}'}
        state = {'drive_folders': {item['id']: {'folder_id': folder_id, 'files': {file_id: {
            'name': 'renamed.ass', 'path': old.relative_to(self.root).as_posix(),
            'sha256': hashlib.sha256(b'one').hexdigest(),
        }}}}}
        with patch.object(queues, 'list_google_drive_folder', return_value=[(file_id, 'renamed.ass')]), \
             patch.object(queues, 'download_google_drive') as download:
            self.assertEqual(queues.download_anissia_drive_folder(
                None, item, self.root, state, self.root / 'state.json',
            ), [old])
        download.assert_not_called()


if __name__ == '__main__':
    unittest.main()
