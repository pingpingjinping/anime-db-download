import calendar
import datetime as dt
import json
import pathlib
import sqlite3
import tempfile
import unittest
import zipfile
from scripts.pi_export_reviewed_work_groups import build_manifest, is_last_day, prepare_snapshot, load_prepared_snapshot
from scripts.apply_pi_reviewed_groups import patch_archive
from scripts.check_monthly_review_overlay import check


class MonthlyReviewedGroupsTest(unittest.TestCase):
    def test_end_of_month_including_leap_day(self):
        self.assertTrue(is_last_day(dt.datetime(2028,2,29)))
        self.assertFalse(is_last_day(dt.datetime(2028,2,28)))
        self.assertTrue(is_last_day(dt.datetime(2026,2,28)))
        self.assertTrue(is_last_day(dt.datetime(2026,4,30)))

    def rows(self):
        return [{"members":["db_AniList_2","re_r1","ani_1"],
                 "title":"검수 작품 1기","kind":"main","season":1}]

    def with_manual(self, manifest, reanime=None, anikoto=None):
        reanime = reanime if reanime is not None else []
        anikoto = anikoto if anikoto is not None else []
        manifest["manual_work_matches"] = reanime
        manifest["manual_match_count"] = len(reanime)
        manifest["manual_anikoto_matches"] = anikoto
        manifest["manual_anikoto_match_count"] = len(anikoto)
        return manifest

    def empty_manual(self, _repo):
        return {"reanime": [], "anikoto": []}

    def test_sanitized_manifest_contains_no_private_paths(self):
        rows=self.rows()
        rows[0]["relative_path"]="naver_subtitles/private-user/secret-name.zip"
        rows[0]["updated_by"]="sensitive_admin"
        payload=build_manifest(rows,"2026-10")
        encoded=json.dumps(payload)
        self.assertEqual(payload["count"],1)
        self.assertNotIn("private-user",encoded)
        self.assertNotIn("updated_by",encoded)
        self.assertEqual(payload["groups"][0]["members"],
                         ["ani_1","db_AniList_2","re_r1"])

    def test_prepare_snapshot_and_first_day_publish_guard(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp)
            with mock.patch.dict("os.environ",{"ANIME_DB_GITHUB_TOKEN":"test"}), \
                 mock.patch("scripts.pi_export_reviewed_work_groups.fetch_canonical_catalog",return_value=self.make_zip(root).read_bytes()), \
                 mock.patch("scripts.pi_export_reviewed_work_groups.export_rows",return_value=self.rows()), \
                 mock.patch("scripts.pi_export_reviewed_work_groups.export_cached_posters",return_value=[]), \
                 mock.patch("scripts.pi_export_reviewed_work_groups.export_manual_work_matches",side_effect=self.empty_manual):
                manifest=prepare_snapshot(root,"2026-11",root/"stage")
                self.assertEqual(manifest["count"],1)
                staged=root/"stage"/"pi-reviewed-work-groups-2026-11.json"
                self.assertTrue(staged.exists())
                self.assertEqual(staged.stat().st_mode & 0o777,0o600)
                prepared=json.loads(staged.read_text())
                prepared["prepared_at_kst"]="2026-11-01T03:00:00+09:00"
                staged.write_text(json.dumps(prepared))
                today=dt.datetime(2026,11,1,4,0,tzinfo=dt.timezone(dt.timedelta(hours=9)))
                self.assertEqual(load_prepared_snapshot("2026-11",root/"stage",today)["count"],1)
                with self.assertRaises(RuntimeError):
                    load_prepared_snapshot("2026-10",root/"stage",today)
                with self.assertRaises(RuntimeError):
                    load_prepared_snapshot("2026-11",root/"stage",today+dt.timedelta(days=1))

    def test_prepared_snapshot_rejects_late_different_reviews(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp)
            with mock.patch.dict("os.environ",{"ANIME_DB_GITHUB_TOKEN":"test"}), \
                 mock.patch("scripts.pi_export_reviewed_work_groups.fetch_canonical_catalog",return_value=self.make_zip(root).read_bytes()), \
                 mock.patch("scripts.pi_export_reviewed_work_groups.export_rows",return_value=self.rows()), \
                 mock.patch("scripts.pi_export_reviewed_work_groups.export_cached_posters",return_value=[]), \
                 mock.patch("scripts.pi_export_reviewed_work_groups.export_manual_work_matches",side_effect=self.empty_manual):
                prepare_snapshot(root,"2026-11",root/"stage")
            changed=[{"members":["ani_1","re_different"],"title":"different","kind":"main","season":1}]
            with mock.patch("scripts.pi_export_reviewed_work_groups.export_rows",return_value=changed):
                with self.assertRaises(RuntimeError):
                    prepare_snapshot(root,"2026-11",root/"stage")

    def test_reject_conflicting_or_ambiguous_source_groups(self):
        with self.assertRaises(ValueError):
            build_manifest([{"members":["ani_1","ani_2","re_r1"],
                              "title":"same source","kind":"main","season":1}],"2026-10")
        with self.assertRaises(ValueError):
            build_manifest(self.rows()+[{"members":["ani_1","re_r2"],
                "title":"conflict","kind":"main","season":1}],"2026-10")

    def make_zip(self,root):
        db=root/"db.sqlite"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE entries(source TEXT,source_record_id TEXT,PRIMARY KEY(source,source_record_id))")
            conn.executemany("INSERT INTO entries VALUES(?,?)",[
                ("Anissia","1"),("ReAnime","r1"),("AniList","2"),("Onnada","3"),
            ])
            conn.execute("CREATE TABLE anikoto_catalog(source_record_id TEXT PRIMARY KEY,payload_json TEXT)")
            conn.execute("INSERT INTO anikoto_catalog VALUES('ak1','{}')")
        archive=root/"catalog.zip"
        with zipfile.ZipFile(archive,"w") as out: out.write(db,arcname="anime.db")
        return archive

    def test_roundtrip_release_patch_and_month_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp)
            archive=self.make_zip(root)
            manifest=root/"review.json"
            manifest.write_text(json.dumps(self.with_manual(build_manifest(self.rows(),"2026-10"))))
            count=patch_archive(archive,manifest,"2026-10")
            self.assertEqual(count,1)
            self.assertEqual(check(archive,"2026-10"),1)
            with zipfile.ZipFile(archive) as z:
                db=root/"restored.db"
                db.write_bytes(z.read("anime.db"))
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0],4)
                self.assertEqual(conn.execute("SELECT members_json FROM pi_reviewed_work_groups").fetchone()[0],
                                 json.dumps(["ani_1","db_AniList_2","re_r1"],ensure_ascii=False,separators=(",",":")))

    def test_manual_reanime_and_anikoto_roundtrip_and_reset(self):
        from scripts.apply_pi_manual_work_matches import assert_overlay
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp)
            archive=self.make_zip(root)
            manifest_path=root/"manual.json"
            ak=[{"anikoto_id":"ak1","anissia_no":1,"mapping_kind":"whole",
                 "anikoto_episode_start":None,"anikoto_episode_end":None,
                 "target_episode_start":None,"target_episode_end":None}]
            record=self.with_manual(build_manifest(self.rows(),"2026-10"),
                [{"reanime_id":"r1","anissia_no":1}],ak)
            manifest_path.write_text(json.dumps(record))
            patch_archive(archive,manifest_path,"2026-10")
            with zipfile.ZipFile(archive) as z:
                db_path=root/"verified.sqlite";db_path.write_bytes(z.read("anime.db"))
            with sqlite3.connect(db_path) as db:
                self.assertEqual(assert_overlay(db,record,"2026-10"),2)
            # Next full export includes no overrides: reset removes stale links.
            reset=self.with_manual(build_manifest(self.rows(),"2026-11"))
            manifest_path.write_text(json.dumps(reset))
            patch_archive(archive,manifest_path,"2026-11")
            with zipfile.ZipFile(archive) as z:
                db_path.write_bytes(z.read("anime.db"))
            with sqlite3.connect(db_path) as db:
                self.assertEqual(assert_overlay(db,reset,"2026-11"),0)

    def test_manual_anikoto_unknown_season_range_fails_closed(self):
        from scripts.apply_pi_manual_work_matches import checked_anikoto
        row={"anikoto_id":"ak1","anissia_no":1,"mapping_kind":"range",
             "anikoto_episode_start":13,"anikoto_episode_end":25,
             "target_episode_start":1,"target_episode_end":12}
        with self.assertRaisesRegex(ValueError,"range"):
            checked_anikoto([row])

    def test_invalid_identity_fails_before_overwriting_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp)
            archive=self.make_zip(root)
            before=archive.read_bytes()
            rows=[{"members":["ani_999","re_r1"],"title":"bad ID","kind":"main","season":1}]
            manifest=root/"review.json"
            manifest.write_text(json.dumps(self.with_manual(build_manifest(rows,"2026-10"))))
            with self.assertRaises(ValueError):
                patch_archive(archive,manifest,"2026-10")
            self.assertEqual(archive.read_bytes(),before)


if __name__=="__main__":unittest.main()
