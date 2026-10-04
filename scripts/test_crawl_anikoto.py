import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import crawl_anikoto as crawler

BASE = "https://anikoto.example"


def page_html(slugs, last=3):
    cards = "".join(f'''<div class="item"><div class="poster tip" data-tip="{100+i}">
        <a href="/watch/{slug}/ep-1"><img data-src="/cover.jpg"></a>
        <div class="meta"><span class="ep-status sub">12</span><span class="ep-status dub">?</span>
        <span class="ep-status total">12</span><div class="right">TV</div></div></div>
        <div class="info"><a class="name" data-jp="Hidamari Sketch" href="/watch/{slug}/ep-1">Hidamari Sketch {slug}</a>
        <div class="genre"><a href="/genre/slice-of-life">Slice of Life</a></div>
        <div class="rated"><span>8.5</span></div></div></div>''' for i, slug in enumerate(slugs))
    return f'<html><div id="list-items">{cards}</div><ul class="pagination"><li><a href="/filter?page=1">1</a></li><li><a href="/filter?page={last}">Last</a></li></ul></html>'


class ListingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "anikoto.sqlite"

    def crawl(self, fetch, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return crawler.crawl(self.db, BASE, fetch=fetch, **kwargs)

    def test_metadata_is_literal_and_year_and_work_links_are_not_invented(self):
        rows, total = crawler.parse_listing(page_html(['s1-2007']), BASE, 1)
        row = rows[0]
        self.assertEqual(total, 3)
        self.assertEqual(row['source_record_id'], 's1-2007')
        self.assertEqual(row['title_romaji'], 'Hidamari Sketch')
        self.assertEqual(row['site_id'], '100')
        self.assertEqual(row['format'], 'TV')
        self.assertEqual(row['genres'], ['Slice of Life'])
        self.assertEqual(row['score'], 8.5)
        self.assertEqual(row['sub_episodes'], 12)
        self.assertIsNone(row['dub_episodes'])
        self.assertIsNone(row['year'])
        self.assertEqual(row['match_status'], 'unmatched')
        self.assertEqual(row['detail_url'], BASE + '/watch/s1-2007')
        self.assertEqual(row['poster_url'], BASE + '/cover.jpg')

    def test_native_title_is_kept_and_encoded_slugs_use_decoded_identity(self):
        source = page_html(['%E3%81%B2%E3%81%A0%E3%81%BE%E3%82%8A']).replace('data-jp="Hidamari Sketch"', 'data-jp="ひだまりスケッチ"')
        row = crawler.parse_listing(source, BASE, 1)[0][0]
        self.assertEqual(row['source_record_id'], 'ひだまり')
        self.assertEqual(row['title_native'], 'ひだまりスケッチ')
        self.assertEqual(row['title_romaji'], '')

    def test_blocked_empty_or_unrecognized_pages_are_errors(self):
        for source in ['<html>Access denied</html>', '<div id="list-items"></div>', page_html(['one']).replace('/watch/', '/broken/')]:
            with self.subTest(source=source[:60]), self.assertRaises(ValueError):
                crawler.parse_listing(source, BASE, 1)

    def test_pause_resume_reuses_run_and_only_requests_the_next_page(self):
        fetched = []
        def fetch(url):
            page = int(crawler.parse_qs(crawler.urlparse(url).query)['page'][0])
            fetched.append(page)
            self.assertIn('sort=name-az', url)
            return page_html([f'work-{page}'])
        first = self.crawl(fetch, max_pages=2)
        self.assertEqual(fetched, [1, 2])
        self.assertEqual(first['status'], 'paused')
        self.assertFalse(first['complete'])
        second = self.crawl(fetch)
        self.assertEqual(fetched, [1, 2, 3])
        self.assertEqual(first['run_id'], second['run_id'])
        self.assertEqual(second['records_in_run'], 3)
        self.assertTrue(second['complete'])
        self.assertEqual(second['sqlite_quick_check'], 'ok')

    def test_failure_commits_prior_pages_and_retry_does_not_skip_the_failed_page(self):
        def failed(url):
            page = int(crawler.parse_qs(crawler.urlparse(url).query)['page'][0])
            if page == 2:
                raise RuntimeError('timeout')
            return page_html(['first'])
        with self.assertRaisesRegex(RuntimeError, 'timeout'):
            self.crawl(failed)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT next_page,status FROM anikoto_crawl_runs').fetchone(), (2, 'failed'))
            self.assertEqual(db.execute('SELECT count(*) FROM anikoto_catalog').fetchone()[0], 1)
        fetched = []
        def resume(url):
            page = int(crawler.parse_qs(crawler.urlparse(url).query)['page'][0])
            fetched.append(page)
            return page_html([f'work-{page}'])
        self.assertTrue(self.crawl(resume)['complete'])
        self.assertEqual(fetched, [2, 3])

    def test_repeated_or_overlapping_pages_cannot_be_reported_complete(self):
        for second in [page_html(['same']), page_html(['same', 'new'])]:
            with self.subTest(second=second[:80]):
                self.db.unlink(missing_ok=True)
                responses = iter([page_html(['same']), second])
                with self.assertRaisesRegex(ValueError, 'repeated|overlapped'):
                    self.crawl(lambda url: next(responses))
                with sqlite3.connect(self.db) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM anikoto_crawl_pages').fetchone()[0], 1)
                    self.assertEqual(db.execute('SELECT status FROM anikoto_crawl_runs').fetchone()[0], 'failed')

    def test_pagination_drift_fails_without_modifying_the_completed_page(self):
        responses = iter([page_html(['one'], 3), page_html(['two'], 4)])
        with self.assertRaisesRegex(ValueError, 'pagination changed'):
            self.crawl(lambda url: next(responses))
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM anikoto_catalog').fetchone()[0], 1)

    def test_existing_reviewed_tables_are_preserved_byte_for_byte(self):
        with sqlite3.connect(self.db) as db:
            db.execute('CREATE TABLE reviewed_links (work_id TEXT, subtitle_id TEXT, episode INTEGER)')
            db.execute("INSERT INTO reviewed_links VALUES ('AniList:1852','original',1)")
        self.crawl(lambda url: page_html(['one'], 1))
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT * FROM reviewed_links').fetchall(), [('AniList:1852','original',1)])

    def test_new_scan_retains_absent_rows_but_export_only_contains_its_own_observations(self):
        old = self.crawl(lambda url: page_html(['old'], 1))
        new = self.crawl(lambda url: page_html(['new'], 1))
        self.assertNotEqual(old['run_id'], new['run_id'])
        self.assertEqual(new['records_stored'], 2)
        self.assertEqual(new['records_in_run'], 1)
        output = Path(self.temp.name) / 'output'
        report = crawler.export(self.db, output)
        self.assertTrue(report['complete'])
        self.assertEqual(json.loads((output/'anikoto-catalog.jsonl').read_text())['source_record_id'], 'new')
        self.assertEqual(json.loads((output/'anikoto-crawl-report.json').read_text())['work_links_created'], 0)


if __name__ == '__main__':
    unittest.main()
