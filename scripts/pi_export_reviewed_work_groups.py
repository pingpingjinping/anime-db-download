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


def export_manual_work_matches(streaming_repo):
    """Read only explicit admin work-to-work IDs; no subtitles or user data."""
    script = streaming_repo / "scripts" / "export-manual-work-match-overrides.py"
    if not script.is_file():
        raise RuntimeError("Missing manual work match exporter in streaming-project")
    proc = subprocess.run([sys.executable, str(script)], cwd=streaming_repo,
                          capture_output=True, text=True)
    if proc.returncode:
        raise RuntimeError("Pi manual work match export failed: " + proc.stderr[:240])
    rows = json.loads(proc.stdout)
    from apply_pi_manual_work_matches import checked_overrides, checked_anikoto
    if not isinstance(rows, dict) or set(rows) != {"reanime", "anikoto"}:
        raise ValueError("Manual export must supply both provider lists")
    checked_overrides(rows["reanime"])
    checked_anikoto(rows["anikoto"])
    return rows


def candidate_name(month):
    return "anime_four_sources_multilingual_prepared_pi_reviewed_" + month + ".zip"


def fetch_canonical_catalog(streaming_repo, token):
    """Use the existing Pi Release downloader and verify the GitHub digest."""
    import importlib.util
    script = streaming_repo / "scripts" / "update-anime-db-release.py"
    if not script.is_file():
        raise RuntimeError("Cannot locate streaming-project GitHub Release updater")
    spec=importlib.util.spec_from_file_location("pi_review_release_read",script)
    updater=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(updater)
    release=updater.api_json(updater.RELEASE_API, token)
    asset=updater.release_asset(release, updater.CATALOG_ASSET_NAME)
    payload=updater.download_asset(asset["url"], token)
    expected=updater.metadata_digest(asset)
    actual=hashlib.sha256(payload).hexdigest()
    if expected and expected != actual:
        raise RuntimeError("Canonical db-v1 catalog SHA-256 verification failed")
    return payload


def export_cached_posters(streaming_repo):
    """Use the streamer-owned, read-only artwork cache export (no user paths)."""
    script=streaming_repo/"scripts"/"export-db-search-posters.py"
    if not script.is_file():
        raise RuntimeError("Missing streaming-project db search poster exporter")
    proc=subprocess.run([sys.executable,str(script)],cwd=streaming_repo,
                        text=True,capture_output=True)
    if proc.returncode:
        raise RuntimeError("Pi cached artwork export failed: "+proc.stderr[:280])
    rows=json.loads(proc.stdout)
    from apply_pi_search_posters import validate
    validate(rows)
    return rows


def stage_path(month, directory):
    return pathlib.Path(directory) / ("pi-reviewed-work-groups-" + month + ".json")


