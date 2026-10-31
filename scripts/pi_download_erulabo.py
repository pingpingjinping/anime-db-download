#!/usr/bin/env python3
"""Automatically click and finalize pending Erulabo subtitle downloads.

Workflow:
1. Opens pending items sequentially in persistent Chromium.
2. Centers the download button and executes virtual mouse click.
3. Downloads the file to a temporary staging folder.
4. Moves the file into the canonical Anissia folder structure.
5. Records metadata under state['success'] and removes it from state['erulabo_pending'].
6. Saves state.json.
7. After the first pass, restarts Chromium once and retries only failed items.
"""
from __future__ import annotations

import argparse
import fcntl
from contextlib import contextmanager
import json
import os
import shutil
import signal
import time
import urllib.request
from pathlib import Path
import subprocess

# 기존 프로젝트 모듈 import
from pi_download_subtitle_queues import append_failure, safe_name, save_state
from subtitle_queue_metadata import build_download_metadata, queue_cache_cleanup
from subtitle_work_storage import work_directory, register_sources, preserve_download, track_download_writes
from anissia_zip_storage import prepare_anissia_files

STATE_FILE = Path.home() / ".local/state/anime-db-download/subtitle-queue-state.json"
PROFILE_DIR = Path.home() / ".local/share/anime-db-download/erulabo-browser-profile"
DEFAULT_TARGET = Path("/home/pi/streaming-project/subtitle-data")
DEFAULT_STAGING_DIR = Path("/tmp/erulabo-staging-downloads")
DEFAULT_DISPLAY = ":99"
DEFAULT_CDP_PORT = 9222
WINDOW_SIZE = (1280, 900)
DEFAULT_TIMEOUT_SECONDS = 90


def load_state(path: Path) -> dict:
    if not path.exists():
        raise RuntimeError(f"상태 파일이 존재하지 않습니다: {path}")
    state = json.loads(path.read_text(encoding="utf-8"))
    pending = state.get("erulabo_pending")
    if not isinstance(pending, dict):
        raise RuntimeError("상태 파일에 'erulabo_pending' 맵이 없습니다.")
    return state


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
    wait_until(lambda: cdp_ready(cdp_port), 60, "Chromium CDP")


def chromium_pids(profile: Path, cdp_port: int) -> list[int]:
    """Find only the main Chromium process using this exact profile and port."""
    required = {
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={cdp_port}",
    }
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            arguments = entry.joinpath("cmdline").read_bytes().decode().split("\0")
        except (OSError, UnicodeError):
            continue
        if required.issubset(arguments) and not any(arg.startswith("--type=") for arg in arguments):
            pids.append(int(entry.name))
    return pids


def stop_chromium(profile: Path, cdp_port: int) -> None:
    pids = chromium_pids(profile, cdp_port)
    if not pids and cdp_ready(cdp_port):
        raise RuntimeError("CDP 포트의 크롬이 지정한 자막 프로필과 일치하지 않습니다.")

    def stopped() -> bool:
        return not chromium_pids(profile, cdp_port) and not cdp_ready(cdp_port)

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        wait_until(stopped, 10, "Erulabo Chromium 종료")
    except RuntimeError:
        # Re-read exact process arguments before escalating; never kill by name.
        for pid in chromium_pids(profile, cdp_port):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        wait_until(stopped, 5, "Erulabo Chromium 강제 종료")


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


def erulabo_destination(item: dict, target: Path) -> Path:
    return (
        work_directory(item, target)
        / safe_name(f"{item.get('episode') or '0'}화")
        / safe_name(item.get("creator") or "unknown")
    )


def collision_safe_path(path: Path) -> Path:
    """같은 파일명이 이미 있으면 덮어쓰지 않고 (1), (2)... 추가."""
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for n in range(1, 1000):
        candidate = path.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Too many duplicate filenames: {path}")


