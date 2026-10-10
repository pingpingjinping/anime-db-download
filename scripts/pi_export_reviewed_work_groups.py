#!/usr/bin/env python3
"""Export sanitised, verified Pi admin subtitle -> source ID groups to db-v1.

No local paths, filenames, account names or tokens are included in the manifest.
Requires ANIME_DB_GITHUB_UPLOAD_TOKEN (contents:write) in the environment.
"""
import argparse
import calendar
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

REPO = "pingpingjinping/anime-db-download"
API = "https://api.github.com/repos/" + REPO
ID_RE = re.compile(r"^(?:ani_[1-9]\d*|re_[A-Za-z0-9_-]+|ak_[A-Za-z0-9_-]+|db_(?:AniList|Onnada)_[A-Za-z0-9_-]+)$")
KIND = {"main", "ova", "special", "movie", "preview", "drama", "unknown"}


def is_last_day(now):
    return now.day == calendar.monthrange(now.year, now.month)[1]


def build_manifest(rows, month):
    groups = {}
    identity_owner = {}
    for record in rows:
        members = sorted(set(str(x) for x in record.get("members", []) if x))
        if len(members) < 2:
            continue  # A lone local file assignment does not establish a DB cross-link.
        if len(members) > 12 or any(not ID_RE.fullmatch(m) for m in members):
            raise ValueError("Invalid or excessive source ID in saved Pi cross-links")
        kind, season = record.get("kind"), record.get("season")
        if kind not in KIND or type(season) is not int or not 1 <= season <= 99:
            raise ValueError("Invalid season/kind in saved Pi cross-links")
        # Two separate DB records from one source should not be collapsed by
        # filename similarity. Multi-target Anikoto stays in separate groups.
        providers = [m.split("_", 2)[1] if m.startswith("db_") else m.split("_", 1)[0]
                     for m in members]
        if len(set(providers)) != len(providers):
            raise ValueError("Conflicting same-provider source IDs in one reviewed group")
        key = hashlib.sha256(("\n".join(members)).encode("utf-8")).hexdigest()[:32]
        group = {"id": key, "members": members, "title": str(record.get("title") or "")[:300],
                 "kind": kind, "season": season}
        if not group["title"]:
            raise ValueError("Missing reviewed work title")
        if key in groups and (groups[key]["kind"],groups[key]["season"]) != (kind,season):
            raise ValueError("Same work group has conflicting season or media format")
        for member in members:
            # One Anikoto listing may intentionally serve multiple TV seasons;
            # its URL is a non-union reference, unlike core source IDs.
            if member.startswith("ak_"):
                continue
            if member in identity_owner and identity_owner[member] != key:
                raise ValueError("Conflicting manual groups share a source ID: " + member)
            identity_owner[member] = key
        groups[key] = group
    return {"schema": 1, "month": month,
            "groups": [groups[key] for key in sorted(groups)],
            "count": len(groups)}


