#!/usr/bin/env python3
"""Fast offline invariants for the current-release multiwork patch plan."""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_multiwork_release_20261015 import PLAN, ROOT, checked_review, patch_catalog

class MultiworkReleaseTests(unittest.TestCase):
    def setUp(self):
        self.plan = json.loads(PLAN.read_text(encoding="utf-8"))

    def test_reviewed_236_and_explicit_54(self):
        all_rows, decisions = checked_review(ROOT, self.plan)
        self.assertEqual(len(all_rows), 236)
        self.assertEqual(len(decisions), 54)
        self.assertEqual(len({int(r["릴리즈원본번호"]) for r in all_rows}), 236)

    def test_no_held_or_unapproved_corrections(self):
        all_rows, _ = checked_review(ROOT, self.plan)
        rows = {r["원본키"]: r for r in all_rows}
        for change in self.plan["targeted"]:
            self.assertIn(rows[change["origin"]]["상태"], {"수정확정", "연결방침확정"})
            self.assertNotIn(rows[change["origin"]]["상태"], {"전체보류", "부분보류", "공개대기"})
        self.assertEqual(self.plan["protect"]["new_episode_assignments"], 0)

    def test_patch_is_idempotent_and_scoped(self):
        with sqlite3.connect(":memory:") as db:
            db.execute("CREATE TABLE entries(source TEXT, source_record_id TEXT, cross_source_ids_json TEXT, PRIMARY KEY(source,source_record_id))")
            for src,id_,anilist,reanime in (
                ("Onnada","384",["2686","145563"],[]),
                ("Onnada","6169",[],["attack-on-titan-the-last-attack-vdtdb4"]),
                ("ReAnime","hamelin-no-violin-hiki-jxynbq",["1325"],[]),
                ("Anissia","2695",["113652"],[]),
                ("Anissia","3160",["175868"],[]),
            ):
                db.execute("INSERT INTO entries VALUES (?,?,?)",
                           (src,id_,json.dumps({"anilist_ids":anilist,"reanime_ids":reanime,"onnada_ids":[]})))
            for x in self.plan["targeted"]:
                target_src = "AniList" if x["field"] == "anilist_ids" else "ReAnime"
                for target in x["keep"]:
                    db.execute("INSERT OR IGNORE INTO entries VALUES (?,?,?)",
                               (target_src,target,'{"anilist_ids":[],"reanime_ids":[],"onnada_ids":[]}'))
            changes = patch_catalog(db,self.plan["targeted"])
            self.assertEqual(len(changes),5)
            again = patch_catalog(db,self.plan["targeted"])
            self.assertTrue(all(c["before"] == c["after"] for c in again))

    def test_unexpected_old_link_rejected(self):
        with sqlite3.connect(":memory:") as db:
            db.execute("CREATE TABLE entries(source TEXT, source_record_id TEXT, cross_source_ids_json TEXT, PRIMARY KEY(source,source_record_id))")
            db.execute("INSERT INTO entries VALUES (?,?,?)",
                       ("Onnada","384",'{"anilist_ids":["unexpected","2686"],"reanime_ids":[],"onnada_ids":[]}'))
            with self.assertRaisesRegex(ValueError, "Unexpected existing link"):
                patch_catalog(db,[self.plan["targeted"][0]])

if __name__ == "__main__":
    unittest.main()
