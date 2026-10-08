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
            self.assertIn('sort=latest-added', url)
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
        new = self.crawl(lambda url: page_html(['new'], 1), full=True)
        self.assertNotEqual(old['run_id'], new['run_id'])
        self.assertEqual(new['records_stored'], 2)
        self.assertEqual(new['records_in_run'], 1)
        output = Path(self.temp.name) / 'output'
        report = crawler.export(self.db, output)
        self.assertTrue(report['complete'])
        self.assertEqual(json.loads((output/'anikoto-catalog.jsonl').read_text())['source_record_id'], 'new')
        self.assertEqual(json.loads((output/'anikoto-crawl-report.json').read_text())['work_links_created'], 0)

    def test_incremental_stops_after_two_known_pages_and_exports_unvisited_baseline(self):
        old = self.crawl(lambda url: page_html(['one','two','unvisited'], 1))
        with sqlite3.connect(self.db) as db:
            unvisited_before = db.execute("SELECT * FROM anikoto_catalog WHERE source_record_id='unvisited'").fetchone()
        fetched = []
        def fetch(url):
            page = int(crawler.parse_qs(crawler.urlparse(url).query)['page'][0])
            fetched.append(page)
            return page_html({1:['new'], 2:['one'], 3:['two']}[page], 10)
        new = self.crawl(fetch)
        self.assertEqual(fetched, [1,2,3])
        self.assertEqual((new['scan_mode'], new['stop_reason'], new['known_page_streak']), ('incremental','known_pages',2))
        self.assertEqual((new['records_in_run'],new['catalog_records']), (3,4))
        self.assertTrue(new['complete'])
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT * FROM anikoto_catalog WHERE source_record_id='unvisited'").fetchone(), unvisited_before)
        output = Path(self.temp.name) / 'incremental-export'
        crawler.export(self.db, output)
        self.assertEqual({json.loads(line)['source_record_id'] for line in (output/'anikoto-catalog.jsonl').read_text().splitlines()},
                         {'new','one','two','unvisited'})

    def test_new_id_in_mixed_page_resets_incremental_streak(self):
        self.crawl(lambda url: page_html(['a','b','c','d','unvisited'], 1))
        pages = {1:['a'],2:['new','b'],3:['c'],4:['d']}
        fetched = []
        def fetch(url):
            page = int(crawler.parse_qs(crawler.urlparse(url).query)['page'][0])
            fetched.append(page)
            return page_html(pages[page], 8)
        summary = self.crawl(fetch)
        self.assertEqual(fetched,[1,2,3,4])
        self.assertEqual(summary['catalog_records'],6)
        self.assertEqual(summary['stop_reason'],'known_pages')

    def test_incremental_resume_retains_one_known_page_streak(self):
        self.crawl(lambda url: page_html(['one','two','old'], 1))
        fetched = []
        def fetch(url):
            page = int(crawler.parse_qs(crawler.urlparse(url).query)['page'][0])
            fetched.append(page)
            return page_html({1:['new'],2:['one'],3:['two']}[page], 9)
        paused = self.crawl(fetch,max_pages=2)
        self.assertEqual((paused['status'],paused['known_page_streak']),('paused',1))
        completed = self.crawl(fetch)
        self.assertEqual(fetched,[1,2,3])
        self.assertEqual(completed['run_id'],paused['run_id'])
        self.assertEqual(completed['stop_reason'],'known_pages')

    def test_repeated_known_page_does_not_trigger_successful_incremental_stop(self):
        self.crawl(lambda url: page_html(['one','two'], 1))
        with self.assertRaisesRegex(ValueError,'repeated'):
            self.crawl(lambda url: page_html(['one'], 9))
        with sqlite3.connect(self.db) as db:
            run = db.execute('SELECT run_id,status FROM anikoto_crawl_runs ORDER BY rowid DESC LIMIT 1').fetchone()
            self.assertEqual(run[1],'failed')
            self.assertEqual(crawler.crawl_policy(db,run[0])['known_page_streak'],1)

    def test_incremental_accepts_changed_total_pages_when_front_ids_do_not_overlap(self):
        self.crawl(lambda url:page_html(['one','two','tail'],1))
        pages=iter([page_html(['one'],299),page_html(['two'],300)])
        summary=self.crawl(lambda url:next(pages))
        self.assertEqual((summary['pages_collected'],summary['stop_reason']),(2,'known_pages'))
        self.assertEqual(summary['catalog_records'],3)

    def test_incremental_partial_overlap_keeps_original_known_ids_across_resume(self):
        self.crawl(lambda url:page_html(['one','two','three','four','tail'],1))
        pages={1:['new','one'],2:['new','two'],3:['three'],4:['four']}
        fetched=[]
        def fetch(url):
            page=int(crawler.parse_qs(crawler.urlparse(url).query)['page'][0])
            fetched.append(page)
            return page_html(pages[page],9)
        paused=self.crawl(fetch,max_pages=1)
        completed=self.crawl(fetch)
        self.assertEqual(fetched,[1,2,3,4])
        self.assertEqual(completed['run_id'],paused['run_id'])
        self.assertEqual((completed['catalog_records'],completed['stop_reason']),(6,'known_pages'))

    def test_bootstrap_never_stops_early_using_partial_saved_rows(self):
        self.crawl(lambda url: page_html(['one','two'], 4),max_pages=1)
        fetched=[]
        def fetch(url):
            page=int(crawler.parse_qs(crawler.urlparse(url).query)['page'][0])
            fetched.append(page)
            return page_html([f'work-{page}'],4)
        summary=self.crawl(fetch,fresh=True)
        self.assertEqual(fetched,[1,2,3,4])
        self.assertEqual(summary['scan_mode'],'full')
        self.assertEqual(summary['catalog_records'],4)

    def test_first_two_known_pages_stop_without_needing_a_new_item(self):
        self.crawl(lambda url: page_html(['one','two','tail'], 1))
        fetched=[]
        def fetch(url):
            page=int(crawler.parse_qs(crawler.urlparse(url).query)['page'][0])
            fetched.append(page)
            return page_html(['one' if page==1 else 'two'],99)
        summary=self.crawl(fetch)
        self.assertEqual(fetched,[1,2])
        self.assertEqual(summary['catalog_records'],3)

    def test_legacy_complete_catalog_seeds_incremental_membership_without_schema_rewrite(self):
        old=self.crawl(lambda url: page_html(['one','two','tail'],1))
        with sqlite3.connect(self.db) as db:
            db.execute('DROP TABLE anikoto_catalog_members')
            db.execute('DROP TABLE anikoto_crawl_policy')
            db.execute("UPDATE anikoto_crawl_runs SET sort='name-az'")
        pages=iter([page_html(['one'],5),page_html(['two'],5)])
        summary=self.crawl(lambda url:next(pages))
        self.assertEqual(summary['scan_mode'],'incremental')
        self.assertEqual(summary['catalog_records'],3)

    def test_full_option_ignores_known_checkpoint_and_excludes_absent_snapshot_members(self):
        self.crawl(lambda url:page_html(['one','two','tail'],1))
        fetched=[]
        def fetch(url):
            page=int(crawler.parse_qs(crawler.urlparse(url).query)['page'][0])
            fetched.append(page)
            return page_html({1:['one'],2:['two'],3:['new']}[page],3)
        summary=self.crawl(fetch,full=True)
        self.assertEqual(fetched,[1,2,3])
        self.assertEqual((summary['scan_mode'],summary['stop_reason']),('full','last_page'))
        self.assertEqual((summary['catalog_records'],summary['records_stored']),(3,4))


if __name__ == '__main__':
    unittest.main()

