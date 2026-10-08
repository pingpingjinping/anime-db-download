import json
import sqlite3
import unittest

import match_new_sources as matcher
from work_identity import base_identity, compatibility, identity, medium, parse_title


def catalog():
    db = sqlite3.connect(':memory:')
    db.execute('''CREATE TABLE entries (
      source TEXT, source_record_id TEXT, year INTEGER, year_basis TEXT, source_year_raw TEXT,
      title_ko TEXT, title_native TEXT, title_romaji TEXT, title_english TEXT, format TEXT,
      status TEXT, genres TEXT, cross_source_ids_json TEXT, existing_match_status TEXT,
      source_metadata_json TEXT, PRIMARY KEY(source,source_record_id))''')
    db.execute('CREATE TABLE anissia_year_match (anime_no INTEGER PRIMARY KEY)')
    db.execute('CREATE TABLE anissia_year_candidates (anime_no INTEGER,reanime_id TEXT)')
    matcher.prepare_tables(db)
    return db


def add(db, source, rid, native='検証アニメーション', ko='', year=2026, fmt=None,
        romaji='', english='', ids=(), meta=None, pending=False):
    fmt = ('TV' if source != 'Anissia' else '') if fmt is None else fmt
    db.execute('INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (source, rid, year, 'test', str(year or ''), ko, native, romaji, english,
                fmt, '', '', json.dumps({'anilist_ids':list(ids),'reanime_ids':[]}), '',
                json.dumps(meta or {})))
    if pending:
        db.execute('INSERT INTO monthly_match_pending VALUES (?,?)', (source, rid))


class IdentityTests(unittest.TestCase):
    def test_season_notations_share_one_parser_and_key(self):
        for suffix in ('第2期', '2nd Season', 'Season 2', '2기', 'S02', 'II'):
            with self.subTest(suffix=suffix):
                self.assertEqual(parse_title('検証アニメーション ' + suffix),
                                 ('検証アニメーション', frozenset({2}), frozenset()))

    def test_part_is_separate_and_numbers_in_names_survive(self):
        self.assertEqual(parse_title('検証アニメーション Part 2')[1:], (frozenset(), frozenset({2})))
        for marker in ('Part II', '第2クール', '2nd Cour', '2쿨'):
            self.assertEqual(parse_title('検証アニメーション '+marker)[1:], (frozenset(), frozenset({2})))
        self.assertIn('86', base_identity('86 エイティシックス'))
        self.assertIn('2024', base_identity('らんま1/2 (2024) 第3期'))

    def test_unicode_equivalence_does_not_erase_sequel_symbols(self):
        self.assertEqual(identity('ガール'), identity('ガール'))
        self.assertEqual(identity('アニメ＋'), identity('アニメ+'))
        self.assertNotEqual(identity('アニメ+'), identity('アニメ'))
        self.assertNotEqual(identity('アニメ☆'), identity('アニメ'))

    def test_movie_label_becomes_type_constraint(self):
        self.assertEqual(base_identity('映画 君と花火と約束と'), base_identity('君と花火と約束と'))
        self.assertEqual(medium({'title_native':'映画 君と花火と約束と'}), 'MOVIE')

    def test_unknown_season_is_not_a_confirmed_first_season(self):
        conflicts, missing = compatibility({'title_native':'アニメ 第2期','year':2026},
                                           {'title_native':'アニメ','year':2026,'format':'TV'})
        self.assertNotIn('season_conflict', conflicts)
        self.assertIn('season_missing', missing)

    def test_type_markers_are_bounded_not_arbitrary_words(self):
        self.assertEqual(medium({'title_native':'アニメ TVSP'}), 'SPECIAL')
        self.assertEqual(medium({'title_native':'アニメ OAD'}), 'OVA')
        self.assertIsNone(medium({'title_english':'Special A'}))
        self.assertEqual(medium({'format':'TV_SHORT'}), 'TV')