def finalize_erulabo_download(
    *,
    state: dict,
    state_path: Path,
    item_id: str,
    item: dict,
    downloaded_file: Path,
    target: Path,
) -> Path:
    """Erulabo 임시 다운로드 파일을 기존 Anissia 구조로 이동하고

    일반 downloader와 동일한 success metadata를 기록한다.
    """
    downloaded_file = downloaded_file.resolve()
    if not downloaded_file.is_file():
        raise RuntimeError(f"Downloaded file not found: {downloaded_file}")

    destination = erulabo_destination(item, target)
    destination.mkdir(parents=True, exist_ok=True)
    # Reuse an existing hash anywhere in this work, removing only the new copy.
    with track_download_writes() as writes:
        final_path = preserve_download(downloaded_file, destination / downloaded_file.name)
        files = prepare_anissia_files([final_path], writes)

    # 2. 메타데이터 구성 및 success 기록
    metadata = build_download_metadata(
        item,
        files,
        target,
    )
    register_sources(item, files, target)
    queue_cache_cleanup(item, files, target)
    state.setdefault("success", {})[item_id] = {
        **metadata,
        "source": item.get("source"),
        "source_url": item.get("source_url"),
        "item": item,
        "new_files": [path.relative_to(target).as_posix() for path in files if path in writes.created],
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }

    # 3. 마지막으로 pending 삭제 및 상태 디스크 동기화
    state.get("erulabo_pending", {}).pop(item_id, None)
    save_state(state_path, state)

    return files[0]


