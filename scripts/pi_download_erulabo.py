#!/usr/bin/env python3
"""Automatically click and download pending Erulabo subtitle pages sequentially.

Iterates over all items in state.erulabo_pending:
1. Navigates to the source URL in Chromium (DISPLAY=:99)
2. Locates the button[data-file-url] and scrolls it into view
3. Moves virtual mouse cursor to the button's exact center and sends mouse click
4. Waits for the browser download event to complete (up to timeout)
5. Saves the file with its original name (.zip) to ~/anime-db-download/downloads
6. Removes the completed item from state.json
7. Proceeds to the next queue item
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
DEFAULT_TIMEOUT_SECONDS = 60


def load_state(path: Path) -> dict:
    if not path.exists():
        raise RuntimeError(f"상태 파일이 존재하지 않습니다: {path}")
    state = json.loads(path.read_text(encoding="utf-8"))
    pending = state.get("erulabo_pending")
    if not isinstance(pending, dict):
        raise RuntimeError("상태 파일에 'erulabo_pending' 맵이 없습니다.")
    return state


def save_state(path: Path, state: dict) -> None:
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    temp_path.replace(path)


def command_path(name: str) -> str:
    value = shutil.which(name)
    if not value:
        raise RuntimeError(f"필수 명령어를 찾을 수 없습니다: {name}")
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
    raise RuntimeError(f"{description} 대기 시간 초과 ({timeout}초)")


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
        raise RuntimeError(f"지원하지 않는 X 디스플레이 형식: {display}")
    number = value[1:].split(".", 1)[0]
    if not number.isdigit():
        raise RuntimeError(f"지원하지 않는 X 디스플레이 형식: {display}")
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
        print(f"[!] {item_id}: 다운로드 버튼을 찾지 못했습니다.")
        return False

    # 뷰포트 중앙으로 스크롤 정렬
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
        print(f"[!] {item_id}: 버튼 좌표(Bounding Box)를 읽을 수 없습니다.")
        return False

    center_x = box["x"] + box["width"] / 2.0
    center_y = box["y"] + box["height"] / 2.0

    print(f"    버튼 파일 속성: {button.get_attribute('data-file-url')}")
    print(f"    가상 마우스 목표 좌표: ({round(center_x)}, {round(center_y)})")

    try:
        print(f"    [*] 가상 마우스 클릭 및 다운로드 대기 중 (최대 {timeout_sec}초)...")
        with page.expect_download(timeout=timeout_sec * 1000) as download_info:
            # 1. 가상 마우스 커서를 버튼 중심 위치로 이동
            page.mouse.move(center_x, center_y)
            page.wait_for_timeout(200)

            # 2. 순수 가상 마우스 좌클릭 신호 전송 (JS 코드 사용 X)
            page.mouse.down()
            page.wait_for_timeout(100)
            page.mouse.up()
            print("    [+] 가상 마우스 클릭 신호 전송 완료")

        download = download_info.value
        filename = download.suggested_filename

        save_dir.mkdir(parents=True, exist_ok=True)
        save_path = save_dir / filename

        # 원래 파일명(.zip)으로 다운로드 폴더에 저장
        download.save_as(save_path)

        print(f"    [+] [성공] 파일 다운로드 및 저장 완료: {save_path}")
        return True

    except Exception as exc:
        print(f"    [-] [실패] 다운로드 에러 또는 타임아웃: {exc}")
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
    print(f"저장 대상 폴더: {args.download_dir}")

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
                # 다운로드가 성공한 경우에만 JSON 상태 파일에서 삭제
                state["erulabo_pending"].pop(item_id, None)
                save_state(args.state, state)
                print(f"    [v] 상태 업데이트: {item_id} 삭제 완료")
            else:
                print(f"    [!] {item_id} 처리 실패로 큐에 보존합니다.")

            # 다음 항목으로 넘어가기 전 2초 대기
            page.wait_for_timeout(2000)

    print("\n모든 대기열 처리가 완료되었습니다.")


if __name__ == "__main__":
    main()
