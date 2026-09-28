#!/usr/bin/env python3
"""Apply unapplied subtitle delta Releases from anime-db-download to the Pi."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = "pingpingjinping/anime-db-download"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases"
TAG_RE = re.compile(r"^subs-\d{4}-\d{2}-\d{2}-\d+$")
ARCHIVE_NAME = "subtitle-delta.tar.gz"
MANIFEST_NAME = "manifest.json"
FAILED_NAME = "failed.jsonl"
DEFAULT_TARGET = Path("/home/pi/streaming-project/subtitle-data")
STATE_FILE = Path.home() / ".local/state/anime-db-download/subtitle-releases.json"
MAX_ASSET_SIZE = 2 * 1024 * 1024 * 1024


def headers(token: str, accept: str = "application/vnd.github+json") -> dict[str, str]:
    return {
        "Accept": accept,
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "anime-db-download-pi-sync/1.0",
    }


def api_json(url: str, token: str):
    req = urllib.request.Request(url, headers=headers(token))
    with urllib.request.urlopen(req, timeout=45) as response:
        return json.load(response)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, response_headers, newurl):
        return None


def download_asset(asset: dict, token: str, destination: Path) -> str:
    req = urllib.request.Request(asset["url"], headers=headers(token, "application/octet-stream"))
    opener = urllib.request.build_opener(NoRedirect)
    response = None
    try:
        response = opener.open(req, timeout=90)
    except urllib.error.HTTPError as error:
        if error.code not in (301, 302, 303, 307, 308):
            raise
        location = error.headers.get("Location") or ""
        parsed = urllib.parse.urlparse(location)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not (
            host == "github.com"
            or host.endswith(".githubusercontent.com")
            or host.endswith(".github.com")
        ):
            raise RuntimeError(f"Unexpected GitHub asset redirect host: {host}")
        response = urllib.request.urlopen(location, timeout=180)

    digest = hashlib.sha256()
    total = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with response, destination.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_ASSET_SIZE:
                raise RuntimeError(f"Release asset exceeded {MAX_ASSET_SIZE} bytes")
            digest.update(chunk)
            output.write(chunk)
    actual = digest.hexdigest()
    expected = str(asset.get("digest") or "")
    if expected.startswith("sha256:"):
        expected = expected.removeprefix("sha256:")
        if not hmac.compare_digest(actual, expected):
            destination.unlink(missing_ok=True)
            raise RuntimeError(f"SHA-256 mismatch for {asset.get('name')}")
    return actual


def list_subtitle_releases(token: str) -> list[dict]:
    releases: list[dict] = []
    page = 1
    while True:
        rows = api_json(f"{RELEASES_API}?per_page=100&page={page}", token)
        if not isinstance(rows, list):
            raise RuntimeError("Unexpected GitHub releases response")
        releases.extend(row for row in rows if TAG_RE.fullmatch(str(row.get("tag_name") or "")))
        if len(rows) < 100:
            break
        page += 1
    return sorted(releases, key=lambda row: (row.get("published_at") or "", row.get("tag_name") or ""))


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "applied": []}
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("version") != 1 or not isinstance(state.get("applied"), list):
        raise RuntimeError(f"Invalid Pi subtitle state: {path}")
    return state


def save_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def safe_extract(archive_path: Path, target: Path):
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            if member.issym() or member.islnk():
                raise RuntimeError(f"Refusing link in subtitle archive: {member.name}")
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise RuntimeError(f"Unsafe path in subtitle archive: {member.name}")
            resolved = (target / member_path).resolve()
            if target != resolved and target not in resolved.parents:
                raise RuntimeError(f"Archive path escaped subtitle directory: {member.name}")
        archive.extractall(target, members=members)


def asset_by_name(release: dict, name: str) -> dict | None:
    matches = [asset for asset in release.get("assets", []) if asset.get("name") == name]
    if len(matches) > 1:
        raise RuntimeError(f"Release {release.get('tag_name')} has duplicate {name} assets")
    return matches[0] if matches else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--state", type=Path, default=STATE_FILE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    token = os.environ.get("ANIME_DB_GITHUB_TOKEN", "").strip()
    if not token:
        raise SystemExit("ANIME_DB_GITHUB_TOKEN is not set")

    state = load_state(args.state)
    applied = set(state["applied"])
    releases = list_subtitle_releases(token)
    pending = [release for release in releases if release["tag_name"] not in applied]

    if not pending:
        print("Subtitle archive is already current.")
        return

    print("Pending subtitle Releases:")
    for release in pending:
        print(f"  - {release['tag_name']} ({release.get('published_at')})")
    if args.dry_run:
        return

    for release in pending:
        tag = release["tag_name"]
        archive_asset = asset_by_name(release, ARCHIVE_NAME)
        if not archive_asset:
            raise RuntimeError(f"{tag} is missing {ARCHIVE_NAME}")

        with tempfile.TemporaryDirectory(prefix="subtitle-release-") as tmp:
            tmpdir = Path(tmp)
            archive_path = tmpdir / ARCHIVE_NAME
            digest = download_asset(archive_asset, token, archive_path)
            print(f"[{tag}] downloaded {ARCHIVE_NAME} sha256={digest}")
            safe_extract(archive_path, args.target)

            manifest_asset = asset_by_name(release, MANIFEST_NAME)
            if manifest_asset:
                path = tmpdir / MANIFEST_NAME
                download_asset(manifest_asset, token, path)
                destination = args.target / "manifests" / f"{tag}.json"
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)

            failed_asset = asset_by_name(release, FAILED_NAME)
            if failed_asset:
                path = tmpdir / FAILED_NAME
                download_asset(failed_asset, token, path)
                if path.stat().st_size:
                    destination = args.target / "failed" / f"{tag}.jsonl"
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, destination)

        state["applied"].append(tag)
        save_state(args.state, state)
        print(f"[{tag}] applied to {args.target}")


if __name__ == "__main__":
    main()
