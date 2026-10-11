#!/usr/bin/env python3
import unittest
from unittest import mock
import contextlib
import gzip
import io
import json
import sqlite3
import tempfile
from pathlib import Path

import update_db
from test_match_new_sources import catalog, add


class FakeResponse:
    def __init__(self, rows, has_next_page):
        self.status_code = 200
        self.headers = {}
        self._payload = {
            "data": {
                "Page": {
                    "pageInfo": {"hasNextPage": has_next_page},
                    "media": rows,
                }
            }
        }

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class AniListIdIncrementTests(unittest.TestCase):
    def test_id_watermark_uses_highest_numeric_id(self):
        self.assertEqual(
            update_db.anilist_id_watermark({"100", "250", "not-an-id", "42"}),
            250,
        )

    def test_id_watermark_rejects_missing_numeric_ids(self):
        with self.assertRaisesRegex(ValueError, "no numeric source IDs"):
            update_db.anilist_id_watermark({"", "not-an-id"})

    @mock.patch.object(update_db.time, "sleep", return_value=None)
    def test_rows_stop_when_stored_id_is_reached(self, _sleep):
        response = FakeResponse(
            [{"id": 105}, {"id": 104}, {"id": 100}, {"id": 99}],
            has_next_page=True,
        )
        with mock.patch.object(update_db.requests, "post", return_value=response) as post:
            rows = list(update_db.anilist_rows(100))

        self.assertEqual([row["id"] for row in rows], [105, 104])
        self.assertEqual(post.call_count, 1)
        payload = post.call_args.kwargs["json"]
        self.assertIn("sort: [ID_DESC]", payload["query"])

    @mock.patch.object(update_db.time, "sleep", return_value=None)
    def test_rows_page_until_stored_id_is_reached(self, _sleep):
        responses = [
            FakeResponse([{"id": 105}, {"id": 104}], has_next_page=True),
            FakeResponse([{"id": 103}, {"id": 100}], has_next_page=True),
        ]
        with mock.patch.object(update_db.requests, "post", side_effect=responses) as post:
            rows = list(update_db.anilist_rows(100))

        self.assertEqual([row["id"] for row in rows], [105, 104, 103])
        self.assertEqual(post.call_count, 2)

    @mock.patch.object(update_db.time, "sleep", return_value=None)
    def test_rows_fail_closed_if_checkpoint_is_not_found(self, _sleep):
        responses = [
            FakeResponse([{"id": 105}], has_next_page=True),
            FakeResponse([{"id": 104}], has_next_page=True),
        ]
        with mock.patch.object(update_db, "ANILIST_MAX_PAGES", 2),              mock.patch.object(update_db.requests, "post", side_effect=responses):
            with self.assertRaisesRegex(ValueError, "ID scan exceeded 2 pages"):
                list(update_db.anilist_rows(100))


