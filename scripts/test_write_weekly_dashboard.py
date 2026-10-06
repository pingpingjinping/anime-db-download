#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


class WeeklyDashboardReportTest(unittest.TestCase):
    def test_mixed_run_records_downloads_failures_and_history(self):
        repo = Path(__file__).resolve().parents[1]
        script = repo / "scripts" / "write_weekly_dashboard.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before_path = root / "before.json"
            state_path = root / "state.json"
            fail_path = root / "failed.jsonl"
            output_path = root / "dashboard.json"

            before = {
                "success": {"old": {"source": "anissia", "files": ["old.srt"]}},
                "retry": {
                    "failed": {
                        "item": {
                            "id": "failed",
                            "source": "anissia",
                            "anime_title": "실패 작품",
                            "episode": 3,
                            "creator": "제작자",
                            "source_url": "https://example.com/fail",
                        }
                    }
                },
                "erulabo_pending": {
                    "erulabo": {
                        "item": {
                            "id": "erulabo",
                            "source": "anissia",
                            "anime_title": "에루라보 작품",
                            "episode": 2,
                            "creator": "에루 제작자",
                            "source_url": "https://erulabo.com/example",
                        }
                    }
                },
            }
            after = {
                "success": {
                    "old": {"source": "anissia", "files": ["old.srt"]},
                    "normal": {
                        "source": "aniall",
                        "source_url": "https://aniall.tistory.com/123",
                        "item": {
                            "id": "normal",
                            "source": "aniall",
                            "anime_title": "새 작품",
                            "episode": 1,
                        },
                        "files": ["aniall_subtitles/새 작품/a.zip"],
                        "completed_at": "2026-10-06T04:01:00+09:00",
                    },
                    "erulabo": {
                        "files": [
                            {"path": "anissia_subtitles/에루라보 작품/2화/a.smi"},
                            {"path": "anissia_subtitles/에루라보 작품/2화/b.ass"},
                        ],
                        "verification": {"work_title": "에루라보 작품"},
                    },
                },
                "retry": {
                    "failed": {
                        "item": before["retry"]["failed"]["item"],
                        "reason": "network",
                        "last_error": "boom",
                        "last_attempt": "2026-10-06T04:02:00+09:00",
                    }
                },
                "waiting_url": {},
                "erulabo_pending": {},
            }

            before_path.write_text(json.dumps(before, ensure_ascii=False), encoding="utf-8")
            state_path.write_text(json.dumps(after, ensure_ascii=False), encoding="utf-8")
            fail_path.write_text(
                json.dumps(
                    {
                        "id": "failed",
                        "source": "anissia",
                        "source_url": "https://example.com/fail",
                        "reason": "network",
                        "error": "boom",
                        "attempted_at": "2026-10-06T04:02:00+0900",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            output_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "runs": [{"id": "older", "finishedAt": "2026-09-29T04:00:00+09:00"}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            subprocess.run(
                [
                    "python3",
                    str(script),
                    "--state",
                    str(state_path),
                    "--before-state",
                    str(before_path),
                    "--failure-log",
                    str(fail_path),
                    "--output",
                    str(output_path),
                    "--started-at",
                    "2026-10-06T04:00:00+09:00",
                    "--finished-at",
                    "2026-10-06T04:05:00+09:00",
                    "--exit-code",
                    "0",
                ],
                check=True,
            )

            report = json.loads(output_path.read_text(encoding="utf-8"))
            run = report["runs"][0]
            self.assertEqual(run["status"], "partial")
            self.assertEqual(run["downloadedItems"], 2)
            self.assertEqual(run["downloadedFiles"], 3)
            self.assertEqual(run["failedItems"], 1)
            self.assertEqual(run["retryItems"], 1)
            self.assertEqual(len(report["runs"]), 2)
            by_id = {row["id"]: row for row in run["downloads"]}
            self.assertEqual(by_id["normal"]["title"], "새 작품")
            self.assertEqual(by_id["erulabo"]["title"], "에루라보 작품")
            self.assertEqual(len(by_id["erulabo"]["files"]), 2)
            self.assertEqual(run["failures"][0]["title"], "실패 작품")


if __name__ == "__main__":
    unittest.main()
