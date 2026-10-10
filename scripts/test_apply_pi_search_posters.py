import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.apply_pi_search_posters import patch_archive, validate


class PiCachedArtworkPatchTests(unittest.TestCase):
    def source_zip(self,root):
        database=root/"catalog.db"
        with sqlite3.connect(database) as sql:
            sql.execute("CREATE TABLE entries(source TEXT,source_record_id TEXT,PRIMARY KEY(source,source_record_id))")
            sql.executemany("INSERT INTO entries VALUES (?,?)", [
                ("Anissia","1"),("ReAnime","toradora"),("AniList","300"),("Onnada","10")
            ])
        archive=root/"anime.zip"
        with zipfile.ZipFile(archive,"w") as zf:
            zf.write(database,arcname="catalog.db")
        return archive

    def test_preserves_existing_catalog_and_adds_exact_identity_poster(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            archive=self.source_zip(root)
            number=patch_archive(archive,[
                {"work_id":"re_toradora","poster":"https://cdn.example/re.jpg","source":"ReAnime"},
                {"work_id":"db_Onnada_10","poster":"https://cdn.example/a.jpg","source":"AniList"},
                {"work_id":"db_Onnada_999","poster":"https://cdn.example/absent.jpg","source":"AniList"},
            ])
            self.assertEqual(number,2)
            with zipfile.ZipFile(archive) as zf:
                (root/"restored.db").write_bytes(zf.read("catalog.db"))
            with sqlite3.connect(root/"restored.db") as sql:
                self.assertEqual(sql.execute("SELECT COUNT(*) FROM entries").fetchone()[0],4)
                self.assertEqual(sql.execute("SELECT COUNT(*) FROM pi_search_poster_cache").fetchone()[0],2)
                self.assertEqual(sql.execute("SELECT poster FROM pi_search_poster_cache WHERE work_id='re_toradora'").fetchone()[0],
                                 "https://cdn.example/re.jpg")

    def test_rejects_non_https_poster_without_overwriting_catalog(self):
        with tempfile.TemporaryDirectory() as d:
            archive=self.source_zip(Path(d))
            original=archive.read_bytes()
            for url in ["file:///tmp/private.jpg","http://example.com/1.jpg",
                        "https://user:pass@example.com/1.jpg","javascript:alert(1)"]:
                with self.assertRaises(ValueError):
                    patch_archive(archive,[{"work_id":"re_toradora","poster":url,"source":"ReAnime"}])
                self.assertEqual(archive.read_bytes(),original)

    def test_rejects_duplicate_or_private_fields(self):
        with self.assertRaises(ValueError):
            validate([{"work_id":"ani_1","poster":"https://cdn.example/x.jpg",
                       "source":"AniList","relative_path":"private"}])
        with self.assertRaises(ValueError):
            validate([
                {"work_id":"ani_1","poster":"https://cdn.example/x.jpg","source":"AniList"},
                {"work_id":"ani_1","poster":"https://cdn.example/y.jpg","source":"AniList"},
            ])


if __name__=="__main__":
    unittest.main()