class NewSourceMatchingTests(unittest.TestCase):
    def setUp(self):
        self.db = catalog()
        self.addCleanup(self.db.close)

    def source(self, **kw):
        kw.setdefault('ko', '검증 작품')
        add(self.db, 'Anissia', '1', pending=True, **kw)

    def auto(self):
        return [tuple(r) for r in self.db.execute('SELECT reanime_id,anissia_no FROM monthly_auto_matches')]

    def reasons(self):
        return {r[0] for r in self.db.execute('SELECT reason FROM monthly_match_candidates')}

    def test_bracketed_short_kanji_reading_is_review_only(self):
        self.source(native='神統記 〈テオゴニア〉', ko='신통기〈테오고니아〉', year=2025)
        add(self.db, 'ReAnime', 'teogonia', native='神統記', year=2025)
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [])
        row = self.db.execute("SELECT evidence_json FROM monthly_match_candidates WHERE reanime_id='teogonia'").fetchone()
        self.assertIn('bracket_reading_alias', json.loads(row[0])['evidence'])

    def test_ova_bonus_subtitle_recalled_even_when_tv_exact_exists(self):
        self.source(native='この素晴らしい世界に祝福を！３ OVA', ko='축복 3 OVA', year=2025)
        add(self.db, 'ReAnime', 'tv', native='この素晴らしい世界に祝福を！３', year=2024)
        add(self.db, 'ReAnime', 'bonus', native='この素晴らしい世界に祝福を！３ーBONUS STAGEー', year=2025, fmt='OVA')
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [])
        rows = {r[0]:json.loads(r[1]) for r in self.db.execute('SELECT reanime_id,evidence_json FROM monthly_match_candidates')}
        self.assertIn('special_subtitle_alias', rows['bonus']['evidence'])
        self.assertIn('format_conflict', rows['tv']['conflicts'])

    def test_bonus_alias_does_not_erase_literal_digits(self):
        self.source(native='検証アニメーション3 OVA', ko='검증 3 OVA')
        add(self.db, 'ReAnime', 'wrong', native='検証アニメーション30 BONUS', fmt='OVA')
        matcher.refresh_auto_matches(self.db)
        self.assertNotIn('wrong', [r[0] for r in self.db.execute('SELECT reanime_id FROM monthly_match_candidates')])

    def test_unique_native_exact_same_year(self):
        self.source()
        add(self.db, 'ReAnime', 'correct')
        result = matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [('correct', 1)])
        self.assertTrue(result['changed'])
        self.assertFalse(matcher.refresh_auto_matches(self.db)['changed'])

    def test_unpending_work_is_not_swept(self):
        add(self.db, 'Anissia', '1', ko='검증 작품')
        add(self.db, 'ReAnime', 'correct')
        self.assertEqual(matcher.refresh_auto_matches(self.db)['auto_matched'], 0)

    def test_conflicting_season_does_not_block_correct_season(self):
        self.source(native='検証アニメーション 第3期', ko='검증 작품 3기')
        add(self.db, 'ReAnime', 'correct', native='検証アニメーション Season 3')
        add(self.db, 'ReAnime', 'wrong', native='検証アニメーション 2nd Season')
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [('correct', 1)])
        self.assertIn('season_conflict', self.reasons())

    def test_explicit_season_match_beats_series_only_hint_without_guessing_s1(self):
        self.source(ko='검증 작품 2기')
        add(self.db, 'ReAnime', 'correct', native='検証アニメーション 第2期')
        add(self.db, 'ReAnime', 'unlabelled')
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [('correct', 1)])
        self.assertIn('season_missing', self.reasons())

    def test_only_unlabelled_target_does_not_confirm_later_season(self):
        self.source(ko='검증 작품 2기')
        add(self.db, 'ReAnime', 'unlabelled')
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [])
        self.assertIn('season_missing', self.reasons())

    def test_conflicting_languages_are_not_hidden_by_native_exact(self):
        self.source(native='検証アニメーション 第2期', ko='검증 작품 3기')
        add(self.db, 'ReAnime', 'target', native='検証アニメーション 第2期')
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [])
        self.assertIn('season_inconsistent', self.reasons())

    def test_same_year_strong_match_beats_unknown_year_candidate(self):
        self.source()
        add(self.db, 'ReAnime', 'correct')
        add(self.db, 'ReAnime', 'old', year=2001)
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [('correct', 1)])
        add(self.db, 'ReAnime', 'unknown', year=None)
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [('correct', 1)])
        self.assertIn('year_missing', self.reasons())

    def test_movie_prefix_does_not_merge_tv(self):
        self.source(native='映画 検証アニメーション', ko='영화 검증 작품')
        add(self.db, 'ReAnime', 'movie', fmt='MOVIE')
        add(self.db, 'ReAnime', 'tv')
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [('movie', 1)])
        self.assertIn('format_conflict', self.reasons())

    def test_tv_special_is_not_tv_series(self):
        self.source(native='検証アニメーション TVSP')
        add(self.db, 'ReAnime', 'special', fmt='SPECIAL')
        add(self.db, 'ReAnime', 'tv')
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [('special', 1)])

    def test_long_short_title_is_review_only(self):
        self.source(native='検証用の長い作品タイトル')
        add(self.db, 'ReAnime', 'target', native='検証用の長い作品タイトル追加の副題')
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [])
        self.assertIn('native_alias_needed', self.reasons())

    def test_bad_declared_id_cannot_lend_title(self):
        self.source(native='別の作品のタイトル')
        add(self.db, 'AniList', '100', native='別の作品のタイトル')
        add(self.db, 'ReAnime', 'wrong', ids=['100'])
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [])
        self.assertIn('no_catalog_entry', self.reasons())

    def test_zero_id_ignored_and_latest_declaration_supersedes_stale_ids(self):
        self.source()
        add(self.db, 'AniList', '100', native='別の作品のタイトル')
        add(self.db, 'AniList', '101')
        add(self.db, 'ReAnime', 'target', ids=['0','100','101'], meta={'anilist_id':101})
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [('target', 1)])
        details = json.loads(self.db.execute('SELECT evidence_json FROM monthly_auto_matches').fetchone()[0])
        self.assertEqual(details['validated_anilist_ids'], ['101'])

    def test_same_work_two_urls_does_not_pick_arbitrary_url(self):
        self.source()
        add(self.db, 'AniList', '100')
        add(self.db, 'ReAnime', 'first', ids=['100'])
        add(self.db, 'ReAnime', 'second', ids=['100'])
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [])
        self.assertEqual(self.reasons(), {'multiple_urls'})

    def test_reverse_ambiguity_includes_unpending_anissia(self):
        self.source()
        add(self.db, 'Anissia', '2', ko='또 다른 등록')
        add(self.db, 'ReAnime', 'target')
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [])
        self.assertIn('multiple_work_candidates', self.reasons())

    def test_reviewed_source_and_target_are_preserved(self):
        self.source()
        add(self.db, 'ReAnime', 'target', pending=True)
        self.db.execute('INSERT INTO anissia_year_match VALUES (1)')
        self.db.execute('INSERT INTO anissia_year_candidates VALUES (1, "target")')
        before = list(self.db.execute('SELECT * FROM entries'))
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [])
        self.assertEqual(list(self.db.execute('SELECT * FROM entries')), before)
        self.assertEqual(list(self.db.execute('SELECT * FROM anissia_year_match')), [(1,)])

    def test_alternate_url_for_reviewed_work_is_reserved(self):
        self.source()
        add(self.db, 'Anissia', '2', ko='검토된 작품')
        add(self.db, 'AniList', '100')
        add(self.db, 'ReAnime', 'reserved', ids=['100'])
        add(self.db, 'ReAnime', 'alternate', ids=['100'])
        self.db.execute('INSERT INTO anissia_year_match VALUES (2)')
        self.db.execute('INSERT INTO anissia_year_candidates VALUES (2, "reserved")')
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [])
        self.assertIn('reviewed_work_reserved', self.reasons())

    def test_candidate_only_change_is_saved_and_second_run_is_stable(self):
        self.source(year=None)
        add(self.db, 'ReAnime', 'target')
        self.assertTrue(matcher.refresh_auto_matches(self.db)['changed'])
        self.assertFalse(matcher.refresh_auto_matches(self.db)['changed'])
        self.db.execute("UPDATE entries SET year=2025 WHERE source='ReAnime'")
        self.assertTrue(matcher.refresh_auto_matches(self.db)['changed'])
        self.assertEqual(self.auto(), [])

    def test_pending_source_retries_after_native_and_year_arrive(self):
        self.source(native='', year=None)
        add(self.db, 'ReAnime', 'target')
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [])
        self.db.execute("UPDATE entries SET title_native='検証アニメーション',year=2026 WHERE source='Anissia'")
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [('target', 1)])

    def test_explicit_different_parts_are_separate(self):
        self.source(native='検証アニメーション Part 2')
        add(self.db, 'ReAnime', 'correct', native='検証アニメーション Part 2')
        add(self.db, 'ReAnime', 'wrong', native='検証アニメーション Part 1')
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [('correct', 1)])

    def test_trailing_digit_needs_multilingual_season_confirmation(self):
        self.source(native='検証アニメーション 第3期', ko='검증 작품 3기')
        add(self.db, 'ReAnime', 'correct', native='検証アニメーション３', english='Test Anime Season 3')
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [('correct',1)])

    def test_bare_digit_does_not_imply_season(self):
        self.source(native='検証アニメーション 第3期')
        add(self.db, 'ReAnime', 'unknown', native='検証アニメーション3')
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [])

    def test_release_year_suffix_requires_row_year(self):
        self.source()
        add(self.db, 'ReAnime', 'correct', native='検証アニメーション (2026)')
        add(self.db, 'ReAnime', 'wrong', native='検証アニメーション (2025)')
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [('correct',1)])

    def test_native_latin_does_not_veto_exact_title_in_another_field(self):
        self.source(ko='Test Anime')
        add(self.db, 'ReAnime', 'correct', native='Test Anime')
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [('correct',1)])

    def test_different_japanese_native_still_needs_alias(self):
        self.source(ko='Test Anime')
        add(self.db, 'ReAnime', 'wrong', native='別のアニメーション', english='Test Anime')
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [])
        self.assertIn('native_alias_needed',self.reasons())

    def test_two_complete_same_year_targets_still_hold(self):
        self.source()
        add(self.db, 'ReAnime', 'first')
        add(self.db, 'ReAnime', 'second')
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [])

    def test_reverse_unknown_year_does_not_veto_complete_source(self):
        self.source()
        add(self.db, 'Anissia', '2', ko='연도 미정', year=None)
        add(self.db, 'ReAnime', 'correct')
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [('correct',1)])

    def test_named_sequel_can_use_exact_full_native_title_with_same_year(self):
        native='検証アニメーション ～続きの物語～'
        self.source(native=native)
        add(self.db, 'ReAnime', 'correct', native=native, english='Test Anime Season 2')
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [('correct',1)])

    def test_unknown_season_is_not_completed_across_different_years(self):
        self.source()
        add(self.db, 'ReAnime', 'wrong', english='Test Anime Season 2', year=2027)
        matcher.refresh_auto_matches(self.db)
        self.assertEqual(self.auto(), [])


