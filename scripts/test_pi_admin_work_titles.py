import json
from contextlib import closing
import pathlib
import sqlite3
import tempfile
import unittest
import zipfile
from scripts.pi_admin_work_titles import checked_titles, apply_titles, assert_titles
from scripts.apply_pi_reviewed_groups import patch_archive, checked_manifest
from scripts.pi_export_reviewed_work_groups import build_manifest


class ManualKoreanTitlesTest(unittest.TestCase):
    def database(self):
        db=sqlite3.connect(":memory:")
        db.execute("CREATE TABLE entries(source TEXT,source_record_id TEXT,title_ko TEXT,PRIMARY KEY(source,source_record_id))")
        db.executemany("INSERT INTO entries VALUES(?,?,?)",[
            ("ReAnime","s1",""),("ReAnime","s2","원래 이름"),("AniList","2",""),
            ("Anissia","1","애니시아 이름"),("Onnada","3","")])
        db.execute("CREATE TABLE anikoto_catalog(source_record_id TEXT PRIMARY KEY,payload_json TEXT)")
        db.execute("INSERT INTO anikoto_catalog VALUES('ak1','{}')")
        db.commit()
        return db

    def test_exact_identity_and_roundtrip(self):
        db=self.database()
        rows=[{"anime_id":"re_s2","canonical_id":"re_s2","title":"새 한글 이름 2기"},
              {"anime_id":"ak_ak1","title":"독립 작품"}]
        with db: self.assertEqual(apply_titles(db,rows),2)
        assert_titles(db,rows)
        self.assertEqual(db.execute("SELECT canonical_id FROM pi_admin_work_titles WHERE anime_id='re_s2'").fetchone()[0],'re_s2')
        self.assertEqual(db.execute("SELECT title_ko FROM entries WHERE source='ReAnime' AND source_record_id='s1'").fetchone()[0],"")
        # A source refresh can be followed by applying the durable overlay again.
        db.execute("UPDATE entries SET title_ko='upstream' WHERE source_record_id='s2'")
        with db: apply_titles(db,rows)
        assert_titles(db,rows)

    def test_reject_bad_export_fields_duplicates_and_unknown_ids(self):
        for rows in [[{"anime_id":"re_s2","title":"English"}],
                     [{"anime_id":"re_s2","title":"이름","path":"secret"}],
                     [{"anime_id":"re_s2","title":"이름"}]*2]:
            with self.assertRaises(ValueError): checked_titles(rows)
        db=self.database()
        with self.assertRaises(ValueError):
            apply_titles(db,[{"anime_id":"re_s2","title":"이름"},{"anime_id":"re_missing","title":"없는 작품"}])
        self.assertEqual(db.execute("SELECT title_ko FROM entries WHERE source_record_id='s2'").fetchone()[0],"원래 이름")

    def test_monthly_zip_includes_title_only_and_existing_work_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp)
            db=self.database()
            with closing(sqlite3.connect(root/"catalog.db")) as dest: db.backup(dest)
            archive=root/"catalog.zip"
            with zipfile.ZipFile(archive,"w") as out: out.write(root/"catalog.db",arcname="catalog.db")
            manifest=build_manifest([],"2026-11")
            manifest.update(manual_work_matches=[],manual_match_count=0,
                            manual_anikoto_matches=[],manual_anikoto_match_count=0,
                            manual_work_titles=[{"anime_id":"re_s2","title":"수정한 작품 2기"}],manual_work_title_count=1)
            target=root/"manifest.json"
            target.write_text(json.dumps(manifest),encoding="utf-8")
            patch_archive(archive,target,"2026-11")
            with zipfile.ZipFile(archive) as src: (root/"restored.db").write_bytes(src.read("catalog.db"))
            with closing(sqlite3.connect(root/"restored.db")) as restored: assert_titles(restored,manifest["manual_work_titles"])
            manifest["manual_work_title_count"]=2
            with self.assertRaises(ValueError): checked_manifest(manifest,"2026-11")


if __name__=="__main__": unittest.main()
