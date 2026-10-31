import contextlib
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pi_erulabo_web_worker as worker
from pi_download_erulabo import browser_lock


class ErulaboWebWorkerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        url = "https://erulabo.com/867"
        self.directory = self.root / "jobs" / hashlib.sha256(url.encode()).hexdigest()
        self.request = {"version": 1, "url": url, "runId": str(uuid.uuid4()), "createdAt": worker.now()}
        worker.atomic_json(self.directory / "request.json", self.request)
        self.args = SimpleNamespace(profile=self.root / "profile", display=":99", cdp_port=9222, timeout=90)
        self.page = Mock()
        self.browser = Mock(contexts=[Mock()])
        self.browser.contexts[0].new_page.return_value = self.page
        self.playwright = Mock()
        self.playwright.chromium.connect_over_cdp.return_value = self.browser
        self.download = self.root / "03.srt"
        self.download.write_text("1\n00:00:01,000 --> 00:00:02,000\nsubtitle\n")
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(worker, "ensure_xvfb"))
        self.ensure = self.stack.enter_context(patch.object(worker, "ensure_chromium"))
        self.stop = self.stack.enter_context(patch.object(worker, "stop_chromium"))
        self.process = self.stack.enter_context(patch.object(worker, "process_item", return_value=self.download))

    def state(self):
        return json.loads((self.directory / "state.json").read_text())

    def test_queue_survives_restart_and_terminal_generations_are_not_repeated(self):
        self.assertEqual(worker.pending_jobs(self.root), [(self.directory, self.request)])
        worker.atomic_json(self.directory / "state.json", {"runId": self.request["runId"], "status": "downloading"})
        self.assertEqual(len(worker.pending_jobs(self.root)), 1)
        for status in ("completed", "failed"):
            worker.atomic_json(self.directory / "state.json", {"runId": self.request["runId"], "status": status})
            self.assertEqual(worker.pending_jobs(self.root), [])
        self.request["runId"] = str(uuid.uuid4())
        worker.atomic_json(self.directory / "request.json", self.request)
        self.assertEqual(len(worker.pending_jobs(self.root)), 1)

    def test_only_canonical_public_posts_with_matching_digest_are_consumed(self):
        for url in ("https://erulabo.com.evil.test/867", "http://erulabo.com/867", "https://erulabo.com/867?url=x",
                    "https://user@erulabo.com/867", "https://erulabo.com/admin", "https://[invalid"):
            request = {**self.request, "url": url}
            worker.atomic_json(self.directory / "request.json", request)
            self.assertIsNone(worker.valid_request(self.directory))
        worker.atomic_json(self.directory / "request.json", self.request)
        link = self.root / ("b" * 64)
        link.symlink_to(self.directory, target_is_directory=True)
        self.assertIsNone(worker.valid_request(link))

    def test_success_atomically_publishes_bytes_and_original_name(self):
        def download(*args, **kwargs):
            for phase in ("opening", "preparing", "downloading"):
                kwargs["progress"](phase)
                self.assertEqual(self.state()["status"], phase)
            return self.download
        self.process.side_effect = download
        worker.process_job(self.directory, self.request, self.args, self.playwright)
        self.assertEqual(self.state()["status"], "completed")
        self.assertEqual(self.state()["runId"], self.request["runId"])
        self.assertEqual(self.state()["filename"], "03.srt")
        self.assertEqual((self.directory / "payload.bin").read_bytes(), self.download.read_bytes())
        self.assertEqual(worker.pending_jobs(self.root), [])
        self.stop.assert_not_called()
        self.page.close.assert_called_once()

    def test_failure_and_page_exceptions_restart_once_then_retry(self):
        for failure in (None, RuntimeError("page closed")):
            with self.subTest(failure=failure):
                self.process.reset_mock()
                self.stop.reset_mock()
                self.process.side_effect = [failure, self.download]
                worker.process_job(self.directory, self.request, self.args, self.playwright)
                self.assertEqual(self.process.call_count, 2)
                self.stop.assert_called_once_with(self.args.profile, 9222)
                self.assertEqual(self.state()["status"], "completed")

    def test_persistent_failure_is_visible_and_waits_for_an_explicit_retry(self):
        self.process.side_effect = [None, None]
        worker.process_job(self.directory, self.request, self.args, self.playwright)
        self.assertEqual(self.state()["status"], "failed")
        self.assertEqual(worker.pending_jobs(self.root), [])
        self.assertFalse((self.directory / "payload.bin").exists())

    def test_recovering_a_killed_download_closes_the_old_browser_before_reuse(self):
        worker.atomic_json(self.directory / "state.json", {"runId": self.request["runId"], "status": "downloading"})
        events = []
        self.stop.side_effect = lambda *_: events.append("stop")
        self.ensure.side_effect = lambda *_: events.append("start")
        worker.process_job(self.directory, self.request, self.args, self.playwright)
        self.assertEqual(events[:2], ["stop", "start"])
        self.assertEqual(self.state()["status"], "completed")

    def test_wrong_attachment_types_are_not_published_as_success(self):
        self.download = self.root / "login.html"
        self.download.write_text("<html>sign in</html>")
        self.process.return_value = self.download
        worker.process_job(self.directory, self.request, self.args, self.playwright)
        self.assertEqual(self.state()["status"], "failed")
        self.assertFalse((self.directory / "payload.bin").exists())

    def test_weekly_and_web_consumers_exclude_each_other_with_the_same_profile_lock(self):
        code = "import fcntl,sys; f=open(sys.argv[1],'a');\ntry: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)\nexcept BlockingIOError: sys.exit(23)"
        filename = str(self.args.profile) + ".lock"
        with browser_lock(self.args.profile):
            self.assertEqual(subprocess.run([sys.executable, "-c", code, filename], check=False).returncode, 23)
        self.assertEqual(subprocess.run([sys.executable, "-c", code, filename], check=False).returncode, 0)

    def test_heartbeat_is_available_while_jobs_use_the_browser(self):
        stopped = threading.Event()
        thread = threading.Thread(target=worker.heartbeat, args=(self.root, stopped))
        thread.start()
        try:
            for _ in range(100):
                if (self.root / "worker.json").exists():
                    break
                stopped.wait(0.01)
            self.assertEqual(worker.read_json(self.root / "worker.json")["version"], 1)
        finally:
            stopped.set()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
