#!/usr/bin/env python3
"""Consume durable Erulabo web requests from the existing shared subtitle volume.

Only numeric public erulabo.com posts are accepted. No web server or CDP port is
exposed. Uses the same persistent browser and click flow as the weekly CLI.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from pi_download_erulabo import (
    PROFILE_DIR, DEFAULT_DISPLAY, DEFAULT_CDP_PORT, DEFAULT_TIMEOUT_SECONDS,
    browser_lock, ensure_xvfb, ensure_chromium, stop_chromium, process_item,
)

TERMINAL = {"completed", "failed"}
MAX_BYTES = 32 * 1024 * 1024


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def atomic_json(filename: Path, data: dict) -> None:
    filename.parent.mkdir(parents=True, exist_ok=True)
    temporary = filename.with_name(filename.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(filename)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(filename: Path) -> dict:
    try:
        if filename.is_symlink() or not filename.is_file() or filename.stat().st_size > 65536:
            return {}
        value = json.loads(filename.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (ValueError, OSError):
        return {}


def valid_request(directory: Path) -> dict | None:
    if directory.is_symlink() or not re.fullmatch(r"[a-f0-9]{64}", directory.name):
        return None
    request = read_json(directory / "request.json")
    url = request.get("url", "")
    if not isinstance(url, str) or len(url) > 256 or request.get("version") != 1:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if (parsed.scheme != "https" or parsed.netloc != "erulabo.com"
            or not re.fullmatch(r"/[1-9]\d*", parsed.path) or parsed.query or parsed.fragment):
        return None
    if hashlib.sha256(url.encode()).hexdigest() != directory.name:
        return None
    if not isinstance(request.get("runId"), str) or not re.fullmatch(r"[a-f0-9-]{36}", request["runId"]):
        return None
    return request


def pending_jobs(root: Path) -> list[tuple[Path, dict]]:
    jobs = root / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    pending = []
    for directory in jobs.iterdir():
        request = valid_request(directory)
        if not request:
            continue
        state = read_json(directory / "state.json")
        if state.get("runId") == request["runId"] and state.get("status") in TERMINAL:
            continue
        pending.append((directory, request))
    return sorted(pending, key=lambda row: row[1].get("createdAt", ""))


def process_job(directory: Path, request: dict, args, playwright) -> None:
    previous = read_json(directory / "state.json")
    recovering = previous.get("runId") == request["runId"] and previous.get("status") in {
        "opening", "preparing", "downloading", "retrying"}
    def progress(status: str, **extra):
        atomic_json(directory / "state.json", {"version": 1, "runId": request["runId"],
            "status": status, "updatedAt": now(), **extra})

    progress("queued")
    staging = directory / ("staging-" + request["runId"])
    try:
        with browser_lock(args.profile):
            if recovering:
                # A killed worker may have left its old tab downloading. No
                # other consumer owns the profile while this lock is held.
                stop_chromium(args.profile, args.cdp_port)
            ensure_xvfb(args.display)
            ensure_chromium(args.display, args.profile, args.cdp_port)
            for attempt in range(2):
                browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{args.cdp_port}")
                page = browser.contexts[0].new_page()
                try:
                    try:
                        downloaded = process_item(page, directory.name, {"source_url": request["url"]},
                            staging, args.timeout, progress=progress)
                    except Exception as error:
                        print(f"Erulabo web attempt {attempt + 1}: {type(error).__name__}", flush=True)
                        downloaded = None
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
                if downloaded:
                    downloaded = Path(downloaded)
                    filename = downloaded.name
                    if downloaded.is_symlink() or not downloaded.is_file() or downloaded.stat().st_size > MAX_BYTES:
                        raise ValueError("Invalid downloaded subtitle size or file type")
                    if not re.search(r"\.(?:zip|ass|ssa|srt|smi|vtt)$", filename, re.I) or len(filename) > 512:
                        raise ValueError("Unsupported subtitle attachment")
                    temporary = directory / ("payload-" + request["runId"] + ".tmp")
                    shutil.copyfile(downloaded, temporary)
                    temporary.replace(directory / "payload.bin")
                    progress("completed", filename=filename)
                    return
                if attempt == 0:
                    progress("retrying")
                    stop_chromium(args.profile, args.cdp_port)
                    ensure_chromium(args.display, args.profile, args.cdp_port)
            progress("failed")
    except Exception as error:
        print(f"Erulabo web job {directory.name} failed: {type(error).__name__}: {error}", flush=True)
        progress("failed")
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def heartbeat(root: Path, stopped: threading.Event) -> None:
    while not stopped.is_set():
        atomic_json(root / "worker.json", {"version": 1, "updatedAt": now()})
        stopped.wait(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=Path(os.environ.get("STREAMING_REPO", "/home/pi/streaming-project")) / "subtitle-data")
    parser.add_argument("--profile", type=Path, default=Path(os.environ.get("ERULABO_BROWSER_PROFILE", str(PROFILE_DIR))))
    parser.add_argument("--display", default=os.environ.get("ERULABO_DISPLAY", DEFAULT_DISPLAY))
    parser.add_argument("--cdp-port", type=int, default=int(os.environ.get("ERULABO_CDP_PORT", DEFAULT_CDP_PORT)))
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()
    root = args.target / ".erulabo-web"
    root.mkdir(parents=True, exist_ok=True)
    # Only one consumer may recover unfinished jobs after a process restart.
    with (root / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        from playwright.sync_api import sync_playwright
        stopped = threading.Event()
        thread = threading.Thread(target=heartbeat, args=(root, stopped), daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                while True:
                    for directory, request in pending_jobs(root):
                        process_job(directory, request, args, playwright)
                    time.sleep(2)
        finally:
            stopped.set()
            thread.join(timeout=6)
            (root / "worker.json").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