def prepare_snapshot(streaming_repo, month, directory, *, dry_run=False):
    """At 03:00, merge reviewed Pi IDs into a validated SQLite release candidate."""
    from apply_pi_reviewed_groups import patch_archive
    manifest = build_manifest(export_rows(streaming_repo), month)
    manual = export_manual_work_matches(streaming_repo)
    manifest["manual_work_matches"] = manual["reanime"]
    manifest["manual_match_count"] = len(manual["reanime"])
    manifest["manual_anikoto_matches"] = manual["anikoto"]
    manifest["manual_anikoto_match_count"] = len(manual["anikoto"])
    if dry_run:
        print("Validated", manifest["count"], "Pi reviewed groups for", month,
              "(no stage/download/upload)")
        return manifest

    read_token=os.environ.get("ANIME_DB_GITHUB_TOKEN","").strip()
    if not read_token:
        raise RuntimeError("ANIME_DB_GITHUB_TOKEN is required to fetch the existing catalog")

    directory = pathlib.Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    target_json = stage_path(month, directory)
    target_db = directory / candidate_name(month)
    if target_json.exists() or target_db.exists():
        if not target_json.is_file() or not target_db.is_file():
            raise RuntimeError("Incomplete 03:00 Pi DB stage; do not upload")
        previous=json.loads(target_json.read_text(encoding="utf-8"))
        from apply_pi_reviewed_groups import checked_manifest
        checked_manifest(previous, month)
        if previous.get("groups") != manifest["groups"]:
            raise RuntimeError("Confirmed reviewed work IDs differ from the frozen 03:00 DB")
        if hashlib.sha256(target_db.read_bytes()).hexdigest()!=previous.get("candidate_sha256"):
            raise RuntimeError("Frozen Pi reviewed DB candidate checksum changed")
        print("Previously prepared Pi reviewed DB:", target_db)
        return previous

    from apply_pi_reviewed_groups import checked_manifest
    checked_manifest(manifest,month)
    existing=fetch_canonical_catalog(streaming_repo,read_token)
    base_sha=hashlib.sha256(existing).hexdigest()
    fd, tmp_db = tempfile.mkstemp(prefix=".candidate-",suffix=".zip",dir=directory)
    os.close(fd)
    fd, tmp_json = tempfile.mkstemp(prefix=".review-",suffix=".json",dir=directory)
    os.close(fd)
    try:
        pathlib.Path(tmp_db).write_bytes(existing)
        # Use a provisional manifest to validate each source ID against actual
        # release SQLite rows before copying the finalized DB to the stage.
        pathlib.Path(tmp_json).write_text(json.dumps(manifest,ensure_ascii=False),
                                           encoding="utf-8")
        patch_archive(tmp_db, tmp_json, month)
        from apply_pi_search_posters import patch_archive as patch_artwork
        artwork_count=patch_artwork(tmp_db,export_cached_posters(streaming_repo))
        manifest["cached_artwork_count"]=artwork_count
        manifest["base_release_sha256"]=base_sha
        manifest["candidate_sha256"]=hashlib.sha256(pathlib.Path(tmp_db).read_bytes()).hexdigest()
        manifest["prepared_at_kst"]=dt.datetime.now(
            dt.timezone(dt.timedelta(hours=9))).isoformat()
        pathlib.Path(tmp_json).write_text(
            json.dumps(manifest,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n",
            encoding="utf-8")
        os.chmod(tmp_db,0o600)
        os.chmod(tmp_json,0o600)
        os.replace(tmp_db,target_db)
        os.replace(tmp_json,target_json)
    finally:
        for file in (tmp_db,tmp_json):
            if os.path.exists(file): os.unlink(file)
    print("Prepared",manifest["count"],"verified groups in Pi SQLite DB candidate:",target_db)
    return manifest


def load_prepared_snapshot(month, directory, today):
    """Verify that 04:00 only uploads 03:00's completed, unchanged DB."""
    target=stage_path(month,directory)
    candidate=pathlib.Path(directory)/candidate_name(month)
    if not target.is_file() or not candidate.is_file():
        raise RuntimeError("03:00 Pi reviewed DB candidate or manifest is missing")
    manifest=json.loads(target.read_text(encoding="utf-8"))
    from apply_pi_reviewed_groups import checked_manifest
    checked_manifest(manifest,month)
    if today.day!=1:
        raise RuntimeError("Reviewed DB upload is only permitted on the first KST day")
    prepared=dt.datetime.fromisoformat(manifest.get("prepared_at_kst",""))
    prepared=prepared.astimezone(dt.timezone(dt.timedelta(hours=9)))
    if prepared.date()!=today.date():
        raise RuntimeError("The prepared Pi reviewed DB belongs to another day")
    if hashlib.sha256(candidate.read_bytes()).hexdigest()!=manifest.get("candidate_sha256"):
        raise RuntimeError("Pi 03:00 reviewed DB candidate has been modified")
    return manifest


def upload_candidate(manifest, directory, token):
    """Publish an immutable prepared *candidate*, never replace canonical db-v1 at 04:00."""
    name=candidate_name(manifest["month"])
    path=pathlib.Path(directory)/name
    payload=path.read_bytes()
    if hashlib.sha256(payload).hexdigest()!=manifest.get("candidate_sha256"):
        raise RuntimeError("Candidate SHA-256 changed before upload")
    release=api("/releases/tags/db-v1",token)
    previous=[asset for asset in release.get("assets",[]) if asset.get("name")==name]
    if previous:
        if len(previous)==1 and previous[0].get("digest")=="sha256:"+manifest["candidate_sha256"]:
            print("Identical Pi-reviewed DB already uploaded:",name)
            return
        raise RuntimeError("Conflicting current-month candidate already exists; manual reconciliation required")
    url="https://uploads.github.com/repos/"+REPO+"/releases/"+str(release["id"])+"/assets?"
    url+=urllib.parse.urlencode({"name":name})
    req=urllib.request.Request(url,data=payload,method="POST",headers={
        "Authorization":"Bearer "+token,
        "Accept":"application/vnd.github+json",
        "X-GitHub-Api-Version":"2022-11-28",
        "Content-Type":"application/zip",
        "User-Agent":"anime-db-reviewed-pi/1.0",
    })
    with urllib.request.urlopen(req,timeout=180) as response:
        uploaded=json.load(response)
    if uploaded.get("name")!=name:
        raise RuntimeError("Unexpected uploaded Pi reviewed DB candidate")
    print("Uploaded staged Pi-reviewed DB ZIP:",name)


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
        read_token=os.environ.get("ANIME_DB_GITHUB_TOKEN","").strip()
        if not read_token:
            raise SystemExit("ANIME_DB_GITHUB_TOKEN is required to recheck the base release")
        current=fetch_canonical_catalog(args.streaming_repo,read_token)
        if hashlib.sha256(current).hexdigest()!=manifest.get("base_release_sha256"):
            raise SystemExit("GitHub canonical release changed since 03:00; refusing stale Pi DB upload")
        upload_candidate(manifest, args.stage_dir, token)
        upload(manifest, token)
    else:
        prepare_snapshot(args.streaming_repo, month, args.stage_dir,
                         dry_run=args.dry_run)




if __name__ == "__main__":
    main()
