#!/usr/bin/env python3
"""Prepare pending Erulabo subtitle pages in a persistent GUI Chromium session.

This helper is intentionally separate from pi_download_subtitle_queues.py.
The normal queue downloader records Erulabo items in state.erulabo_pending.
Run this script afterwards to open the first pending page, center its
button[data-file-url] download control in the viewport, and move the X11
pointer over the button. It does not click the button or mark the item as
downloaded.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

STATE_FILE = Path.home() / ".local/state/anime-db-download/subtitle-queue-state.json"
PROFILE_DIR = Path.home() / ".local/share/anime-db-download/erulabo-browser-profile"
DEFAULT_DISPLAY = ":99"
DEFAULT_CDP_PORT = 9222
DEFAULT_VNC_PORT = 5900
DEFAULT_NOVNC_PORT = 6080
WINDOW_SIZE = (1280, 900)


def load_state(path: Path) -> dict:
    if not path.exists():
        raise RuntimeError(f"Subtitle state does not exist: {path}")
    state = json.loads(path.read_text(encoding="utf-8"))
    pending = state.get("erulabo_pending")
    if not isinstance(pending, dict):
        raise RuntimeError("Subtitle state has no erulabo_pending map")
    return state


def command_path(name: str) -> str:
    value = shutil.which(name)
    if not value:
        raise RuntimeError(f"Required command not found: {name}")
    return value


def tcp_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def cdp_ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version",
            timeout=1.0,
        ) as response:
            payload = json.load(response)
        return bool(payload.get("webSocketDebuggerUrl"))
    except Exception:
        return False


def wait_until(predicate, timeout: float, description: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    raise RuntimeError(f"Timed out waiting for {description}")


def spawn(command: list[str], log_path: Path, *, env: dict[str, str] | None = None) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("ab", buffering=0)
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=handle,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
        close_fds=True,
    )


def display_number(display: str) -> str:
    value = display.strip()
    if not value.startswith(":"):
        raise RuntimeError(f"Unsupported X display: {display}")
    number = value[1:].split(".", 1)[0]
    if not number.isdigit():
        raise RuntimeError(f"Unsupported X display: {display}")
    return number


def ensure_xvfb(display: str) -> None:
    number = display_number(display)
    socket_path = Path(f"/tmp/.X11-unix/X{number}")
    if socket_path.exists():
        return

    xvfb = command_path("Xvfb")
    spawn(
        [
            xvfb,
            display,
            "-screen",
            "0",
            f"{WINDOW_SIZE[0]}x{WINDOW_SIZE[1]}x24",
        ],
        Path("/tmp/erulabo-xvfb.log"),
    )
    wait_until(socket_path.exists, 8, f"Xvfb {display}")


def ensure_chromium(display: str, profile: Path, cdp_port: int) -> None:
    if cdp_ready(cdp_port):
        return

    chromium = command_path("chromium")
    profile.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["DISPLAY"] = display
    spawn(
        [
            chromium,
            "--no-sandbox",
            "--disable-gpu",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={cdp_port}",
            f"--user-data-dir={profile}",
            f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}",
            "about:blank",
        ],
        Path("/tmp/erulabo-chromium.log"),
        env=env,
    )
    wait_until(lambda: cdp_ready(cdp_port), 15, "Chromium CDP")


def ensure_vnc(display: str, vnc_port: int, novnc_port: int) -> None:
    if not tcp_open(vnc_port):
        x11vnc = command_path("x11vnc")
        spawn(
            [
                x11vnc,
                "-display",
                display,
                "-localhost",
                "-rfbport",
                str(vnc_port),
                "-forever",
                "-shared",
                "-nopw",
            ],
            Path("/tmp/erulabo-x11vnc.log"),
        )
        wait_until(lambda: tcp_open(vnc_port), 8, "x11vnc")

    if not tcp_open(novnc_port):
        websockify = command_path("websockify")
        web_root = Path("/usr/share/novnc")
        if not (web_root / "vnc.html").exists():
            raise RuntimeError(f"noVNC web root not found: {web_root}")
        spawn(
            [
                websockify,
                f"--web={web_root}",
                f"127.0.0.1:{novnc_port}",
                f"127.0.0.1:{vnc_port}",
            ],
            Path("/tmp/erulabo-novnc.log"),
        )
        wait_until(lambda: tcp_open(novnc_port), 8, "noVNC websockify")


def first_pending(state: dict) -> tuple[str, dict] | None:
    rows = []
    for item_id, pending in state.get("erulabo_pending", {}).items():
        item = (pending or {}).get("item") or {}
        url = str(item.get("source_url") or "").strip()
        if not url:
            continue
        rows.append((str((pending or {}).get("queued_at") or ""), item_id, item))
    if not rows:
        return None
    rows.sort(key=lambda row: (row[0], row[1]))
    _, item_id, item = rows[0]
    return item_id, item


def move_x_pointer(display: str, page, box: dict) -> tuple[int, int] | None:
    xdotool = shutil.which("xdotool")
    if not xdotool:
        return None

    metrics = page.evaluate(
        """() => ({
            screenX: window.screenX,
            screenY: window.screenY,
            outerWidth: window.outerWidth,
            outerHeight: window.outerHeight,
            innerWidth: window.innerWidth,
            innerHeight: window.innerHeight
        })"""
    )
    left_inset = max(0.0, (metrics["outerWidth"] - metrics["innerWidth"]) / 2.0)
    top_inset = max(0.0, metrics["outerHeight"] - metrics["innerHeight"])

    x = round(metrics["screenX"] + left_inset + box["x"] + box["width"] / 2.0)
    y = round(metrics["screenY"] + top_inset + box["y"] + box["height"] / 2.0)

    env = os.environ.copy()
    env["DISPLAY"] = display
    subprocess.run(
        [xdotool, "mousemove", "--sync", str(x), str(y)],
        check=True,
        env=env,
    )
    return x, y


def prepare_pending(item_id: str, item: dict, cdp_port: int, display: str) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed in this Python environment. "
            "Install requirements-pi-subtitles.txt first."
        ) from exc

    url = str(item.get("source_url") or "").strip()
    if not url:
        raise RuntimeError(f"{item_id} has no source_url")

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{cdp_port}"
        )
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            # GUI Chromium can finish rendering a useful page even if navigation
            # reports a transient network error. Let the selector check decide.
            print(f"[~] navigation warning: {exc}")

        page.wait_for_timeout(2_000)
        button = page.locator("button[data-file-url]").first
        button.wait_for(state="visible", timeout=30_000)

        button.evaluate(
            """el => el.scrollIntoView({
                behavior: "instant",
                block: "center",
                inline: "center"
            })"""
        )
        page.wait_for_timeout(500)

        box = button.bounding_box()
        if box is None:
            raise RuntimeError("Erulabo download button has no bounding box")

        pointer = move_x_pointer(display, page, box)
        viewport_center = (
            round(box["x"] + box["width"] / 2.0),
            round(box["y"] + box["height"] / 2.0),
        )

        print(f"[+] Prepared Erulabo item: {item_id}")
        print(f"    URL: {page.url}")
        print(f"    file: {button.get_attribute('data-file-url')}")
        print(f"    button center in viewport: {viewport_center[0]},{viewport_center[1]}")
        if pointer is None:
            print("    X pointer: not moved (xdotool is not installed)")
        else:
            print(f"    X pointer: {pointer[0]},{pointer[1]}")
        print("    No click was sent.")

        # Do not call browser.close(): this browser was started independently
        # and must remain available for the manual noVNC step.


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=STATE_FILE)
    parser.add_argument("--profile", type=Path, default=PROFILE_DIR)
    parser.add_argument("--display", default=DEFAULT_DISPLAY)
    parser.add_argument("--cdp-port", type=int, default=DEFAULT_CDP_PORT)
    parser.add_argument("--vnc-port", type=int, default=DEFAULT_VNC_PORT)
    parser.add_argument("--novnc-port", type=int, default=DEFAULT_NOVNC_PORT)
    args = parser.parse_args()

    state = load_state(args.state)
    pending = first_pending(state)
    if pending is None:
        print("No pending Erulabo subtitle items.")
        return

    item_id, item = pending
    print(f"Erulabo pending: {len(state['erulabo_pending'])}")
    print(f"Preparing: {item_id}")

    ensure_xvfb(args.display)
    ensure_chromium(args.display, args.profile, args.cdp_port)
    ensure_vnc(args.display, args.vnc_port, args.novnc_port)
    prepare_pending(item_id, item, args.cdp_port, args.display)

    print()
    print(
        f"noVNC is listening on Pi localhost:{args.novnc_port}. "
        f"Use an SSH tunnel, then open /vnc.html."
    )


if __name__ == "__main__":
    main()