class RecentSampleRegressionTests(unittest.TestCase):
    def test_2025_fifty_preserve_46_targets_and_four_holds(self):
        from pathlib import Path
        from audit_new_source_matches import replay
        fixture = json.loads((Path(__file__).resolve().parents[1] / 'data/matching/anissia_2025_50_20261008.json').read_text())
        result = replay(fixture)
        self.assertEqual(result['summary']['auto_matched'],46)
        self.assertEqual(result['summary']['review_works'],4)
        for sample in result['samples']:
            self.assertEqual(sample['reanime_id'],fixture['expected_automatic'].get(str(sample['anissia_no'])))
            if expected := fixture['expected_provisional_candidates'].get(str(sample['anissia_no'])):
                self.assertIn(expected, [c['reanime_id'] for c in sample['candidates']])


    def test_fifty_recent_samples_preserve_verified_targets_and_six_holds(self):
        from audit_new_source_matches import DEFAULT_SAMPLES, replay
        fixture = json.loads(DEFAULT_SAMPLES.read_text())
        result = replay(fixture)
        self.assertEqual(len(result['samples']), 50)
        self.assertEqual(result['summary']['auto_matched'], 44)
        self.assertEqual(result['summary']['review_works'], 6)
        expected = dict(fixture['expected_automatic'], **{'3479':'the-vermilion-mask-5577y8'})
        for sample in result['samples']:
            with self.subTest(anissia_no=sample['anissia_no']):
                self.assertEqual(sample['reanime_id'], expected.get(str(sample['anissia_no'])))
        held = {s['anissia_no'] for s in result['samples'] if s['status']=='review'}
        self.assertEqual(held, {3527,3433,3471,3470,3578,3590})


if __name__ == '__main__':
    unittest.main()
