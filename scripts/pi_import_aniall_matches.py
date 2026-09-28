#!/usr/bin/env python3
"""Import the newest reviewed-safe AniAll Release on the Pi, once per asset."""
import json
import os
import subprocess
import tempfile
import urllib.request
from pathlib import Path


API = "https://api.github.com/repos/pingpingjinping/anime-db-download"


def fetch(url, token, accept="application/vnd.github+json"):
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": accept,
        "User-Agent": "anime-db-download-pi-monthly"})
    with urllib.request.urlopen(request, timeout=45) as response:
        return response.read()


def main():
    token = os.environ.get("ANIME_DB_GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise SystemExit("Set ANIME_DB_GITHUB_TOKEN to read private AniAll match releases.")
    streaming = Path(os.environ.get("STREAMING_REPO", "/home/pi/streaming-project"))
    state = streaming / "subtitle-data" / "aniall-monthly-imported.json"
    imported = set(json.loads(state.read_text()).get("tags", [])) if state.exists() else set()
    releases = json.loads(fetch(API + "/releases?per_page=100", token))
    releases = [r for r in releases if r["tag_name"].startswith("aniall-matches-")
                and not r.get("draft")]
    if not releases:
        raise SystemExit("No completed AniAll monthly audit Release found.")
    for release in sorted(releases, key=lambda r: r["created_at"]):
        tag = release["tag_name"]
        if tag in imported:
            continue
        assets = {a["name"]: a for a in release["assets"]}
        if not {"aniall-auto-matches.json", "aniall-candidates.json"}.issubset(assets):
            raise SystemExit(f"AniAll Release {tag} is missing mapping or candidate assets.")
        data = fetch(assets["aniall-auto-matches.json"]["url"], token, "application/octet-stream")
        candidate_data = fetch(assets["aniall-candidates.json"]["url"], token, "application/octet-stream")
        items = json.loads(data)
        candidates = json.loads(candidate_data)
        if not isinstance(items, list) or any(i.get("reviewStatus") != "auto_exact" for i in items):
            raise SystemExit("AniAll monthly mapping asset is invalid.")
        if not isinstance(candidates, list) or any(i.get("reviewStatus") != "auto_candidate" for i in candidates):
            raise SystemExit("AniAll candidate asset is invalid.")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "aniall-auto-matches.json"
            path.write_bytes(data)
            candidate_path = Path(folder) / "aniall-candidates.json"
            candidate_path.write_bytes(candidate_data)
            if items:
                subprocess.run(["/usr/bin/python3", str(streaming / "scripts/import-aniall-matches.py"),
                                str(path)], cwd=streaming, check=True)
            if candidates:
                subprocess.run(["/usr/bin/python3", str(streaming / "scripts/import-aniall-candidates.py"),
                                str(candidate_path)], cwd=streaming, check=True)
        imported.add(tag)
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"tags": sorted(imported)}, indent=2) + "\n")
        print(f"AniAll monthly mapping: {len(items)} exact links, {len(candidates)} scored candidates from {tag}.")


if __name__ == "__main__":
    main()
