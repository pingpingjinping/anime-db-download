"""New Anissia archives unpack; older ZIPs only supply read-only hash matches."""
import contextlib
import io
import json
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

import anissia_zip_storage as unpacker
import pi_download_subtitle_queues as queues
from pi_download_erulabo import finalize_erulabo_download
from subtitle_work_storage import load_manifest, preserve_download, track_download_writes


def zip_bytes(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, data in members:
            archive.writestr(name, data)
    return buffer.getvalue()


class AnissiaZipTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.work = self.root / 'anissia_subtitles' / 'A'
        self.destination = self.work / '3화' / 'maker'
        self.destination.mkdir(parents=True)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def item(self):
        return {'id': 'anissia:test-zip', 'source': 'anissia', 'anime_no': '123',
                'anime_title': 'A', 'episode': '3', 'creator': 'maker',
                'source_url': 'https://example.test/3', 'handler': 'tistory'}

    def old_file(self, name, data):
        path = self.work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def incoming(self, name, data):
        temporary = self.root / 'download.part'
        temporary.write_bytes(data)
        return preserve_download(temporary, self.destination / name)

    def prepare_zip(self, members, name='1-3.zip'):
        with track_download_writes() as writes:
            archive = self.incoming(name, zip_bytes(members))
            files = unpacker.prepare_anissia_files([archive], writes)
        return archive, files, writes

    def test_new_zip_keeps_only_new_episode_and_preserves_fonts_and_inner_folders(self):
        one = self.old_file('1화/maker/old.ass', b'one')
        two = self.old_file('2화/maker/old.smi', b'two')
        archive, files, writes = self.prepare_zip([
            ('Season 1/renamed 01.smi', b'one'), ('Season 1/renamed 02.ass', b'two'),
            ('Season 1/03.ass', b'three'), ('fonts/Korean.ttf', b'font'), ('readme.txt', b'notes'),
        ])
        self.assertFalse(archive.exists())
        self.assertIn(one, files)
        self.assertIn(two, files)
        new = self.destination / '1-3/Season 1/03.ass'
        self.assertEqual(new.read_bytes(), b'three')
        self.assertEqual((self.destination / '1-3/fonts/Korean.ttf').read_bytes(), b'font')
        self.assertEqual((self.destination / '1-3/readme.txt').read_bytes(), b'notes')
        self.assertEqual(set(writes.created), set(files) - {one, two})
        self.assertEqual(list(self.destination.rglob('*.ass')), [new])

    def test_older_zip_members_are_hash_compared_without_unpacking_or_modifying_old_zip(self):
        old = self.old_file('1화/old.zip', zip_bytes([('old-name.ass', b'one'), ('02.smi', b'two')]))
        original = old.read_bytes()
        before = old.stat()
        archive, files, writes = self.prepare_zip([
            ('different 01.srt', b'one'), ('different 02.ass', b'two'), ('03.ass', b'three'),
        ])
        self.assertEqual(old.read_bytes(), original)
        self.assertEqual(old.stat().st_ino, before.st_ino)
        self.assertEqual(old.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertFalse(archive.exists())
        self.assertEqual(set(files), {old, self.destination / '1-3/03.ass'})
        self.assertEqual(writes.reused, {old})
        self.assertFalse((self.work / '1화/old').exists())

    def test_new_loose_file_can_reuse_a_member_of_an_old_zip(self):
        old = self.old_file('1화/old.zip', zip_bytes([('01.ass', b'one')]))
        with track_download_writes() as writes:
            new = self.incoming('renamed.smi', b'one')
            files = unpacker.prepare_anissia_files([new], writes)
        self.assertEqual(files, [old])
        self.assertFalse(new.exists())
        self.assertEqual(writes.created, set())

    def test_different_bytes_at_same_member_name_are_both_preserved(self):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            archive, files, writes = self.prepare_zip([('03.ass', b'version one'), ('03.ass', b'version two')])
        self.assertFalse(archive.exists())
        self.assertEqual(len(files), 2)
        self.assertEqual({path.read_bytes() for path in files}, {b'version one', b'version two'})
        self.assertEqual(set(files), writes.created)

    def test_identical_members_with_different_names_keep_only_one_new_file(self):
        archive, files, writes = self.prepare_zip([('03.ass', b'three'), ('other.smi', b'three')])
        self.assertFalse(archive.exists())
        self.assertEqual(len(files), 1)
        self.assertEqual(writes.created, set(files))

    def test_identical_existing_raw_zip_is_reused_and_never_converted(self):
        payload = zip_bytes([('01.ass', b'one')])
        old = self.old_file('1화/old.zip', payload)
        with track_download_writes() as writes:
            reused = self.incoming('renamed.zip', payload)
            self.assertEqual(unpacker.prepare_anissia_files([reused], writes), [old])
        self.assertTrue(old.exists())
        self.assertEqual(writes.created, set())

    def test_aniall_zip_stays_unchanged(self):
        directory = self.root / 'aniall_subtitles/A'
        directory.mkdir(parents=True)
        temporary = self.root / 'new.part'
        payload = zip_bytes([('01.ass', b'one')])
        temporary.write_bytes(payload)
        with track_download_writes() as writes:
            archive = preserve_download(temporary, directory / 'all.zip')
            files = unpacker.prepare_anissia_files([archive], writes)
        self.assertEqual(files, [archive])
        self.assertEqual(archive.read_bytes(), payload)
        self.assertEqual(list(directory.rglob('*.ass')), [])

    def test_nested_font_zip_is_unpacked_with_its_folder_structure(self):
        archive, files, _ = self.prepare_zip([
            ('03.ass', b'three'), ('fonts.zip', zip_bytes([('nested/font.otf', b'font')])),
        ])
        self.assertFalse(archive.exists())
        self.assertIn(self.destination / '1-3/fonts/nested/font.otf', files)
        self.assertEqual(list(self.destination.rglob('*.zip')), [])

    def test_invalid_zip_retains_raw_download(self):
        with track_download_writes() as writes:
            archive = self.incoming('bad.zip', b'not a zip')
            self.assertEqual(unpacker.prepare_anissia_files([archive], writes), [archive])
        self.assertEqual(archive.read_bytes(), b'not a zip')
        self.assertEqual(writes.created, {archive})

    def test_all_crc_checks_finish_before_saving_any_extracted_file(self):
        payload = zip_bytes([('03.ass', b'payload-three'), ('font.ttf', b'payload-font')])
        payload = payload.replace(b'payload-font', b'badload-font', 1)
        with track_download_writes() as writes:
            archive = self.incoming('bad-crc.zip', payload)
            self.assertEqual(unpacker.prepare_anissia_files([archive], writes), [archive])
        self.assertEqual(list(self.destination.rglob('*.ass')), [])
        self.assertEqual(archive.read_bytes(), payload)

    def test_path_traversal_and_symlink_entries_keep_archive_without_writing_outputs(self):
        link = zipfile.ZipInfo('link.ass')
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        for bad_name in ('../../escaped.ass', '/absolute.ass', 'C:\\escaped.ass', link):
            with self.subTest(name=bad_name):
                archive, files, _ = self.prepare_zip([('03.ass', b'three'), (bad_name, b'bad')], name='bad.zip')
                self.assertEqual(files, [archive])
                self.assertTrue(archive.exists())
                self.assertEqual(list(self.destination.rglob('*.ass')), [])
                archive.unlink()
        self.assertFalse((self.root / 'escaped.ass').exists())

    def test_symlinked_output_directory_does_not_change_external_files(self):
        external = self.root / 'outside'
        external.mkdir()
        (self.destination / '1-3').symlink_to(external)
        archive, files, _ = self.prepare_zip([('03.ass', b'three')])
        self.assertEqual(files, [archive])
        self.assertEqual(list(external.iterdir()), [])

    def test_commit_failure_rolls_back_only_new_outputs_and_keeps_original_zip(self):
        old = self.old_file('1화/01.ass', b'one')
        real_save = unpacker.preserve_download
        def fail(temporary, destination):
            if destination.name == 'font.ttf':
                raise OSError('disk full')
            return real_save(temporary, destination)
        with patch.object(unpacker, 'preserve_download', side_effect=fail):
            archive, files, writes = self.prepare_zip([
                ('01.ass', b'one'), ('03.ass', b'three'), ('font.ttf', b'font'),
            ])
        self.assertEqual(files, [archive])
        self.assertTrue(archive.exists())
        self.assertEqual(old.read_bytes(), b'one')
        self.assertEqual(list(self.destination.rglob('*.ass')), [])
        self.assertEqual(writes.created, {archive})

    def test_expansion_limit_retains_archive(self):
        with patch.object(unpacker, 'MAX_EXPANDED_BYTES', 3):
            archive, files, _ = self.prepare_zip([('03.ass', b'three')])
        self.assertEqual(files, [archive])
        self.assertTrue(archive.exists())

    def test_cp949_member_names_are_preserved(self):
        original = '자막/03화.smi'
        info = zipfile.ZipInfo(original.encode('cp949').decode('cp437'))
        self.assertEqual(unpacker.member_path(info), Path(original))

    def test_erulabo_success_records_extracted_files_not_the_deleted_zip(self):
        staged = self.root / 'staged.zip'
        staged.write_bytes(zip_bytes([('03.ass', b'three'), ('font.ttf', b'font')]))
        item = self.item()
        state = {'success': {}, 'erulabo_pending': {item['id']: {'item': item}}}
        state_path = self.root / 'state.json'
        kept = finalize_erulabo_download(
            state=state, state_path=state_path, item_id=item['id'], item=item,
            downloaded_file=staged, target=self.root,
        )
        row = state['success'][item['id']]
        self.assertTrue(kept.is_file())
        self.assertEqual(len(row['files']), 2)
        self.assertEqual(len(row['new_files']), 2)
        self.assertTrue(all((self.root / path).is_file() for path in row['new_files']))
        self.assertEqual(list(self.work.rglob('*.zip')), [])
        self.assertEqual(len(load_manifest(self.root)['files']), 2)
        self.assertEqual(json.loads(state_path.read_text())['erulabo_pending'], {})

    def test_drive_zip_cache_tracks_all_unpacked_outputs_without_redownloading(self):
        folder_id = '1' * 25
        file_id = '2' * 25
        item = {**self.item(), 'source_url': f'https://drive.google.com/drive/folders/{folder_id}'}
        state_path = self.root / 'state.json'
        state = queues.load_state(state_path)
        payload = zip_bytes([('01.ass', b'one'), ('02.ass', b'two')])
        def download(session, url, destination, suggested_name=''):
            temporary = self.root / 'drive.part'
            temporary.write_bytes(payload)
            return preserve_download(temporary, destination / suggested_name)
        with patch.object(queues, 'list_google_drive_folder', return_value=[(file_id, 'set.zip')]), \
             patch.object(queues, 'download_google_drive', side_effect=download) as transfer:
            first = queues.download_anissia_drive_folder(None, item, self.root, state, state_path)
            second = queues.download_anissia_drive_folder(None, item, self.root, state, state_path)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 2)
        transfer.assert_called_once()
        record = state['drive_folders'][item['id']]['files'][file_id]
        self.assertEqual(len(record['files']), 2)
        self.assertTrue(all((self.root / row['path']).exists() for row in record['files']))
        self.assertEqual(list(self.work.rglob('*.zip')), [])

    def test_regular_run_records_old_zip_reference_and_only_new_extracted_episode(self):
        old = self.old_file('1화/old.zip', zip_bytes([('01.ass', b'one'), ('02.smi', b'two')]))
        state_path = self.root / 'state.json'
        state = queues.load_state(state_path)
        item = self.item()
        def download(*_):
            return [self.incoming('new.zip', zip_bytes([
                ('renamed 01.smi', b'one'), ('renamed 02.ass', b'two'), ('03.ass', b'three'),
            ]))]
        with patch.object(queues, 'download_anissia', side_effect=download), \
             patch.object(queues, 'maybe_backfill_history'):
            self.assertTrue(queues.process_item(None, state, state_path, item, self.root))
        row = state['success'][item['id']]
        self.assertEqual(row['files'], [old.relative_to(self.root).as_posix(),
                                        'anissia_subtitles/A/3화/maker/new/03.ass'])
        self.assertEqual(row['new_files'], ['anissia_subtitles/A/3화/maker/new/03.ass'])
        self.assertTrue(all((self.root / path).is_file() for path in row['files']))
        self.assertEqual(row['verification']['status'], 'verified')


if __name__ == '__main__':
    unittest.main()
