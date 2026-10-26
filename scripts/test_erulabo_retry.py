#!/usr/bin/env python3
"""Exercise the two download passes without Chromium, network or delays."""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import signal
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pi_download_erulabo as erulabo
import pi_download_subtitle_queues as queues
import write_weekly_dashboard as dashboard


class ErulaboRetryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.args = SimpleNamespace(
            state=root / "state.json", profile=root / "profile", target=root / "target",
            staging_dir=root / "staging", display=":99", cdp_port=9222, timeout=90,
        )
        self.items = [
            (name, {"id": name, "source": "anissia", "source_url": f"https://erulabo.com/{name}"})
            for name in ("a", "b", "c")
        ]
        self.state = {"success": {}, "erulabo_pending": {
            key: {"item": item} for key, item in self.items
        }}
        self.page = Mock(name="first_page")
        self.retry_page = Mock(name="retry_page")
        self.browser = Mock(contexts=[Mock(pages=[self.page])])
        self.retry_context = Mock()
        self.retry_context.new_page.return_value = self.retry_page
        self.retry_browser = Mock(contexts=[self.retry_context])
        self.playwright = Mock()
        self.playwright.chromium.connect_over_cdp.side_effect = [self.browser, self.retry_browser]
        self.events = []
        self.process = self.enter_patch("process_item")
        self.finalize = self.enter_patch("finalize_erulabo_download")
        self.finalize.side_effect = self.finalize_success
        self.stop = self.enter_patch("stop_chromium")
        self.stop.side_effect = lambda *args: self.events.append("stop")
        self.ensure = self.enter_patch("ensure_chromium")
        self.ensure.side_effect = lambda *args: self.events.append("start")
        self.failure = self.enter_patch("append_failure")
        self.enter_patch("time.sleep")
        # Keep unittest output compact while production still logs every attempt.
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def enter_patch(self, name):
        mocked = patch(f"pi_download_erulabo.{name}")
        result = mocked.start()
        self.addCleanup(mocked.stop)
        return result

    def finalize_success(self, **kwargs):
        key = kwargs["item_id"]
        self.state["success"][key] = {"item": kwargs["item"], "files": [f"{key}.zip"]}
        self.state["erulabo_pending"].pop(key)
        erulabo.save_state(self.args.state, self.state)
        return self.args.target / f"{key}.zip"

    def run_downloads(self):
        erulabo.download_pending(self.playwright, self.state, self.args, self.items)

    def test_retries_only_failures_after_finishing_first_pass_and_restart(self):
        def process(**kwargs):
            key = kwargs["item_id"]
            retry = kwargs["page"] is self.retry_page
            self.events.append(f"{key}:{'retry' if retry else 'first'}")
            return Path("download.zip") if retry or key == "b" else None

        self.process.side_effect = process
        self.run_downloads()
        self.assertEqual(self.events, [
            "a:first", "b:first", "c:first", "stop", "start", "a:retry", "c:retry",
        ])
        self.assertEqual(set(self.state["success"]), {"a", "b", "c"})
        self.assertEqual(self.state["erulabo_pending"], {})
        self.assertEqual(json.loads(self.args.state.read_text())["erulabo_pending"], {})
        self.assertEqual(self.finalize.call_count, 3)
        self.failure.assert_not_called()
        self.stop.assert_called_once_with(self.args.profile, self.args.cdp_port)
        self.ensure.assert_called_once_with(self.args.display, self.args.profile, self.args.cdp_port)

    def test_successful_first_pass_does_not_restart(self):
        self.process.return_value = Path("download.zip")
        self.run_downloads()
        self.assertEqual(self.process.call_count, 3)
        self.assertEqual(self.finalize.call_count, 3)
        self.stop.assert_not_called()
        self.ensure.assert_not_called()
        self.failure.assert_not_called()
        self.playwright.chromium.connect_over_cdp.assert_called_once()

    def test_persistent_failure_is_logged_once_and_remains_pending(self):
        self.process.side_effect = [None, Path("b.zip"), Path("c.zip"), None]
        self.run_downloads()
        self.assertEqual(self.process.call_count, 4)
        self.assertEqual(set(self.state["success"]), {"b", "c"})
        self.assertEqual(set(self.state["erulabo_pending"]), {"a"})
        self.failure.assert_called_once_with(
            self.items[0][1], "Erulabo download failed after browser restart retry",
            "erulabo_download_failed",
        )

    def test_browser_exception_does_not_skip_remaining_items(self):
        self.process.side_effect = [RuntimeError("Page closed"), Path("b.zip"), Path("c.zip"), Path("a.zip")]
        self.run_downloads()
        self.assertEqual([c.kwargs["item_id"] for c in self.process.call_args_list], ["a", "b", "c", "a"])
        self.assertEqual(self.state["erulabo_pending"], {})
        self.failure.assert_not_called()

    def test_dashboard_includes_retry_success_and_only_final_failure(self):
        root = Path(self.temp.name)
        before = root / "before.json"
        before.write_text(json.dumps(self.state))
        fail_log = root / "failed.jsonl"
        report = root / "dashboard.json"
        started = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat()
        self.process.side_effect = [None, Path("b.zip"), None, Path("a.zip"), None]
        self.failure.side_effect = queues.append_failure
        with patch.object(queues, "FAIL_LOG", fail_log):
            self.run_downloads()
        finished = dt.datetime.now(dt.timezone.utc).isoformat()
        with patch("sys.argv", [
            "write_weekly_dashboard.py", "--state", str(self.args.state),
            "--before-state", str(before), "--failure-log", str(fail_log),
            "--output", str(report), "--started-at", started,
            "--finished-at", finished, "--exit-code", "0",
        ]):
            dashboard.main()
        run = json.loads(report.read_text())["runs"][0]
        self.assertEqual(run["downloadedItems"], 2)
        self.assertEqual({row["id"] for row in run["downloads"]}, {"a", "b"})
        self.assertEqual(run["failedItems"], 1)
        self.assertEqual([row["id"] for row in run["failures"]], ["c"])
        self.assertEqual(run["erulaboPendingItems"], 1)

    def test_restart_error_preserves_queue_and_logs_final_failures(self):
        self.process.side_effect = [None, Path("b.zip"), None]
        self.stop.side_effect = RuntimeError("Cannot restart")
        with self.assertRaisesRegex(RuntimeError, "Cannot restart"):
            self.run_downloads()
        self.assertEqual(set(self.state["erulabo_pending"]), {"a", "c"})
        self.assertEqual([c.args[0]["id"] for c in self.failure.call_args_list], ["a", "c"])
        self.ensure.assert_not_called()
        self.assertEqual(self.process.call_count, 3)

    def test_finalization_error_does_not_redownload_already_moved_file(self):
        self.process.return_value = Path("download.zip")
        self.finalize.side_effect = OSError("disk full")
        with self.assertRaisesRegex(OSError, "disk full"):
            self.run_downloads()
        self.process.assert_called_once()
        self.stop.assert_not_called()


