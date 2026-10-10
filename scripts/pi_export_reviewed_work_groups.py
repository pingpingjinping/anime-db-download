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
    # Local PostgreSQL command uses the existing docker-compose DB credentials.
    # The SQL does not return relative_path, updated_by or any subtitle filenames.
    sql = """
      SELECT json_build_object(
        'members', array_prepend(l.anime_id,
          COALESCE((SELECT array_agg(m.anime_id ORDER BY m.anime_id)
            FROM anime_admin_subtitle_link_members m
            WHERE m.relative_path=l.relative_path), ARRAY[]::text[])),
        'title', l.work_title, 'kind', l.subtitle_kind, 'season', l.season
      )::text
      FROM anime_admin_subtitle_links l ORDER BY l.anime_id;
    """
    command = ["docker", "compose", "exec", "-T", "db", "sh", "-c",
               'psql -X -q -A -t -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"',
               "sh", sql]
    result = subprocess.run(command, cwd=streaming_repo, text=True, capture_output=True)
    if result.returncode:
        # Initial setup may have no administrator-created link tables yet.
        if 'relation "anime_admin_subtitle_links" does not exist' in result.stderr:
            return []
        raise RuntimeError("Unable to export Pi reviewed groups: " + result.stderr[:300])
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


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
    parser.add_argument("--streaming-repo", type=pathlib.Path, default=pathlib.Path("/home/pi/streaming-project"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Manual test on a non-last day; still uses the current month")
    args = parser.parse_args()
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))
    if not is_last_day(now) and not args.force:
        print("Not the last calendar day in Asia/Seoul; skip.")
        return
    manifest = build_manifest(export_rows(args.streaming_repo), now.strftime("%Y-%m"))
    if args.dry_run:
        print("Validated " + str(manifest["count"]) + " reviewed groups for " + manifest["month"])
        return
    token = os.environ.get("ANIME_DB_GITHUB_UPLOAD_TOKEN", "").strip()
    if not token:
        raise SystemExit("ANIME_DB_GITHUB_UPLOAD_TOKEN with release contents:write permission is required")
    upload(manifest, token)


if __name__ == "__main__":
    main()
