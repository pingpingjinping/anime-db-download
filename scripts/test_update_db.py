#!/usr/bin/env python3
import json
import sqlite3
import unittest

import update_db


class AniListWatermarkTests(unittest.TestCase):
    def make_db(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("CREATE TABLE entries (source TEXT NOT NULL, source_metadata_json TEXT)")
        return db

    def test_saved_watermark_wins(self):
        db = self.make_db()
        db.execute("INSERT INTO metadata VALUES ('anilist_updated_at_watermark', '1700000000')")
        db.execute("INSERT INTO entries VALUES ('AniList', ?)",
                   (json.dumps({"updatedAt": 1800000000}),))
        self.assertEqual(
            update_db.resolve_anilist_watermark(db, 1900000000),
            (1700000000, "saved"),
        )

    def test_bootstraps_from_latest_snapshot_updated_at(self):
        db = self.make_db()
        db.executemany("INSERT INTO entries VALUES ('AniList', ?)", [
            (json.dumps({"updatedAt": 1700000000}),),
            (json.dumps({"updatedAt": 1750000000}),),
            ("not-json",),
            (json.dumps({"updatedAt": None}),),
        ])
        self.assertEqual(
            update_db.resolve_anilist_watermark(db, 1900000000),
            (1750000000, "snapshot_updatedAt"),
        )

    def test_snapshot_watermark_is_clamped_to_now(self):
        db = self.make_db()
        db.execute("INSERT INTO entries VALUES ('AniList', ?)",
                   (json.dumps({"updatedAt": 2000000000}),))
        self.assertEqual(
            update_db.resolve_anilist_watermark(db, 1900000000),
            (1900000000, "snapshot_updatedAt"),
        )

    def test_refuses_blind_bootstrap_without_updated_at(self):
        db = self.make_db()
        db.execute("INSERT INTO entries VALUES ('AniList', '{}')")
        with self.assertRaisesRegex(ValueError, "refusing an unbounded bootstrap scan"):
            update_db.resolve_anilist_watermark(db, 1900000000)


if __name__ == "__main__":
    unittest.main()
