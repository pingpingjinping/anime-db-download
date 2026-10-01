#!/usr/bin/env python3
import unittest
from unittest import mock

import update_db


class FakeResponse:
    def __init__(self, rows, has_next_page):
        self.status_code = 200
        self.headers = {}
        self._payload = {
            "data": {
                "Page": {
                    "pageInfo": {"hasNextPage": has_next_page},
                    "media": rows,
                }
            }
        }

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class AniListIdIncrementTests(unittest.TestCase):
    def test_id_watermark_uses_highest_numeric_id(self):
        self.assertEqual(
            update_db.anilist_id_watermark({"100", "250", "not-an-id", "42"}),
            250,
        )

    def test_id_watermark_rejects_missing_numeric_ids(self):
        with self.assertRaisesRegex(ValueError, "no numeric source IDs"):
            update_db.anilist_id_watermark({"", "not-an-id"})

    @mock.patch.object(update_db.time, "sleep", return_value=None)
    def test_rows_stop_when_stored_id_is_reached(self, _sleep):
        response = FakeResponse(
            [{"id": 105}, {"id": 104}, {"id": 100}, {"id": 99}],
            has_next_page=True,
        )
        with mock.patch.object(update_db.requests, "post", return_value=response) as post:
            rows = list(update_db.anilist_rows(100))

        self.assertEqual([row["id"] for row in rows], [105, 104])
        self.assertEqual(post.call_count, 1)
        payload = post.call_args.kwargs["json"]
        self.assertIn("sort: [ID_DESC]", payload["query"])

    @mock.patch.object(update_db.time, "sleep", return_value=None)
    def test_rows_page_until_stored_id_is_reached(self, _sleep):
        responses = [
            FakeResponse([{"id": 105}, {"id": 104}], has_next_page=True),
            FakeResponse([{"id": 103}, {"id": 100}], has_next_page=True),
        ]
        with mock.patch.object(update_db.requests, "post", side_effect=responses) as post:
            rows = list(update_db.anilist_rows(100))

        self.assertEqual([row["id"] for row in rows], [105, 104, 103])
        self.assertEqual(post.call_count, 2)

    @mock.patch.object(update_db.time, "sleep", return_value=None)
    def test_rows_fail_closed_if_checkpoint_is_not_found(self, _sleep):
        responses = [
            FakeResponse([{"id": 105}], has_next_page=True),
            FakeResponse([{"id": 104}], has_next_page=True),
        ]
        with mock.patch.object(update_db, "ANILIST_MAX_PAGES", 2),              mock.patch.object(update_db.requests, "post", side_effect=responses):
            with self.assertRaisesRegex(ValueError, "ID scan exceeded 2 pages"):
                list(update_db.anilist_rows(100))


if __name__ == "__main__":
    unittest.main()