class ListingRefreshTests(unittest.TestCase):
    @mock.patch.object(update_db.time, 'sleep', return_value=None)
    def test_anilist_full_selected_batch_with_next_flag_stops_at_requested_ids(self, _sleep):
        ids = list(range(1, 51))
        with mock.patch.object(update_db.requests, 'post',
                               return_value=FakeResponse([{'id': n} for n in ids], True)) as post:
            self.assertEqual([r['id'] for r in update_db.anilist_selected_rows(ids)], ids)
        self.assertEqual(post.call_count, 1)

    @mock.patch.object(update_db.time, 'sleep', return_value=None)
    def test_anilist_partial_selected_batch_follows_pages(self, _sleep):
        responses = [FakeResponse([{'id': 1}], True), FakeResponse([{'id': 2}], False)]
        with mock.patch.object(update_db.requests, 'post', side_effect=responses) as post:
            self.assertEqual([r['id'] for r in update_db.anilist_selected_rows([1,2])], [1,2])
        self.assertEqual([c.kwargs['json']['variables']['page'] for c in post.call_args_list], [1,2])
        self.assertEqual([c.kwargs['json']['variables']['ids'] for c in post.call_args_list], [[1,2],[1,2]])

    @mock.patch.object(update_db.time, 'sleep', return_value=None)
    def test_anilist_selected_terminal_page_can_omit_removed_ids(self, _sleep):
        responses = [FakeResponse([{'id': 1}], True), FakeResponse([], False)]
        with mock.patch.object(update_db.requests, 'post', side_effect=responses):
            self.assertEqual([r['id'] for r in update_db.anilist_selected_rows([1,2])], [1])

    @mock.patch.object(update_db.time, 'sleep', return_value=None)
    def test_anilist_selected_pagination_rejects_repeats_no_progress_and_unknown_ids(self, _sleep):
        cases = [([FakeResponse([{'id':1}],True), FakeResponse([{'id':1}],False)], 'repeated IDs'),
                 ([FakeResponse([{'id':1},{'id':1}],False)], 'repeated IDs'),
                 ([FakeResponse([],True)], 'no progress'),
                 ([FakeResponse([{'id':3}],False)], 'unrequested ID')]
        for responses, reason in cases:
            with self.subTest(reason=reason), mock.patch.object(update_db.requests, 'post', side_effect=responses):
                with self.assertRaisesRegex(ValueError, reason):
                    list(update_db.anilist_selected_rows([1,2]))

    def test_anilist_selected_missing_pagination_is_not_silently_terminal(self):
        response = FakeResponse([{'id':1}], False)
        response._payload['data']['Page']['pageInfo'] = {}
        with mock.patch.object(update_db.requests, 'post', return_value=response):
            with self.assertRaisesRegex(ValueError, 'pagination information'):
                list(update_db.anilist_selected_rows([1,2]))

    @mock.patch.object(update_db.time, 'sleep', return_value=None)
    def test_anilist_selected_ids_are_batched_with_independent_page_state(self, _sleep):
        ids = list(range(1, 52))
        responses = [FakeResponse([{'id':n} for n in ids[:50]], True), FakeResponse([{'id':51}], False)]
        with mock.patch.object(update_db.requests, 'post', side_effect=responses) as post:
            self.assertEqual([r['id'] for r in update_db.anilist_selected_rows(ids)], ids)
        self.assertEqual([c.kwargs['json']['variables']['page'] for c in post.call_args_list], [1,1])
        self.assertEqual([len(c.kwargs['json']['variables']['ids']) for c in post.call_args_list], [50,1])

    @mock.patch.object(update_db.time, 'sleep', return_value=None)
    def test_anissia_starts_with_zero_and_stops_after_two_known_pages(self, _sleep):
        responses = [{'data':{'totalPages':3,'totalElements':5,'content':[{'animeNo':5},{'animeNo':4}]}},
                     {'data':{'content':[{'animeNo':3},{'animeNo':2}]}},
                     {'data':{'content':[{'animeNo':1}]}}]
        with mock.patch.object(update_db, 'fetch_json', side_effect=responses) as fetch:
            rows = list(update_db.online_rows('Anissia', {'1','2','3','4','5'}))
        self.assertEqual([r['animeNo'] for r in rows], [5,4,3,2])
        self.assertEqual([c.args[0] for c in fetch.call_args_list],
                         [f'https://api.anissia.net/anime/list/{n}?q=' for n in range(2)])

    @mock.patch.object(update_db.time, 'sleep', return_value=None)
    def test_anissia_new_id_anywhere_in_page_resets_known_streak(self, _sleep):
        pages = [[10,6], [5], [4,9], [3], [2], [1]]
        responses = [{'data': {'totalPages': len(pages), 'totalElements': 8,
                               'content': [{'animeNo': aid} for aid in ids]}} for ids in pages]
        with mock.patch.object(update_db, 'fetch_json', side_effect=responses) as fetch:
            rows = list(update_db.online_rows('Anissia', {str(n) for n in range(1,7)}))
        self.assertEqual([r['animeNo'] for r in rows], [10,6,5,4,9,3,2])
        self.assertEqual(fetch.call_count, 5)

    @mock.patch.object(update_db.time, 'sleep', return_value=None)
    def test_anissia_without_baseline_scans_to_last_page(self, _sleep):
        responses = [{'data': {'totalPages': 3, 'totalElements': 3, 'content': [{'animeNo': n}]}} for n in (3,2,1)]
        with mock.patch.object(update_db, 'fetch_json', side_effect=responses) as fetch:
            self.assertEqual([r['animeNo'] for r in update_db.anissia_rows()], [3,2,1])
        self.assertEqual(fetch.call_count, 3)

    @mock.patch.object(update_db.time, 'sleep', return_value=None)
    def test_anissia_repeated_known_page_cannot_count_as_second_overlap(self, _sleep):
        response = {'data': {'totalPages': 3, 'totalElements': 6, 'content': [{'animeNo': 3}, {'animeNo': 2}]}}
        with mock.patch.object(update_db, 'fetch_json', return_value=response):
            with self.assertRaisesRegex(ValueError, 'repeated IDs'):
                list(update_db.anissia_rows({'3','2'}))

    @mock.patch.object(update_db.time, 'sleep', return_value=None)
    def test_anissia_does_not_depend_on_ascending_or_descending_ids(self, _sleep):
        responses = [{'data':{'totalPages':2,'totalElements':3,'content':[{'animeNo':1},{'animeNo':2}]}},
                     {'data':{'content':[{'animeNo':3}]}}]
        with mock.patch.object(update_db, 'fetch_json', side_effect=responses):
            self.assertEqual([r['animeNo'] for r in update_db.anissia_rows()], [1,2,3])

    @mock.patch.object(update_db.time, 'sleep', return_value=None)
    def test_anissia_incomplete_and_malformed_listings_fail_closed(self, _sleep):
        cases = [
            [{'data':{'totalPages':2,'totalElements':3,'content':[{'animeNo':1}]}}, {'data':{'content':[]}}],
            [{'data':{'totalPages':2,'totalElements':3,'content':[{'animeNo':1}]}}, {'data':{'content':[{'animeNo':1}]}}],
            [{'data':{'totalPages':1,'content':'invalid'}}],
        ]
        for responses in cases:
            with self.subTest(responses=responses), mock.patch.object(update_db, 'fetch_json', side_effect=responses):
                with self.assertRaises(ValueError):
                    list(update_db.anissia_rows())

    @mock.patch.object(update_db.time, 'sleep', return_value=None)
    def test_reanime_known_year_sorted_pages_do_not_hide_old_year_new_id(self, _sleep):
        responses = [{'results':[{'anime_id':f'known-{n}'}]} for n in range(3)]
        responses += [{'results':[{'anime_id':'new-old-year','season_year':1998}]}, {'results':[]}]
        with mock.patch.object(update_db, 'fetch_json', side_effect=responses):
            rows = list(update_db.online_rows('ReAnime', {f'known-{n}' for n in range(3)}))
        self.assertEqual(rows[-1]['anime_id'], 'new-old-year')

    @mock.patch.object(update_db.time, 'sleep', return_value=None)
    def test_reanime_page_limit_and_repeated_page_fail_closed(self, _sleep):
        with mock.patch.object(update_db, 'REANIME_MAX_PAGES', 1), \
                mock.patch.object(update_db, 'fetch_json', return_value={'results':[{'anime_id':'a'}]}):
            with self.assertRaisesRegex(ValueError, 'page limit'):
                list(update_db.online_rows('ReAnime', set()))
        with mock.patch.object(update_db, 'fetch_json', return_value={'results':[{'anime_id':'a'}]}):
            with self.assertRaisesRegex(ValueError, 'no progress'):
                list(update_db.online_rows('ReAnime', set()))

    @mock.patch.object(update_db.time, 'sleep', return_value=None)
    def test_anilist_refreshes_existing_ids_below_watermark(self, _sleep):
        with mock.patch.object(update_db, 'anilist_rows', return_value=iter([{'id':105}])), \
                mock.patch.object(update_db.requests, 'post', return_value=FakeResponse([{'id':42}],False)) as post:
            rows = list(update_db.online_rows('AniList', {'100','42'}, 100, {'42'}))
        self.assertEqual([r['id'] for r in rows], [105,42])
        self.assertEqual(post.call_args.kwargs['json']['variables']['ids'], [42])

    @mock.patch.object(update_db.time, 'sleep', return_value=None)
    def test_anilist_refresh_uses_supported_id_sort_in_both_queries(self, _sleep):
        def api_post(url, **kwargs):
            import re
            # AniList's MediaSort uses ID/ID_DESC, never ID_ASC.
            modes = re.findall(r'sort:\s*\[([A-Z_]+)\]', kwargs['json']['query'])
            if not modes or any(mode not in {'ID', 'ID_DESC'} for mode in modes):
                raise ValueError('Unsupported AniList MediaSort')
            selected = 'id_in:' in kwargs['json']['query']
            return FakeResponse([{'id':42 if selected else 105}], False)
        with mock.patch.object(update_db.requests, 'post', side_effect=api_post):
            rows = list(update_db.online_rows('AniList', {'100','42'}, 100, {'42'}))
        self.assertEqual([r['id'] for r in rows], [105,42])

    def test_anilist_http_400_keeps_graphql_validation_reason(self):
        response = mock.Mock(status_code=400, headers={})
        response.json.return_value = {'errors':[{'message':'Value ID_ASC does not exist in MediaSort'}]}
        response.raise_for_status.side_effect = update_db.requests.HTTPError('400 Bad Request')
        with mock.patch.object(update_db.requests, 'post', return_value=response):
            with self.assertRaisesRegex(ValueError, 'ID_ASC does not exist in MediaSort'):
                list(update_db.anilist_selected_rows({'42'}))
        response.raise_for_status.assert_not_called()

    def test_onnada_does_not_revisit_latest_or_pending_existing_ids(self):
        db = catalog()
        self.addCleanup(db.close)
        add(db, 'Anissia', '1', ko='오래된 작품', pending=True)
        add(db, 'Onnada', '1', ko='오래된작품', year=2000)
        for n in range(2,105):
            add(db, 'Onnada', str(n), ko='다른 작품', year=2000)
        self.assertEqual(update_db.refresh_source_ids(db, 'Onnada'), set())

    def test_anilist_selection_includes_missing_special_reference(self):
        db = catalog()
        self.addCleanup(db.close)
        add(db, 'AniList', '100', year=2000)
        add(db, 'ReAnime', 'special', fmt='SPECIAL', ids=['42'])
        with mock.patch.object(update_db.dt, 'date') as today:
            today.today.return_value.year = 2026
            ids = update_db.refresh_source_ids(db, 'AniList')
        self.assertIn('42', ids)


