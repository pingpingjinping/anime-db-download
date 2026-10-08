import copy
import contextlib
import io
import json
import unittest
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import match_anikoto as matcher
from catalog_corrections import corrected_rows


def row(source='AniList', identity='1', title='Example Show', year=2020, format='TV', count=12, **fields):
    return {'source': source, 'source_record_id': identity, 'title_romaji': title,
            'title_native': '', 'title_ko': '', 'title_english': '', 'year': year,
            'format': format, 'status': 'FINISHED', 'cross_source_ids_json': '{}',
            'source_metadata_json': json.dumps({'episodes': count}), **fields}


def listing(identity='example-slug', title='Example Show', format='TV', count=12):
    return {'source': 'Anikoto', 'source_record_id': identity, 'title': title,
            'title_jp_raw': title, 'year': None, 'format': format, 'total_episodes': count}


class MatchingTests(unittest.TestCase):
    def test_incremental_unvisited_work_survives_matching_runtime_and_release(self):
        import crawl_anikoto as crawler
        import package_anikoto_release as packager
        from test_crawl_anikoto import BASE, page_html
        from test_match_new_sources import catalog as baseline_catalog, add
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            baseline,raw,runtime=root/'baseline.db',root/'raw.sqlite',root/'runtime.db'
            db=baseline_catalog()
            add(db,'AniList','tail-target',native='',romaji='Hidamari Sketch unvisited',meta={'episodes':12})
            for source in ('Anissia','ReAnime','Onnada'):
                add(db,source,'other',native='',romaji='Unrelated '+source)
            db.execute('CREATE TABLE protected_episode(file_id TEXT PRIMARY KEY,episode INTEGER)')
            db.execute("INSERT INTO protected_episode VALUES ('original',1)")
            db.commit()
            with sqlite3.connect(baseline) as disk:
                db.backup(disk)
            db.close()
            with contextlib.redirect_stdout(io.StringIO()):
                crawler.crawl(raw,BASE,fetch=lambda url:page_html(['one','two','unvisited'],1))
                pages=iter([page_html(['new'],10),page_html(['one'],10),page_html(['two'],10)])
                summary=crawler.crawl(raw,BASE,fetch=lambda url:next(pages))
                crawler.export(raw,root/'export')
            self.assertEqual((summary['records_in_run'],summary['catalog_records']),(3,4))
            before=(matcher.sha256(baseline),matcher.sha256(raw))
            report=matcher.match(baseline,raw,root/'matching')
            self.assertEqual((report['records'],report['automatic']),(4,1))
            matcher.create_matched_database(baseline,raw,root/'matching/anikoto-matches.json',runtime)
            with sqlite3.connect(runtime) as db:
                self.assertEqual(db.execute('SELECT source_record_id FROM anikoto_catalog').fetchall(),[('unvisited',)])
                self.assertEqual(db.execute('SELECT * FROM protected_episode').fetchall(),[('original',1)])
            packaged=packager.package(baseline,runtime,raw,root/'matching',root/'export',root/'release')
            self.assertEqual(packaged['runtime_works'],1)
            self.assertEqual(before,(matcher.sha256(baseline),matcher.sha256(raw)))

    def test_unique_complete_title_and_metadata_creates_a_link_without_mutating_inputs(self):
        rows, listings = [row()], [listing()]
        before = copy.deepcopy((rows, listings))
        links, outcomes, _ = matcher.compare(listings, rows)
        self.assertEqual((links[0]['target_source'], links[0]['target_id']), ('AniList', '1'))
        self.assertEqual(outcomes[0]['status'], 'automatic')
        self.assertEqual((rows, listings), before)

    def test_remakes_with_same_title_are_ambiguous_without_an_upstream_year(self):
        links, outcomes, _ = matcher.compare([listing()], [row(), row(identity='2', year=2010)])
        self.assertFalse(links)
        self.assertEqual(outcomes[0]['reason'], 'multiple_targets')

    def test_multiple_anikoto_slugs_for_one_target_are_not_arbitrarily_selected(self):
        links, outcomes, _ = matcher.compare([listing('one'), listing('two')], [row()])
        self.assertFalse(links)
        self.assertTrue(all(o['reason'] == 'multiple_anikoto_records' for o in outcomes))

    def test_format_episode_season_and_part_conflicts_prevent_automatic_links(self):
        for source in [row(format='MOVIE'), row(count=13), row(title_english='Example Show Season 2'),
                       row(title_english='Example Show Part 2'), row(year=None), row(count=None), row(status='CANCELLED')]:
            with self.subTest(source=source):
                self.assertFalse(matcher.compare([listing()], [source])[0])

    def test_title_symbols_and_sequel_words_are_not_collapsed(self):
        pairs = [('Hidamari Sketch', 'Hidamari Sketch x ☆☆☆'), ('WORKING!', 'WORKING!!'),
                 ('Saenai Heroine', 'Saenai Heroine ♭'), ('Example', 'Example Recap')]
        for original, sequel in pairs:
            with self.subTest(sequel=sequel):
                self.assertNotEqual(matcher.key(original), matcher.key(sequel))
                self.assertFalse(matcher.compare([listing(title=sequel)], [row(title=original)])[0])

    def test_fullwidth_case_and_multiplication_symbol_are_normalized(self):
        self.assertEqual(matcher.key('Ｈｉｄａｍａｒｉ　Sketch×365'), matcher.key('Hidamari Sketch x 365'))

    def test_wrong_existing_source_id_is_flagged_and_cannot_be_propagated(self):
        wrong = row('Anissia', '26', year=2010, title='', title_ko='히다마리 스케치×☆☆☆',
                    cross_source_ids_json='{"anilist_ids":["1"]}')
        links, _, conflicts = matcher.compare([listing()], [row(year=2007), wrong])
        self.assertEqual(links[0]['source_refs'], [{'source': 'AniList', 'id': '1'}])
        self.assertEqual(conflicts[0]['reasons'], ['year_conflict'])

    def test_recorded_reanime_id_needs_matching_title_year_format_and_counts(self):
        good = row('ReAnime', 'good', cross_source_ids_json='{"anilist_ids":["1"]}')
        bad = row('ReAnime', 'bad', title='Different Work', cross_source_ids_json='{"anilist_ids":["1"]}')
        links, _, _ = matcher.compare([listing()], [row(), good, bad])
        self.assertEqual(links[0]['source_refs'], [{'source': 'AniList', 'id': '1'}, {'source': 'ReAnime', 'id': 'good'}])

    def test_reviewed_rejection_and_explicit_acceptance_override_automation(self):
        rejected = {'example-slug': {'status': 'rejected', 'reason': 'Different release'}}
        self.assertFalse(matcher.compare([listing()], [row()], rejected)[0])
        accepted = {'example-slug': {'status': 'accepted', 'target': 'AniList:1', 'reason': 'Reviewed original title'}}
        links, _, _ = matcher.compare([listing(count=13)], [row()], accepted)
        self.assertEqual(links[0]['status'], 'reviewed')
        self.assertEqual(links[0]['evidence']['rule'], 'reviewed_decision')

    def test_an_automatic_link_cannot_claim_an_explicitly_reviewed_target(self):
        decisions={'manual':{'status':'accepted','target':'AniList:1','reason':'Confirmed other edition'}}
        links,outcomes,_=matcher.compare([listing('manual',count=13),listing('automatic')],[row()],decisions)
        self.assertEqual([link['anikoto_id'] for link in links],['manual'])
        self.assertEqual(next(o for o in outcomes if o['anikoto_id']=='automatic')['reason'],'reviewed_target_already_linked')

    def test_correction_has_identity_guards_and_keeps_unrelated_source_fields(self):
        original = row(title_ko='Example', cross_source_ids_json='{"mal_ids":["99"]}')
        change = {'source': 'AniList', 'id': '1', 'expected': {'title_ko': 'Example', 'year': 2020},
                  'set': {'format': 'TV_SHORT'}, 'cross_source_ids': {'reanime_ids': ['new']},
                  'reason': 'Official corrected format', 'urls': ['https://example.com/official']}
        result = corrected_rows([original], [change])[0]
        self.assertEqual(json.loads(result['cross_source_ids_json'])['mal_ids'], ['99'])
        self.assertEqual(original['format'], 'TV')
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            corrected_rows([row(title_ko='Different')], [change])
        self.assertEqual(corrected_rows([result], [change]), [result])

    def test_partial_snapshot_is_rejected_before_reading_the_baseline(self):
        with patch.object(matcher, 'validate_catalog', return_value={'complete': False}), \
                patch.object(matcher, 'read_catalog', side_effect=AssertionError('must not read')):
            with self.assertRaisesRegex(ValueError, 'completed crawl'):
                matcher.match('baseline.db', 'catalog.db', 'output')

    def test_previous_rejection_is_applied_to_the_next_match_report(self):
        with tempfile.TemporaryDirectory() as root:
            baseline,catalog=Path(root)/'baseline.db',Path(root)/'catalog.db'
            with sqlite3.connect(baseline) as db:
                db.execute('CREATE TABLE anikoto_work_links(anikoto_id TEXT,target_source TEXT,target_id TEXT,status TEXT,payload_json TEXT)')
                db.execute('INSERT INTO anikoto_work_links VALUES (?,?,?,?,?)',('example-slug','','','rejected','{"reason":"Different edition"}'))
            with sqlite3.connect(catalog) as db:
                db.execute('CREATE TABLE anikoto_catalog(source_record_id TEXT,payload_json TEXT,last_seen_run TEXT)')
                db.execute('INSERT INTO anikoto_catalog VALUES (?,?,?)',('example-slug',json.dumps(listing()),'run'))
            with patch.object(matcher,'validate_catalog',return_value={'complete':True,'run_id':'run'}), \
                    patch.object(matcher,'read_catalog',return_value=[row()]):
                report=matcher.match(baseline,catalog,Path(root)/'out')
            self.assertEqual((report['automatic'],report['rejected']),(0,1))
            payload=json.loads((Path(root)/'out/anikoto-matches.json').read_text())
            self.assertEqual(payload['rejections'][0]['reason'],'Different edition')

    def test_database_copy_preserves_reviewed_links_and_protected_rows(self):
        with tempfile.TemporaryDirectory() as root:
            baseline, catalog, matches, output = [Path(root)/n for n in ['base.db','catalog.db','matches.json','out.db']]
            with sqlite3.connect(baseline) as db:
                db.executescript('''CREATE TABLE entries(source TEXT,source_record_id TEXT,year INTEGER,title_ko TEXT);
                    INSERT INTO entries VALUES ('AniList','99',1999,'Reviewed title');
                    CREATE TABLE protected_episode(file_id TEXT,target_episode INTEGER);
                    INSERT INTO protected_episode VALUES ('original',1);
                    CREATE TABLE anikoto_work_links(anikoto_id TEXT PRIMARY KEY,target_source TEXT,target_id TEXT,status TEXT,payload_json TEXT);
                    INSERT INTO anikoto_work_links VALUES ('manual','AniList','99','reviewed','{"reviewed":true}');
                    INSERT INTO anikoto_work_links VALUES ('rejected','AniList','88','rejected','{"rejected":true}');
                    INSERT INTO anikoto_work_links VALUES ('old','AniList','77','automatic','{}');
                    CREATE TABLE anikoto_catalog(source_record_id TEXT PRIMARY KEY,payload_json TEXT NOT NULL);
                    INSERT INTO anikoto_catalog VALUES ('manual','{"title":"Reviewed original","genre_links":[{"name":"unused"}]}');
                    INSERT INTO anikoto_catalog VALUES ('old','{"unused":"stale unlinked payload"}');''')
            with sqlite3.connect(catalog) as db:
                db.execute('CREATE TABLE anikoto_catalog(source_record_id TEXT,payload_json TEXT,last_seen_run TEXT)')
                db.executemany('INSERT INTO anikoto_catalog VALUES (?,?,?)', [('current','{"title":"Current","genres":["Comedy"],"genre_links":[{"name":"unused"}],"score":8}','run'),('stale','{}','previous'),('unmatched','{}','run')])
            before = (matcher.sha256(baseline),matcher.sha256(catalog))
            payload = {'metadata':{'baseline_sha256':before[0],'anikoto_sha256':before[1],'crawl_run_id':'run'},
                       'links':[{'anikoto_id':slug,'target_source':'AniList','target_id':'1','status':'automatic',
                                 'source_refs':[{'source':'AniList','id':'1'}]}
                                for slug in ['manual','rejected','current']],
                       'rejections':[{'anikoto_id':'new-rejection','status':'rejected','reason':'Verified different season'}]}
            matches.write_text(json.dumps(payload))
            matcher.create_matched_database(baseline,catalog,matches,output)
            self.assertEqual(before,(matcher.sha256(baseline),matcher.sha256(catalog)))
            with sqlite3.connect(output) as db:
                self.assertEqual(db.execute('SELECT * FROM protected_episode').fetchall(),[('original',1)])
                self.assertEqual(db.execute('SELECT anikoto_id,target_id,status FROM anikoto_work_links ORDER BY anikoto_id').fetchall(),
                                 [('current','1','automatic'),('manual','99','reviewed'),('new-rejection','','rejected'),('rejected','88','rejected')])
                self.assertEqual(db.execute('SELECT source_record_id FROM anikoto_catalog ORDER BY source_record_id').fetchall(),[('current',),('manual',)])
                info=json.loads(db.execute("SELECT payload_json FROM anikoto_catalog WHERE source_record_id='manual'").fetchone()[0])
                self.assertEqual((info['year'],info['korean_title']),(1999,'Reviewed title'))
                info=json.loads(db.execute("SELECT payload_json FROM anikoto_catalog WHERE source_record_id='current'").fetchone()[0])
                self.assertEqual(info['genres'],['Comedy'])
                self.assertNotIn('genre_links',info)
                self.assertNotIn('score',info)
                self.assertEqual(json.loads(db.execute("SELECT payload_json FROM anikoto_work_links WHERE anikoto_id='current'").fetchone()[0]),
                                 {'source_refs':[{'source':'AniList','id':'1'}]})
            with self.assertRaisesRegex(ValueError,'new path'):
                matcher.create_matched_database(baseline,catalog,matches,output)
            with sqlite3.connect(baseline) as db:db.execute("INSERT INTO protected_episode VALUES ('new',2)")
            with self.assertRaisesRegex(ValueError,'inputs changed'):
                matcher.create_matched_database(baseline,catalog,matches,Path(root)/'other.db')


if __name__ == '__main__':
    unittest.main()

