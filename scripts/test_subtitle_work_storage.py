import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from subtitle_work_storage import work_directory, register_sources, preserve_download, load_manifest


class WorkStorageTests(unittest.TestCase):
    def item(self, source='anissia', identity='100', title='작품 2기'):
        return {'source': source, 'anime_no': identity, 'post_id': identity, 'anime_title': title, 'title': title,
                'source_url': f'https://aniall.tistory.com/{identity}' if source=='aniall' else 'https://example.test/ep2', 'episode': '2'}

    def test_reuses_existing_title_folder_and_keeps_seasons_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for source in ('anissia','aniall'):
                existing=root / f'{source}_subtitles' / '작품 2기';existing.mkdir(parents=True)
                self.assertEqual(work_directory(self.item(source),root),existing)
                self.assertEqual(work_directory(self.item(source,title='작품 1기'),root).name,'작품 1기')
                self.assertFalse(any(p.name.startswith('100__') for p in existing.parent.iterdir()))

    def test_same_source_keeps_its_folder_after_title_change_and_legacy_numbered_remains_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);item=self.item('aniall')
            directory=work_directory(item,root);directory.mkdir();file=directory/'all.zip';file.write_bytes(b'zip')
            register_sources(item,[file],root)
            self.assertEqual(work_directory(self.item('aniall',title='다른 번역명'),root),directory)
            legacy=root/'anissia_subtitles'/'100__작품 2기';legacy.mkdir(parents=True)
            self.assertEqual(work_directory(self.item(),root),legacy)

    def test_same_title_posts_share_folder_but_keep_exact_file_source_ids_and_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);first=self.item('aniall');second=self.item('aniall','101')
            directory=work_directory(first,root);directory.mkdir()
            self.assertEqual(work_directory(second,root),directory)
            for item,name,data in ((first,'01.zip',b'one'),(second,'02.zip',b'two')):
                file=directory/name;file.write_bytes(data);register_sources(item,[file],root)
            records=load_manifest(root)['files']
            self.assertEqual(records['aniall_subtitles/작품 2기/01.zip']['sources'][0]['id'],'100')
            self.assertEqual(records['aniall_subtitles/작품 2기/02.zip']['sources'][0]['id'],'101')
            self.assertEqual(records['aniall_subtitles/작품 2기/01.zip']['sha256'],hashlib.sha256(b'one').hexdigest())

    def test_identical_attachment_reused_and_different_same_name_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);original=root/'all.zip';original.write_bytes(b'old')
            temporary=root/'download.part';temporary.write_bytes(b'old')
            self.assertEqual(preserve_download(temporary,original),original)
            self.assertFalse(temporary.exists())
            temporary.write_bytes(b'new');variant=preserve_download(temporary,original)
            self.assertNotEqual(variant,original);self.assertEqual(original.read_bytes(),b'old')
            temporary.write_bytes(b'new');self.assertEqual(preserve_download(temporary,original),variant)
            self.assertEqual(variant.read_bytes(),b'new')

    def test_invalid_id_url_missing_title_and_symlink_directory_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for item in (self.item(identity='../1'),self.item('aniall',title=''),{**self.item('aniall'),'source_url':'https://aniall.tistory.com/999'}):
                with self.assertRaises(ValueError):work_directory(item,root)
            corpus=root/'anissia_subtitles';corpus.mkdir(exist_ok=True);(corpus/'작품 2기').symlink_to(root)
            with self.assertRaises(ValueError):work_directory(self.item(),root)

    def test_new_download_registers_incremental_local_name_index_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = self.item('anissia')
            directory = work_directory(item, root)
            directory.mkdir(parents=True)
            archive = directory / '새 자막 02.zip'
            archive.write_bytes(b'local subtitle data')
            register_sources(item, [archive], root)
            journal = root / '.local-subtitle-search-updates.jsonl'
            rows = [json.loads(line) for line in journal.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(rows, [{'path': 'anissia_subtitles/작품 2기/새 자막 02.zip'}])

    def test_concurrent_source_updates_keep_both_files(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);item=self.item('aniall');directory=work_directory(item,root);directory.mkdir()
            files=[]
            for name in ('one.zip','two.zip'):
                file=directory/name;file.write_bytes(name.encode());files.append(file)
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda file:register_sources(item,[file],root),files))
            self.assertEqual(len(load_manifest(root)['files']),2)

    def test_regular_history_and_erulabo_destinations_use_same_work_folder(self):
        from pi_download_subtitle_queues import historical_destination, download_anissia
        from pi_download_erulabo import erulabo_destination
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);item={**self.item(),'creator':'제작자'}
            expected=root/'anissia_subtitles'/'작품 2기'/'2화'/'제작자'
            self.assertEqual(historical_destination(item,root,'2'),expected)
            self.assertEqual(erulabo_destination(item,root),expected)
            with patch('pi_download_subtitle_queues.download_source_page',return_value=[]) as download:
                download_anissia(None,item,root)
                self.assertEqual(download.call_args.args[2],expected)

if __name__=='__main__':unittest.main()
