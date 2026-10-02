#!/usr/bin/env python3
"""Automatically click and download pending Erulabo subtitle pages sequentially.

Iterates over all items in state.erulabo_pending:
1. Opens the page and centers the download button
2. Clicks the button using the virtual mouse
3. Waits for the browser download to finish (up to timeout)
4. Automatically saves the downloaded file with its original name (.zip) to disk
5. Removes the item from state.erulabo_pending only on success
6. Moves to the next item
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import time
import urllib.request
from pathlib import Path
import subprocess

STATE_FILE = Path.home() / ".local/state/anime-db-download/subtitle-queue-state.json"
PROFILE_DIR = Path.home() / ".local/share/anime-db-download/erulabo-browser-profile"
DEFAULT_DOWNLOAD_DIR = Path.home() / "anime-db-download/downloads"
DEFAULT_DISPLAY = ":99"
DEFAULT_CDP_PORT = 9222
WINDOW_SIZE = (1280, 900)
DEFAULT_TIMEOUT_SECONDS = 60  # 최대 다운로드 대기 시간 (초)


def load_state(path: Path) -> dict:
    if not path.exists():
        raise RuntimeError(f"Subtitle state does not exist: {path}")
    state = json.loads(path.read_text(encoding="utf-8"))
    pending = state.get("erulabo_pending")
    if not isinstance(pending, dict):
        raise RuntimeError("Subtitle state has no erulabo_pending map")
    return state


def save_state(path: Path, state: dict) -> None:
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    temp_path.replace(path)


def command_path(name: str) -> str:
    value = shutil.which(name)
    if not value:
        raise RuntimeError(f"Required command not found: {name}")
    return value


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


def get_pending_list(state: dict) -> list[tuple[str, dict]]:
    rows = []
    for item_id, pending in state.get("erulabo_pending", {}).items():
        item = (pending or {}).get("item") or {}
        url = str(item.get("source_url") or "").strip()
        if not url:
            continue
        rows.append((str((pending or {}).get("queued_at") or ""), item_id, item))
    rows.sort(key=lambda row: (row[0], row[1]))
    return [(item_id, item) for _, item_id, item in rows]


def process_item(page, item_id: str, item: dict, save_dir: Path, timeout_sec: int) -> bool:
    url = str(item.get("source_url") or "").strip()
    print(f"\n[>] 처리 시작: {item_id}")
    print(f"    URL: {url}")

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    except Exception as exc:
        print(f"[~] 네비게이션 경고: {exc}")

    page.wait_for_timeout(2_000)

    try:
        button = page.locator("button[data-file-url]").first
        button.wait_for(state="visible", timeout=15_000)
    except Exception:
        print(f"[!] {item_id}: 다운로드 버튼을 찾지 못했습니다. 건너뜁니다.")
        return False

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
        print(f"[!] {item_id}: 버튼 영역(Bounding Box)을 가져올 수 없습니다.")
        return False

    center_x = box["x"] + box["width"] / 2.0
    center_y = box["y"] + box["height"] / 2.0

    print(f"    파일 URL: {button.get_attribute('data-file-url')}")
    print(f"    가상 마우스 좌표: ({round(center_x)}, {round(center_y)})")

    page.mouse.move(center_x, center_y)
    page.wait_for_timeout(300)

    try:
        print(f"    [*] 마우스 클릭 전송 및 다운로드 대기 중 (최대 {timeout_sec}초)...")
        with page.expect_download(timeout=timeout_sec * 1000) as download_info:
            page.mouse.click(center_x, center_y)

        download = download_info.value
        # 브라우저 임시 이름 대신 서버가 제공한 원래 파일명(xxx.zip) 획득
        filename = download.suggested_filename

        save_dir.mkdir(parents=True, exist_ok=True)
        save_path = save_dir / filename

        # 임시 경로(/tmp/playwright-artifacts-...)에서 목적지 폴더로 온전한 파일명으로 저장
        download.save_as(save_path)

        print(f"    [+] [SUCCESS] 파일 다운로드 및 저장 성공: {save_path}")
        return True

    except Exception as exc:
        print(f"    [-] [FAIL] 다운로드 감지 실패 또는 타임아웃: {exc}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=STATE_FILE)
    parser.add_argument("--profile", type=Path, default=PROFILE_DIR)
    parser.add_argument("--download-dir", type=Path, default=DEFAULT_DOWNLOAD_DIR)
    parser.add_argument("--display", default=DEFAULT_DISPLAY)
    parser.add_argument("--cdp-port", type=int, default=DEFAULT_CDP_PORT)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()

    state = load_state(args.state)
    pending_items = get_pending_list(state)
    if not pending_items:
        print("대기 중인 Erulabo 자막 항목이 없습니다.")
        return

    print(f"총 대기 항목 수: {len(pending_items)}개")
    print(f"파일 저장 경로: {args.download_dir}")

    ensure_xvfb(args.display)
    ensure_chromium(args.display, args.profile, args.cdp_port)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright가 설치되어 있지 않습니다.") from exc

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{args.cdp_port}")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()

        for idx, (item_id, item) in enumerate(pending_items, 1):
            print(f"\n==================== [{idx}/{len(pending_items)}] ====================")
            success = process_item(
                page=page,
                item_id=item_id,
                item=item,
                save_dir=args.download_dir,
                timeout_sec=args.timeout,
            )

            if success:
                # 다운로드가 정상 완료되었을 때만 JSON에서 제거 후 즉시 파일 갱신
                state["erulabo_pending"].pop(item_id, None)
                save_state(args.state, state)
                print(f"    [v] 상태 업데이트 완료: {item_id} 큐에서 삭제됨")
            else:
                print(f"    [!] 항목 {item_id} 처리에 실패하여 큐에 유지합니다.")

            # 다음 다운로드로 넘어가기 전 안전 쿨타임 (2초)
            page.wait_for_timeout(2000)

    print("\n모든 대기 항목 처리가 끝났습니다.")


if __name__ == "__main__":
    main()
