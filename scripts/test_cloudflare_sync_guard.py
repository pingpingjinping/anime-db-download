import json
import tempfile
import unittest
from pathlib import Path
from cloudflare_sync_guard import can_skip, manifest_data, verify_counts, wrangler_rows

DIGEST = "1" * 64
MANIFEST = {"sql_sha256": DIGEST, "works": 6248, "aliases": 12688}

def record(digest=DIGEST, works=6248, aliases=12688, actual_works=6248, actual_aliases=12688):
    return {
        "sql_sha256": digest,
        "works": works,
        "aliases": aliases,
        "actual_works": actual_works,
        "actual_aliases": actual_aliases,
    }

class CloudflareSyncGuardTests(unittest.TestCase):
    def test_skip_only_identical_verified_rows(self):
        self.assertTrue(can_skip(MANIFEST, [record()]))
        self.assertFalse(can_skip(MANIFEST, []))  # no metadata on first auto run
        self.assertFalse(can_skip(MANIFEST, [record(digest="2"*64)]))
        self.assertFalse(can_skip(MANIFEST, [record(actual_works=1)]))
        self.assertFalse(can_skip(MANIFEST, [record(aliases=1)]))
        self.assertFalse(can_skip(MANIFEST, [record(),record()]))

    def test_after_publish_verify_exact_table_counts(self):
        verify_counts(MANIFEST, [{"works": 6248}, {"aliases": 12688}])
        with self.assertRaises(ValueError):
            verify_counts(MANIFEST, [{"works": 1}, {"aliases": 12688}])
        with self.assertRaises(ValueError):
            verify_counts(MANIFEST, [{"works": 6248}])
        with self.assertRaises(ValueError):
            verify_counts(MANIFEST, [{"works": 6248}, {"aliases": "12688"}])

    def test_wranger_json_is_strict_and_checks_success(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "query.json"
            p.write_text(json.dumps([
                {"results": [{"works": 6248}], "success": True},
                {"results": [{"aliases": 12688}], "success": True},
            ]))
            self.assertEqual(wrangler_rows(p), [{"works": 6248}, {"aliases": 12688}])
            p.write_text(json.dumps([{"results": [], "success": False}]))
            with self.assertRaises(ValueError):
                wrangler_rows(p)

    def test_sql_digest_must_be_64_hex_chars(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "manifest.json"
            p.write_text(json.dumps(MANIFEST))
            self.assertEqual(manifest_data(p), MANIFEST)
            p.write_text(json.dumps({**MANIFEST, "sql_sha256": "fake"}))
            with self.assertRaises(ValueError):
                manifest_data(p)


if __name__ == "__main__":
    unittest.main()
