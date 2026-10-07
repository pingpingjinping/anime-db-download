from __future__ import annotations

import base64
import shutil
import time
from pathlib import PurePosixPath

from winpng_decode import EmbeddedFile

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

VIEWER_READY = """() => (
    typeof window.downloadZip === 'function'
    && !!document.getElementById('viewFileList')
)"""

SETUP = """() => {
    window.__wp = { failed: false };
    window.alert = () => { window.__wp.failed = true; };
    window.prompt = () => null;
    window.confirm = () => false;
}"""

CANDIDATES = """() => {
    const seen = new Set(), out = [];
    const nodes = document.querySelectorAll(
        '#content img, .tt_article_useless_p_margin img, .contents_style img'
    );
    nodes.forEach((img, i) => {
        const src = img.src || '';
        if (!src.split('?')[0].toLowerCase().endsWith('.png') || seen.has(src)) return;
        seen.add(src);
        const key = 'wp-' + i;
        img.setAttribute('data-wp-key', key);
        out.push({ key, src });
    });
    return out;
}"""

CLICK = """(key) => {
    window.__wp.failed = false;
    const list = document.getElementById('viewFileList');
    if (list) list.innerHTML = '';
    const img = document.querySelector('img[data-wp-key="' + key + '"]');
    if (!img) return false;
    img.click();
    return true;
}"""

STATE = """() => {
    if (window.__wp && window.__wp.failed) return 'failed';
    const root = document.getElementById('winPNG');
    if (root && root.classList.contains('progress')) return 'wait';
    const entries = document.querySelectorAll('#viewFileList a[data-href]');
    if (!entries.length) return 'wait';
    if (document.querySelector('#viewFileList a.processing')) return 'wait';
    return 'done';
}"""

COLLECT = """async () => {
    const b64 = async (url) => {
        if (!url) return null;
        try {
            const buf = new Uint8Array(await (await fetch(url)).arrayBuffer());
            let s = '';
            for (let i = 0; i < buf.length; i += 0x8000) {
                s += String.fromCharCode(...buf.subarray(i, i + 0x8000));
            }
            return btoa(s);
        } catch (e) {
            return null;
        }
    };

    const out = [];
    for (const a of document.querySelectorAll('#viewFileList a[data-href]')) {
        const dirSpan = a.querySelector(':scope > span:not(.file)');
        const nested = [...a.querySelectorAll('a')];
        const assA = nested.find((x) => x.download && x.download.toLowerCase().endsWith('.ass'));
        const smiA = nested.find((x) => x.download && x.download.toLowerCase().endsWith('.smi'));
        out.push({
            dir: dirSpan ? dirSpan.textContent : '',
            name: a.download || '',
            assName: assA ? assA.download : null,
            ass: await b64(a.getAttribute('data-ass')),
            smiName: smiA ? smiA.download : null,
            smi: await b64(a.getAttribute('data-smi')),
        });
    }
    return out;
}"""


def _safe_relative(value: str) -> str | None:
    normalized = str(value or "").replace("\\", "/").strip("/ \t\r\n")
    if not normalized or "\x00" in normalized:
        return None
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        return None
    return path.as_posix()


def _wait_state(page, timeout_seconds: float) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = page.evaluate(STATE)
        if state in ("done", "failed"):
            return state
        page.wait_for_timeout(250)
    return "timeout"


def _decode_result(row: dict, expected_names: set[str]) -> list[EmbeddedFile]:
    raw_name = str(row.get("name") or "")
    directory = str(row.get("dir") or "")
    original = _safe_relative(directory + raw_name)
    if original is None or original.casefold() not in expected_names:
        return []
    out: list[EmbeddedFile] = []
    for key, name_key in (("ass", "assName"), ("smi", "smiName")):
        payload = row.get(key)
        name = str(row.get(name_key) or "")
        if not payload or not name:
            continue
        relative = _safe_relative(directory + name)
        if relative is None or PurePosixPath(relative).suffix.lower() not in (".ass", ".smi"):
            continue
        if PurePosixPath(relative).with_suffix(".jmk").as_posix().casefold() != original.casefold():
            continue
        try:
            data = base64.b64decode(payload, validate=True)
        except Exception:
            continue
        if data:
            out.append(EmbeddedFile(relative, data))
    return out


def convert_tistory_jmks(page_url: str, jmk_names: set[str],
                         timeout_seconds: float = 60) -> list[EmbeddedFile]:
    """Use the author's own Tistory/Jamaker viewer to turn JMK into ASS/SMI."""
    expected_names = {relative.casefold() for name in jmk_names
                      if (relative := _safe_relative(name)) is not None}
    if not expected_names:
        return []

    chromium = shutil.which("chromium") or shutil.which("chromium-browser")
    if not chromium:
        raise RuntimeError("Chromium is required for JMK conversion")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is required for JMK conversion") from exc

    results: list[EmbeddedFile] = []
    seen: set[tuple[str, bytes]] = set()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=chromium,
            headless=True,
            args=["--no-sandbox", "--disable-gpu"],
        )
        try:
            page = browser.new_page(
                user_agent=UA,
                viewport={"width": 1280, "height": 900},
            )
            try:
                page.goto(page_url, wait_until="domcontentloaded", timeout=60_000)
            except Exception:
                pass

            page.wait_for_function(VIEWER_READY, timeout=20_000)
            page.evaluate(SETUP)
            candidates = page.evaluate(CANDIDATES) or []

            for candidate in candidates:
                if not page.evaluate(CLICK, candidate["key"]):
                    continue
                state = _wait_state(page, timeout_seconds)
                if state != "done":
                    continue

                for row in page.evaluate(COLLECT) or []:
                    for item in _decode_result(row, expected_names):
                        key = (item.path, item.data)
                        if key in seen:
                            continue
                        seen.add(key)
                        results.append(item)

                converted_sources = {
                    PurePosixPath(item.path).with_suffix(".jmk").as_posix().casefold()
                    for item in results
                }
                if expected_names.issubset(converted_sources):
                    break
        finally:
            browser.close()

    return results