def export_rows(streaming_repo):
    """Ask streaming-project to produce its read-only, path-free PG projection."""
    script = streaming_repo / "scripts" / "export-reviewed-subtitle-work-links.py"
    if not script.is_file():
        raise RuntimeError("Missing streaming-project reviewed link exporter: " + str(script))
    result = subprocess.run([sys.executable, str(script)], cwd=streaming_repo,
                            capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("Unable to validate Pi reviewed links: " + result.stderr[:280])
    rows = json.loads(result.stdout)
    if not isinstance(rows, list):
        raise ValueError("Reviewed work exporter did not return an array")
    return rows


def stage_path(month, directory):
    return pathlib.Path(directory) / ("pi-reviewed-work-groups-" + month + ".json")


def prepare_snapshot(streaming_repo, month, directory, *, dry_run=False):
    """Freeze a sanitized verified snapshot for the independent 04:00 publish."""
    manifest = build_manifest(export_rows(streaming_repo), month)
    if dry_run:
        print("Validated", manifest["count"], "Pi reviewed groups for", month,
              "(no stage or upload)")
        return manifest
    import stat
    directory = pathlib.Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    target = stage_path(month, directory)
    manifest["prepared_at_kst"] = dt.datetime.now(
        dt.timezone(dt.timedelta(hours=9))).isoformat()
    payload = (json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")) + "\n").encode("utf-8")
    if target.exists():
        # Prevent an edit after a snapshot has already been prepared/uploaded.
        if target.read_bytes() != payload:
            # Prepared time naturally differs; permit byte-identical reviewed
            # groups only, leaving the original preparation timestamp intact.
            earlier = json.loads(target.read_text(encoding="utf-8"))
            earlier.pop("prepared_at_kst", None)
            manifest2 = dict(manifest)
            manifest2.pop("prepared_at_kst", None)
            if earlier != manifest2:
                raise RuntimeError("This month's prepared Pi review has changed; inspect before republishing")
        print("Existing verified monthly Pi review stage:", target)
        return manifest
    fd, tmp = tempfile.mkstemp(prefix=".pi-reviewed-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print("Prepared", manifest["count"], "groups for month", month, "at", target)
    return manifest


def load_prepared_snapshot(month, directory, today):
    """Only a fresh, first-KST-day frozen snapshot may be published."""
    target = stage_path(month, directory)
    if not target.is_file():
        raise RuntimeError("03:00 Pi reviewed snapshot is missing: " + str(target))
    manifest = json.loads(target.read_text(encoding="utf-8"))
    from apply_pi_reviewed_groups import checked_manifest
    checked_manifest(manifest, month)
    prepared = dt.datetime.fromisoformat(manifest.get("prepared_at_kst", ""))
    kst = dt.timezone(dt.timedelta(hours=9))
    prepared = prepared.astimezone(kst)
    if prepared.date() != today.date() or today.day != 1:
        raise RuntimeError("Prepared Pi review must come from this month's first KST day")
    return manifest


def api(path, token):
    req = urllib.request.Request(API + path, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "anime-db-review-export/1.0",
    })
    with urllib.request.urlopen(req, timeout=25) as response:
        return json.load(response)


def upload(manifest, token):
    release = api("/releases/tags/db-v1", token)
    name = "pi-reviewed-work-groups-" + manifest["month"] + ".json"
    payload = (json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")) + "\n").encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    previous = [x for x in release.get("assets", []) if x.get("name") == name]
    if previous:
        if len(previous) == 1 and previous[0].get("digest") == "sha256:" + digest:
            print("Already uploaded: " + name)
            return
        raise RuntimeError("Month's manifest already exists with different content; manual reconciliation required")
    url = "https://uploads.github.com/repos/" + REPO + "/releases/" + str(release["id"]) + "/assets?"
    url += urllib.parse.urlencode({"name": name})
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "anime-db-review-export/1.0",
    })
    with urllib.request.urlopen(req, timeout=45) as response:
        uploaded = json.load(response)
    if uploaded.get("name") != name:
        raise RuntimeError("Unexpected uploaded release asset")
    print("Uploaded " + name + " with " + str(manifest["count"]) + " reviewed work groups, SHA-256 " + digest)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--streaming-repo", type=pathlib.Path,
                        default=pathlib.Path("/home/pi/streaming-project"))
    parser.add_argument("--stage-dir", type=pathlib.Path,
                        default=pathlib.Path("/home/pi/.local/state/anime-db-download/monthly-reviewed"))
    operations = parser.add_mutually_exclusive_group()
    operations.add_argument("--prepare", action="store_true",
                            help="At 03:00, verify live Pi PostgreSQL links and stage JSON")
    operations.add_argument("--publish-staged", action="store_true",
                            help="At 04:00, upload the exactly staged JSON to GitHub")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true",
                        help="Allow a preparation dry-run outside the first day")
    args = parser.parse_args()
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))
    if now.day != 1 and not (args.force and args.dry_run and not args.publish_staged):
        raise SystemExit("Pi monthly reviewed export only runs on the first KST calendar day")

    month = now.strftime("%Y-%m")
    if args.publish_staged:
        if args.dry_run:
            raise SystemExit("--dry-run cannot be used with --publish-staged")
        manifest = load_prepared_snapshot(month, args.stage_dir, now)
        token = os.environ.get("ANIME_DB_GITHUB_UPLOAD_TOKEN", "").strip()
        if not token:
            raise SystemExit("ANIME_DB_GITHUB_UPLOAD_TOKEN with contents:write is required")
        upload(manifest, token)
    else:
        prepare_snapshot(args.streaming_repo, month, args.stage_dir,
                         dry_run=args.dry_run)




if __name__ == "__main__":
    main()