def onnada_listing(ids, next_page=None):
    cards = ''.join(f'<a href="/anime/{n}"><h4>작품 {n}</h4></a>' for n in ids)
    link = f'<a href="/anime?page={next_page}">next</a>' if next_page else ''
    return f'<p>전체 {len(ids)}개</p>{cards}{link}'


def onnada_detail(country='일본', category='TV', native='検証アニメーション', title='검증 작품'):
    return (f'<h1>{title}</h1><p><span>{native}</span><span>Example Show</span></p>'
            f'<dl><dt>제작국가</dt><dd>{country}</dd><dt>분류</dt><dd>{category}</dd>'
            '<dt>방영일</dt><dd>2026-10</dd><dt>총화수</dt><dd>12</dd></dl>')


class OnnadaNewIdTests(unittest.TestCase):
    @mock.patch.object(update_db.time, 'sleep', return_value=None)
    def test_listing_keeps_scanning_and_only_new_ids_get_details(self, _sleep):
        first = '<p>전체 3개</p><a href="/anime/1"><h4>old</h4></a><a href="/anime?page=2">next</a>'
        second = onnada_listing([2,3])
        observed = []
        def fetch(url):
            if url == 'https://onnada.com/anime': return first
            if url.endswith('?page=2'): return second
            if url.endswith('/3'): return onnada_detail()
            raise AssertionError('Existing ID must not be fetched: '+url)
        with mock.patch.object(update_db,'fetch_html',side_effect=fetch) as get, \
                contextlib.redirect_stderr(io.StringIO()):
            rows = list(update_db.online_rows('Onnada', {'1','2'}, refresh_ids={'1','2'},
                                             onnada_observer=lambda *values: observed.append(values)))
        self.assertEqual([r['onnada_id'] for r in rows], [3])
        self.assertEqual(get.call_count,3)
        self.assertEqual(observed,[(3,'included','japanese_work')])

    @mock.patch.object(update_db.time, 'sleep', return_value=None)
    def test_exclusions_are_recorded_with_reasons_but_bad_details_are_not(self, _sleep):
        details=[onnada_detail(country='중국'),onnada_detail(category='특전'),
                 onnada_detail(country='',native='Unverified Title'),'<html>blocked</html>']
        observed=[]
        with mock.patch.object(update_db,'fetch_html',side_effect=[onnada_listing([1,2,3,4]),*details]), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(ValueError,'detail 4 had no title'):
                list(update_db.online_rows('Onnada',set(),onnada_observer=lambda *v:observed.append(v)))
        self.assertEqual(observed,[(1,'excluded','non_japanese'),(2,'excluded','bonus'),
                                   (3,'excluded','country_unverified')])

    @mock.patch.object(update_db.time, 'sleep', return_value=None)
    def test_incomplete_listing_does_not_record_any_details(self, _sleep):
        observed=[]
        with mock.patch.object(update_db,'fetch_html',return_value='<p>전체 100개</p><a href="/anime/1"><h4>one</h4></a>') as get, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(ValueError,'Incomplete Onnada listing'):
                list(update_db.online_rows('Onnada',set(),onnada_observer=lambda *v:observed.append(v)))
        self.assertEqual(get.call_count,1)
        self.assertEqual(observed,[])

    def make_snapshot(self, root):
        db=catalog()
        add(db,'Onnada','1',ko='기존 작품',year=2000)
        add(db,'Anissia','1',ko='기존 작품',pending=True)
        db.execute('CREATE TABLE year_summary (year INTEGER,source TEXT,record_count INTEGER)')
        db.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT)')
        db.commit()
        with sqlite3.connect(root/'base.db') as disk:
            db.backup(disk)
        db.close()
        snapshot=root/'snapshot.db.gz'
        update_db.pack_snapshot(root/'base.db',snapshot)
        return snapshot

    def run_online(self, snapshot, fetch):
        real_online=update_db.online_rows
        def only_onnada(source,*args,**kwargs):
            return real_online(source,*args,**kwargs) if source=='Onnada' else iter([])
        with mock.patch('sys.argv',['update_db','--snapshot',str(snapshot),'--online']), \
                mock.patch.object(update_db,'anilist_id_watermark',return_value=1), \
                mock.patch.object(update_db,'online_rows',side_effect=only_onnada), \
                mock.patch.object(update_db,'fetch_html',side_effect=fetch) as get, \
                contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            update_db.main()
        return get.call_count

    @mock.patch.object(update_db.time, 'sleep', return_value=None)
    def test_excluded_only_snapshot_is_saved_and_next_run_skips_all_old_ids(self, _sleep):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); snapshot=self.make_snapshot(root)
            before=snapshot.read_bytes()
            def first_fetch(url):
                if url=='https://onnada.com/anime': return onnada_listing([1,2])
                if url.endswith('/2'): return onnada_detail(country='중국')
                raise AssertionError('Old work fetched')
            self.assertEqual(self.run_online(snapshot,first_fetch),2)
            after=snapshot.read_bytes()
            self.assertNotEqual(before,after)
            with gzip.open(snapshot,'rb') as src:
                (root/'after.db').write_bytes(src.read())
            with sqlite3.connect(root/'after.db') as db:
                self.assertEqual(db.execute('SELECT source_record_id,outcome,reason FROM onnada_detail_checks').fetchall(),
                                 [('2','excluded','non_japanese')])
                self.assertEqual(db.execute("SELECT title_ko,year FROM entries WHERE source='Onnada'").fetchall(),
                                 [('기존 작품',2000)])
            def second_fetch(url):
                self.assertEqual(url,'https://onnada.com/anime')
                return onnada_listing([1,2])
            self.assertEqual(self.run_online(snapshot,second_fetch),1)
            self.assertEqual(snapshot.read_bytes(),after)
            def new_fetch(url):
                if url=='https://onnada.com/anime':return onnada_listing([1,2,3])
                if url.endswith('/3'):return onnada_detail(title='새 작품')
                raise AssertionError('Old ID fetched again')
            self.assertEqual(self.run_online(snapshot,new_fetch),2)
            with gzip.open(snapshot,'rb') as src:
                (root/'final.db').write_bytes(src.read())
            with sqlite3.connect(root/'final.db') as db:
                self.assertEqual(db.execute('SELECT source_record_id,outcome FROM onnada_detail_checks ORDER BY source_record_id').fetchall(),
                                 [('2','excluded'),('3','included')])
                self.assertEqual(db.execute("SELECT source_record_id FROM entries WHERE source='Onnada' ORDER BY source_record_id").fetchall(),
                                 [('1',),('3',)])

    @mock.patch.object(update_db.time, 'sleep', return_value=None)
    def test_failure_after_an_exclusion_does_not_publish_the_check(self, _sleep):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); snapshot=self.make_snapshot(root); before=snapshot.read_bytes()
            def fetch(url):
                if url=='https://onnada.com/anime':return onnada_listing([1,2,3])
                if url.endswith('/2'):return onnada_detail(country='중국')
                if url.endswith('/3'):raise RuntimeError('Detail request failed')
                raise AssertionError('Old ID fetched')
            with self.assertRaisesRegex(RuntimeError,'Detail request failed'):
                self.run_online(snapshot,fetch)
            self.assertEqual(snapshot.read_bytes(),before)


