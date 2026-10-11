"""Regression tests for October's reviewed work/file/episode protections."""
import json
import sqlite3
import unittest

from reviewed_matching_guard import (
    assert_reviewed_state, capture_reviewed_state, is_reviewed_source,
    read_rules, verify_role,
)


class ReviewRulesTests(unittest.TestCase):
    def test_release_rules_are_complete_and_do_not_assign_episodes(self):
        rules = read_rules()
        self.assertEqual(rules["new_episode_assignments"], 0)
        if rules.get('body_audit_batch'):
            self.assertEqual(rules['manual_episode_assignments'],60)
            episodes=rules['roles']['registry']['subtitle_confirmed_episodes']['rows']
            self.assertEqual(len(episodes),36)
            self.assertEqual(sum(r['expected']['status']=='hold' for r in episodes),12)
            manual=rules['roles']['registry']['body_audit_confirmed_episodes_v1']['rows']
            self.assertEqual(len(manual),60)
            self.assertTrue(all(r['expected']['status']=='confirmed_episode' for r in manual))
            return
        self.assertEqual(
            {role: {table: len(group["rows"]) for table, group in groups.items()}
             for role, groups in rules["roles"].items()},
            {
                "catalog": {"anissia_year_match": 87, "entries": 87},
                "registry": {
                    "packaged_files_v6": 583, "subsubsub_file_registry_v1": 36,
                    "subsubsub_file_targets_v1": 72,
                    "subtitle_confirmed_episodes": 36,
                    "subtitle_review_details": 13, "subtitle_work_reanime_links": 127,
                },
            })
        episodes = rules["roles"]["registry"]["subtitle_confirmed_episodes"]["rows"]
        self.assertEqual(len(episodes), 36)
        self.assertTrue(all(r["expected"]["status"] == "hold"
                            and r["expected"]["target_episode"] is None for r in episodes))

    def test_exact_reviewed_ids_survive_and_bad_target_is_detected(self):
        rules = {"roles": {"catalog": {
            "entries": {"primary_key": ["source", "source_record_id"],
                        "rows": [{"key": {"source": "Anissia", "source_record_id": "15"},
                                  "expected": {"existing_match_status": "strong_exact",
                                               "cross_source_ids_json": {"reanime_ids": ["right"]}}}]}}}}
        with sqlite3.connect(":memory:") as db:
            db.execute("CREATE TABLE entries (source TEXT, source_record_id TEXT, "
                       "existing_match_status TEXT, cross_source_ids_json TEXT)")
            db.execute("INSERT INTO entries VALUES ('Anissia','15','strong_exact',?)",
                       (json.dumps({"reanime_ids": ["right"]}),))
            self.assertEqual(verify_role(db, "catalog", rules), 1)
            db.execute("UPDATE entries SET cross_source_ids_json=?",
                       (json.dumps({"reanime_ids": ["right", "wrong-movie"]}),))
            with self.assertRaisesRegex(ValueError, "cross_source_ids_json changed"):
                verify_role(db, "catalog", rules)

    def test_hold_status_and_removed_link_must_not_regress(self):
        rules = {"roles": {"registry": {
            "subtitle_confirmed_episodes": {"primary_key": ["map_id"], "rows": [
                {"key": {"map_id": 12}, "expected": {
                    "target_source": None, "target_id": None,
                    "target_episode": None, "status": "hold"}}]},
            "subtitle_work_reanime_links": {"primary_key": [
                "target_source", "target_id", "reanime_id"], "rows": [
                {"key": {"target_source": "Anissia", "target_id": "11",
                         "reanime_id": "wrong-tv"}, "absent": True}]},
        }}}
        with sqlite3.connect(":memory:") as db:
            db.execute("CREATE TABLE subtitle_confirmed_episodes (map_id INTEGER, "
                       "target_source TEXT,target_id TEXT,target_episode INTEGER,status TEXT)")
            db.execute("CREATE TABLE subtitle_work_reanime_links (target_source TEXT,"
                       "target_id TEXT,reanime_id TEXT)")
            db.execute("INSERT INTO subtitle_confirmed_episodes VALUES(12,NULL,NULL,NULL,'hold')")
            self.assertEqual(verify_role(db, "registry", rules), 2)
            db.execute("INSERT INTO subtitle_work_reanime_links VALUES('Anissia','11','wrong-tv')")
            with self.assertRaisesRegex(ValueError, "reappeared"):
                verify_role(db, "registry", rules)
            db.execute("DELETE FROM subtitle_work_reanime_links")
            db.execute("UPDATE subtitle_confirmed_episodes SET status='confirmed_episode',"
                       "target_episode=11 WHERE map_id=12")
            with self.assertRaisesRegex(ValueError, "Reviewed matching regression"):
                verify_role(db, "registry", rules)

    def test_monthly_snapshot_preserves_later_manual_overrides_and_episodes(self):
        with sqlite3.connect(":memory:") as db:
            db.execute("CREATE TABLE anissia_year_match (anime_no INTEGER PRIMARY KEY)")
            db.execute("INSERT INTO anissia_year_match VALUES (15)")
            db.execute("CREATE TABLE subtitle_confirmed_episodes "
                       "(map_id INTEGER PRIMARY KEY,target_episode INTEGER,status TEXT)")
            db.execute("INSERT INTO subtitle_confirmed_episodes VALUES (9,13,'confirmed_episode')")
            db.execute("CREATE TABLE entries (source TEXT,source_record_id TEXT,"
                       "cross_source_ids_json TEXT,existing_match_status TEXT,source_metadata_json TEXT)")
            # This is a user override applied *after* the October correction.
            db.execute("INSERT INTO entries VALUES ('Anissia','15',?,'manual',?)",
                       (json.dumps({"reanime_ids": ["manual-season-2"]}), "{}"))
            before = capture_reviewed_state(db)
            db.execute("INSERT INTO entries VALUES('ReAnime','new','{}',NULL,'{}')")
            assert_reviewed_state(db, before)  # New unreviewed works are allowed
            db.execute("UPDATE entries SET cross_source_ids_json=? "
                       "WHERE source='Anissia' AND source_record_id='15'",
                       (json.dumps({"reanime_ids": ["wrong-season-1"]}),))
            with self.assertRaisesRegex(ValueError, "reviewed source links"):
                assert_reviewed_state(db, before)
            db.execute("UPDATE entries SET cross_source_ids_json=? "
                       "WHERE source='Anissia' AND source_record_id='15'",
                       (json.dumps({"reanime_ids": ["manual-season-2"]}),))
            db.execute("UPDATE subtitle_confirmed_episodes SET target_episode=1 WHERE map_id=9")
            with self.assertRaisesRegex(ValueError, "Manual review/episode"):
                assert_reviewed_state(db, before)

    def test_marker_protects_a_hold_even_without_year_match_row(self):
        marked = {"source_metadata_json": json.dumps({
            "reviewed_matching_20261011": {"decision": "hold"}})}
        self.assertTrue(is_reviewed_source("Anissia", "15", marked, set()))
        self.assertTrue(is_reviewed_source("Anissia", "15", {
            "source_metadata_json": "{}"}, {"15"}))
        self.assertFalse(is_reviewed_source("Anissia", "16", {
            "source_metadata_json": "{}"}, {"15"}))


if __name__ == "__main__":
    unittest.main()
