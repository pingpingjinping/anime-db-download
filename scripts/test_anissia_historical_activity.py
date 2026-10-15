"""Recent list movement probes for old Anissia works; never widen baseline to all history."""
import datetime as dt
import unittest
from unittest.mock import Mock, patch

from discover_subtitles import (
    KST, discover_anissia, historical_list_activity,
    HISTORICAL_HEAD_PAGES, HISTORICAL_PROMOTION_PAGES,
)


TODAY = dt.date(2026, 10, 16)
SCAN_FLOOR = dt.datetime(2026, 10, 12, tzinfo=KST)


def historical(anime_no, **additional):
    return {"animeNo": anime_no, "status": "END",
            "endDate": "2019-07-01", "subject": f"과거작 {anime_no}",
            **additional}


def state():
    return {"discovered": {"anissia": {}}, "retry": {"anissia_api": {}},
            "bootstrap_cutoff_kst": "2026-09-25T23:59:59+09:00"}


def caption(url="https://example.test/sub.srt", updated="2026-10-15 10:00:00"):
    return {"episode": "1", "name": "제작자", "updDt": updated, "website": url}


class HistoricalActivityTests(unittest.TestCase):
    def test_recent_head_is_sampled_once_per_week(self):
        options = dict(anime_no="10", page=0, position=0,
                       previous_positions={}, previous_checks={},
                       historical_retry={}, scan_floor=SCAN_FLOOR, today=TODAY)
        self.assertTrue(historical_list_activity(historical("10"), **options))
        options["previous_checks"] = {"10": "2026-10-15"}
        self.assertFalse(historical_list_activity(historical("10"), **options))
        options["previous_checks"] = {"10": "2026-10-09"}
        self.assertTrue(historical_list_activity(historical("10"), **options))

    def test_old_work_promoted_to_top_eight_pages_is_checked(self):
        opts = dict(anime_no="42", previous_positions={"42": 100},
                    previous_checks={"42": "2026-10-15"},
                    historical_retry={}, scan_floor=SCAN_FLOOR, today=TODAY)
        self.assertTrue(historical_list_activity(historical("42"), page=4, position=15, **opts))
        self.assertFalse(historical_list_activity(historical("42"), page=4, position=95, **opts))
        self.assertFalse(historical_list_activity(
            historical("42"), page=HISTORICAL_PROMOTION_PAGES, position=15, **opts))

    def test_api_metadata_and_failed_promoted_work_get_a_second_chance(self):
        opts = dict(anime_no="42", page=100, position=1000,
                    previous_positions={}, previous_checks={},
                    historical_retry={}, scan_floor=SCAN_FLOOR, today=TODAY)
        self.assertTrue(historical_list_activity(
            historical("42", updDt="2026-10-15 12:00:00"), **opts))
        self.assertFalse(historical_list_activity(
            historical("42", updDt="2019-07-01 12:00:00"), **opts))
        opts["historical_retry"] = {"42": {"error": "timeout"}}
        self.assertTrue(historical_list_activity(historical("42"), **opts))

    def test_new_historical_caption_is_queued_and_old_caption_excluded(self):
        s = state()
        pages = [[historical("44"), {"animeNo": 45, "status": "ON",
                                      "subject": "방영작"}]]
        def fetch(_session, url):
            if "/list/" in url:
                return Mock(json=lambda: {"data": {"totalPages": 1, "content": pages[0]}})
            if url.endswith("/44"):
                return Mock(json=lambda: {"data": [caption(), caption(updated="2019-02-01 01:00:00")]})
            return Mock(json=lambda: {"data": []})
        queue = []
        with patch("discover_subtitles.request", side_effect=fetch) as req:
            info = discover_anissia(Mock(), s, queue, [], SCAN_FLOOR, 0, today=TODAY)
        self.assertEqual([i["anime_no"] for i in queue], ["44"])
        self.assertEqual(info["anissia_scanned_historical"], 1)
        self.assertEqual(s["anissia_historical_checked"]["44"], TODAY.isoformat())
        self.assertEqual(s["anissia_list_positions"], {"44": 0, "45": 1})
        self.assertEqual(sum("/animeNo/44" in c.args[1] for c in req.call_args_list), 1)

    def test_page_four_bump_queues_history_without_polling_every_other_history(self):
        s = state()
        s["anissia_list_positions"] = {"99": 900}
        s["anissia_historical_checked"] = {"99": "2026-10-15"}
        pages = [
            [{"animeNo": n, "status": "ON", "subject": str(n)}
             for n in range(page * 10 + 1, (page + 1) * 10 + 1)]
            for page in range(5)
        ]
        pages[4] = [historical("99"), historical("100")]
        def fetch(_session, url):
            if "/list/" in url:
                page = int(url.split("/list/")[1].split("?")[0])
                return Mock(json=lambda: {"data": {"totalPages": 5, "content": pages[page]}})
            return Mock(json=lambda: {"data": [caption()]})
        queue = []
        with patch("discover_subtitles.request", side_effect=fetch) as req:
            scope = discover_anissia(Mock(), s, queue, [], SCAN_FLOOR, 0, today=TODAY)
        self.assertEqual(scope["anissia_scanned_historical"], 1)
        self.assertEqual(scope["anissia_skipped_historical"], 1)
        self.assertEqual([q["anime_no"] for q in queue], ["99"])
        self.assertFalse(any("/animeNo/100" in call.args[1] for call in req.call_args_list))

    def test_broken_page_never_overwrites_previous_rank_snapshot(self):
        s = state()
        s["anissia_list_positions"] = {"old": 123}
        pages = [[{"animeNo": 1, "status": "ON"}], None]
        def fetch(_session, url):
            page = int(url.split("/list/")[1].split("?")[0])
            return Mock(json=lambda: {"data": {"totalPages": 2, "content": pages[page]}})
        with patch("discover_subtitles.request", side_effect=fetch):
            with self.assertRaisesRegex(RuntimeError, "Unexpected Anissia list page"):
                discover_anissia(Mock(), s, [], [], SCAN_FLOOR, 0, today=TODAY)
        self.assertEqual(s["anissia_list_positions"], {"old": 123})


if __name__ == "__main__":
    unittest.main()