class SnapshotSaveTests(unittest.TestCase):
    def test_candidate_only_change_packs_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = catalog()
            payload = {'animeNo':1,'subject':'검증 작품','originalSubject':'検証アニメーション',
                       'startDate':'2026-10-99','status':'','genres':''}
            rid, values, cross, metadata = update_db.normalize('Anissia', payload)
            db.execute('INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('Anissia',rid,*values,json.dumps(cross),None,json.dumps(metadata)))
            db.execute("INSERT INTO monthly_match_pending VALUES ('Anissia','1')")
            add(db, 'ReAnime', 'target', year=None)
            db.execute('CREATE TABLE monthly_review (source TEXT,source_record_id TEXT,reason TEXT,observed_at TEXT,PRIMARY KEY(source,source_record_id,reason))')
            db.execute('CREATE TABLE year_summary (year INTEGER,source TEXT,record_count INTEGER)')
            db.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT)')
            db.commit()
            disk = sqlite3.connect(root/'base.db')
            db.backup(disk); disk.close(); db.close()
            snapshot = root/'base.db.gz'
            update_db.pack_snapshot(root/'base.db', snapshot)
            before = snapshot.read_bytes()
            input_path = root/'rows.jsonl'
            input_path.write_text(json.dumps(payload)+'\n')
            args = ['update_db','--snapshot',str(snapshot),'--input','Anissia='+str(input_path)]
            with mock.patch('sys.argv', args), contextlib.redirect_stdout(io.StringIO()) as stdout:
                update_db.main()
            result = json.loads(stdout.getvalue())
            self.assertEqual(result['sources']['Anissia'], {'scanned':1,'added':0,'modified':0})
            self.assertTrue(result['new_work_matching']['changed'])
            self.assertNotEqual(snapshot.read_bytes(), before)
            with gzip.open(snapshot,'rb') as src:
                (root/'after.db').write_bytes(src.read())
            check = sqlite3.connect(root/'after.db')
            self.addCleanup(check.close)
            self.assertEqual(check.execute('SELECT reason FROM monthly_match_candidates').fetchone()[0], 'year_missing')

    def test_reviewed_cross_source_id_cannot_be_extended_by_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = catalog()
            add(db, 'ReAnime', 'approved', year=2025,
                meta={'reviewed_matching_20261011': {
                    'decision': 'correct_work', 'new_episode_assignment': False}})
            approved = {'anilist_ids': ['approved-anilist'], 'reanime_ids': ['approved']}
            db.execute("UPDATE entries SET cross_source_ids_json=?,existing_match_status='strong_exact' "
                       "WHERE source='ReAnime' AND source_record_id='approved'",
                       (json.dumps(approved),))
            db.execute('CREATE TABLE year_summary (year INTEGER,source TEXT,record_count INTEGER)')
            db.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT)')
            db.commit()
            source_db = root/'source.db'
            with sqlite3.connect(source_db) as disk:
                db.backup(disk)
            db.close()
            snapshot = root/'snapshot.db.gz'
            update_db.pack_snapshot(source_db, snapshot)
            input_path = root/'incoming.jsonl'
            input_path.write_text(json.dumps({
                'reanime_id': 'approved', 'anilist_id': 'wrong-movie',
                'title_japanese': '検証アニメーション',
                'season_year': 2025, 'format': 'TV'}, ensure_ascii=False) + '\n')
            with mock.patch('sys.argv', ['update_db','--snapshot',str(snapshot),
                                         '--input', 'ReAnime='+str(input_path)]), \
                 contextlib.redirect_stdout(io.StringIO()):
                update_db.main()
            with gzip.open(snapshot, 'rb') as stream:
                (root/'output.db').write_bytes(stream.read())
            with sqlite3.connect(root/'output.db') as result:
                cross, status = result.execute(
                    "SELECT cross_source_ids_json,existing_match_status FROM entries "
                    "WHERE source='ReAnime' AND source_record_id='approved'").fetchone()
                self.assertEqual(json.loads(cross), approved)
                self.assertEqual(status, 'strong_exact')

    def test_incomplete_online_listing_does_not_replace_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'snapshot.db.gz'
            db = catalog()
            db.execute('CREATE TABLE year_summary (year INTEGER,source TEXT,record_count INTEGER)')
            db.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT)')
            db.commit()
            disk = sqlite3.connect(Path(temp)/'base.db'); db.backup(disk); disk.close(); db.close()
            update_db.pack_snapshot(Path(temp)/'base.db', path)
            before = path.read_bytes()
            def incomplete(*args):
                yield {'animeNo':1,'subject':'신작','originalSubject':'新作アニメ','startDate':'2026-10-99'}
                raise ValueError('Incomplete Anissia listing')
            with mock.patch('sys.argv',['update_db','--snapshot',str(path),'--online']), \
                    mock.patch.object(update_db,'anilist_id_watermark',return_value=1), \
                    mock.patch.object(update_db,'online_rows',side_effect=lambda source,*args: incomplete() if source=='Anissia' else iter([])), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, 'Incomplete Anissia'):
                    update_db.main()
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()