class ChromiumShutdownTest(unittest.TestCase):
    def test_matches_exact_profile_and_port_excluding_child_processes(self):
        profile = Path("/tmp/erulabo-profile")
        entries = []
        argument_lists = [
            ["chromium", "--user-data-dir=/tmp/erulabo-profile", "--remote-debugging-port=9222"],
            ["chromium", "--user-data-dir=/tmp/erulabo-profile-other", "--remote-debugging-port=9222"],
            ["chromium", "--user-data-dir=/tmp/erulabo-profile", "--remote-debugging-port=9333"],
            ["chromium", "--user-data-dir=/tmp/erulabo-profile", "--remote-debugging-port=9222", "--type=renderer"],
        ]
        for pid, arguments in enumerate(argument_lists, 100):
            entry = Mock()
            entry.name = str(pid)
            entry.joinpath.return_value.read_bytes.return_value = "\0".join(arguments).encode()
            entries.append(entry)
        with patch.object(Path, "iterdir", return_value=iter(entries)):
            self.assertEqual(erulabo.chromium_pids(profile, 9222), [100])

    def test_unrelated_cdp_browser_is_not_killed(self):
        with patch.object(erulabo, "chromium_pids", return_value=[]), \
             patch.object(erulabo, "cdp_ready", return_value=True), \
             patch.object(erulabo.os, "kill") as kill:
            with self.assertRaisesRegex(RuntimeError, "프로필과 일치"):
                erulabo.stop_chromium(Path("/tmp/profile"), 9222)
            kill.assert_not_called()

    def test_termination_waits_for_both_process_and_cdp_shutdown(self):
        with patch.object(erulabo, "chromium_pids", side_effect=[[123], [], [], []]), \
             patch.object(erulabo, "cdp_ready", side_effect=[True, False, False]), \
             patch.object(erulabo.os, "kill") as kill, \
             patch.object(erulabo, "wait_until") as wait:
            def check(predicate, *_):
                self.assertFalse(predicate())  # Process exited; CDP is still listening.
                self.assertTrue(predicate())
            wait.side_effect = check
            erulabo.stop_chromium(Path("/tmp/profile"), 9222)
            kill.assert_called_once_with(123, signal.SIGTERM)

    def test_force_stop_rechecks_matching_pids_after_grace_period(self):
        with patch.object(erulabo, "chromium_pids", side_effect=[[123], [124]]), \
             patch.object(erulabo.os, "kill") as kill, \
             patch.object(erulabo, "wait_until", side_effect=[RuntimeError("timeout"), None]):
            erulabo.stop_chromium(Path("/tmp/profile"), 9222)
            self.assertEqual(kill.call_args_list, [call(123, signal.SIGTERM), call(124, signal.SIGKILL)])


if __name__ == "__main__":
    unittest.main()