@contextmanager
def browser_lock(profile: Path):
    """The weekly CLI, web worker and weekly cleanup share this browser lock."""
    profile.parent.mkdir(parents=True, exist_ok=True)
    with Path(str(profile) + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def process_item(page, item_id: str, item: dict, staging_dir: Path, timeout_sec: int,
                 progress=None) -> Path | None:
    url = str(item.get("source_url") or "").strip()
    print(f"\n[>] 처리 시작: {item_id}")
    print(f"    URL: {url}")
    if progress:
        progress("opening")

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    except Exception as exc:
        print(f"[~] 네비게이션 경고: {exc}")

    # 페이지 기본 렌더링 대기
    page.wait_for_timeout(5_000)

    try:
        button = page.locator("button[data-file-url]").first
        button.wait_for(state="visible", timeout=20_000)
    except Exception:
        print(f"[!] {item_id}: 다운로드 버튼을 찾지 못했습니다.")
        return None

    # 1. 가운데 정렬 이동
    if progress:
        progress("preparing")
    button.evaluate(
        """el => el.scrollIntoView({
            behavior: "instant",
            block: "center",
            inline: "center"
        })"""
    )

    # 2. 정렬 후 12초 대기
    print("    [*] 가운데 정렬 완료 -> 12초 대기 중...")
    page.wait_for_timeout(12_000)

    box = button.bounding_box()
    if box is None:
        print(f"[!] {item_id}: 버튼 좌표를 읽을 수 없습니다.")
        return None

    center_x = box["x"] + box["width"] / 2.0
    center_y = box["y"] + box["height"] / 2.0

    print(f"    버튼 파일 속성: {button.get_attribute('data-file-url')}")
    print(f"    가상 마우스 목표 좌표: ({round(center_x)}, {round(center_y)})")

    # 3. 커서 이동 후 8초 대기
    print("    [*] 커서 이동 중...")
    page.mouse.move(center_x, center_y)
    print("    [*] 커서 안착 완료 -> 클릭 대기 8초 중...")
    page.wait_for_timeout(8_000)

    # 4. 1회 클릭 및 임시 디렉토리로 다운로드 수신
    try:
        if progress:
            progress("downloading")
        print(f"    [*] 마우스 1회 클릭 및 다운로드 대기 중 (최대 {timeout_sec}초)...")
        with page.expect_download(timeout=timeout_sec * 1000) as download_info:
            page.mouse.click(center_x, center_y)
            print("    [+] 1회 클릭 전송 완료!")

        download = download_info.value
        filename = download.suggested_filename

        staging_dir.mkdir(parents=True, exist_ok=True)
        temp_download_path = staging_dir / filename

        # 1차 임시 저장
        download.save_as(temp_download_path)
        print(f"    [+] 1차 임시 다운로드 수신 완료: {temp_download_path}")
        return temp_download_path

    except Exception as exc:
        print(f"    [-] [실패] 다운로드 에러 또는 타임아웃: {exc}")
        return None


def download_pass(page, items: list[tuple[str, dict]], state: dict, args) -> list[tuple[str, dict]]:
    failed = []
    for idx, (item_id, item) in enumerate(items, 1):
        print(f"\n==================== [{idx}/{len(items)}] ====================")
        try:
            downloaded_temp_file = process_item(
                page=page,
                item_id=item_id,
                item=item,
                staging_dir=args.staging_dir,
                timeout_sec=args.timeout,
            )
        except Exception as exc:
            # A closed page or another browser error must not skip later items.
            print(f"[!] {item_id}: 브라우저 처리 오류: {exc}")
            downloaded_temp_file = None

        if downloaded_temp_file:
            # Finalization errors propagate: a moved file must not be downloaded again.
            final_path = finalize_erulabo_download(
                state=state,
                state_path=args.state,
                item_id=item_id,
                item=item,
                downloaded_file=downloaded_temp_file,
                target=args.target,
            )
            print("[+] [성공] Erulabo 자막 반영 완료")
            print(f"    파일: {final_path}")
            print("    상태: success 기록 + erulabo_pending 삭제")
        else:
            failed.append((item_id, item))
            print(f"[!] {item_id} 다운로드 실패: 큐에 그대로 보존합니다.")

        # Keep the cooldown working even when the page/browser disconnected.
        time.sleep(5)
    return failed


def record_final_failures(
    items: list[tuple[str, dict]],
    error: str = "Erulabo download failed after browser restart retry",
) -> None:
    for _, item in items:
        append_failure(
            item,
            error,
            "erulabo_download_failed",
        )


def download_pending(playwright, state: dict, args, pending_items: list[tuple[str, dict]]) -> None:
    browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{args.cdp_port}")
    context = browser.contexts[0]
    page = context.pages[0] if context.pages else context.new_page()
    print("\n=== Erulabo 첫 순회 ===")
    failed = download_pass(page, pending_items, state, args)
    if not failed:
        return

    print(f"\n=== 첫 순회 완료: 실패 {len(failed)}개, 크롬 재시작 후 1회 재시도 ===")
    try:
        stop_chromium(args.profile, args.cdp_port)
        ensure_chromium(args.display, args.profile, args.cdp_port)
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{args.cdp_port}")
        # Use a fresh tab even if Chromium restores the previous session's tabs.
        page = browser.contexts[0].new_page()
    except Exception as exc:
        record_final_failures(failed, f"Erulabo browser restart failed: {exc}")
        raise

    remaining = download_pass(page, failed, state, args)
    record_final_failures(remaining)
    print(f"\n재시도 결과: 성공 {len(failed) - len(remaining)}개, 대기 유지 {len(remaining)}개")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=STATE_FILE)
    parser.add_argument("--profile", type=Path, default=PROFILE_DIR)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--staging-dir", type=Path, default=DEFAULT_STAGING_DIR)
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
    print(f"최종 자막 데이터 폴더: {args.target}")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright가 설치되어 있지 않습니다.") from exc

    with browser_lock(args.profile):
        ensure_xvfb(args.display)
        ensure_chromium(args.display, args.profile, args.cdp_port)
        with sync_playwright() as playwright:
            download_pending(playwright, state, args, pending_items)

    print("\n모든 대기열 처리가 완료되었습니다.")


if __name__ == "__main__":
    main()
