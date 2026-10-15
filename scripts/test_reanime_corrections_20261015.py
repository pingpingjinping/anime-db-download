import json
import sqlite3
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent))
from build_reanime_corrections_20261015 import PLAN,apply

class SourceLinkTests(unittest.TestCase):
    def setUp(self):
        self.plan=json.loads(PLAN.read_text(encoding="utf-8"))

    def test_explicit_pinned_update_scope(self):
        self.assertEqual(len(self.plan["changes"]),2)
        self.assertEqual(self.plan["expected_review_ledger_rows"],236)
        self.assertEqual(self.plan["protect"]["new_episode_assignments"],0)
        self.assertEqual([r["source"]+":"+r["id"] for r in self.plan["changes"]],["Onnada:384","Anissia:2695"])

    def sample(self):
        db=sqlite3.connect(":memory:")
        db.execute("CREATE TABLE entries(source TEXT, source_record_id TEXT, cross_source_ids_json TEXT, PRIMARY KEY(source,source_record_id))")
        for row in self.plan["changes"]:
            data={"anilist_ids":row["original_anilist_ids"],"reanime_ids":row["before"],"onnada_ids":[]}
            db.execute("INSERT INTO entries VALUES (?,?,?)",
                       (row["source"],row["id"],json.dumps(data)))
            for target in row["after"]:
                db.execute("INSERT OR IGNORE INTO entries VALUES (?,?,?)",("ReAnime",target,'{"anilist_ids":[],"reanime_ids":[]}'))
        return db

    def test_applies_only_expected_two(self):
        with self.sample() as db:
            updates=apply(db,self.plan)
            self.assertEqual(len(updates),2)
            for row in self.plan["changes"]:
                old=db.execute("SELECT cross_source_ids_json FROM entries WHERE source=? AND source_record_id=?",(row["source"],row["id"])).fetchone()[0]
                data=json.loads(old)
                self.assertEqual(data[row["field"]],row["after"])
                self.assertEqual(data["anilist_ids"],row["original_anilist_ids"])

    def test_fails_on_changed_prior_links(self):
        with self.sample() as db:
            db.execute("UPDATE entries SET cross_source_ids_json=? WHERE source='Onnada' AND source_record_id='384'",
                        ('{"anilist_ids":["4208"],"reanime_ids":["unknown-1980"]}',))
            with self.assertRaisesRegex(ValueError,"Prior ReAnime identities differ"):
                apply(db,self.plan)

if __name__=="__main__":
    unittest.main()
