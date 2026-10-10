#!/usr/bin/env python3
"""Prevent Pi from importing an old or unconfirmed monthly GitHub db-v1 asset."""
import datetime as dt
import hashlib
import hmac
import json
import os
import pathlib
import sys


def verify_release(streaming_repo, now=None):
    now = now or dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))
    month = now.strftime("%Y-%m")
    if now.day < 2:
        raise RuntimeError("Second-day Pi refresh cannot run before KST month day 2")
    sys.path.insert(0,str(pathlib.Path(streaming_repo)/"scripts"))
    import importlib.util
    script=pathlib.Path(streaming_repo)/"scripts"/"update-anime-db-release.py"
    spec=importlib.util.spec_from_file_location("release_updater_for_guard",script)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    token=os.environ.get("ANIME_DB_GITHUB_TOKEN","").strip()
    if not token:
        raise RuntimeError("ANIME_DB_GITHUB_TOKEN is required for catalog preflight")
    release=module.api_json(module.RELEASE_API,token)
    catalog=module.release_asset(release,module.CATALOG_ASSET_NAME)
    name="monthly-db-refresh-"+month+".json"
    marker_asset=module.release_asset(release,name)
    raw=module.download_asset(marker_asset["url"],token)
    digest=module.metadata_digest(marker_asset)
    if digest and not hmac.compare_digest(hashlib.sha256(raw).hexdigest(),digest):
        raise RuntimeError("Monthly completion marker failed GitHub digest verification")
    marker=json.loads(raw)
    if marker.get("schema")!=1 or marker.get("month")!=month:
        raise RuntimeError("This month's DB refresh completion marker is missing or invalid")
    expected=marker.get("catalog_sha256","")
    published=module.metadata_digest(catalog)
    if not expected or len(expected)!=64 or not published or not hmac.compare_digest(expected,published):
        raise RuntimeError("Catalog asset does not match completed first-day refresh; skipping Pi import")
    print("Verified completed GitHub catalog month",month,"SHA-256",expected)


def main():
    repo=pathlib.Path(os.environ.get("STREAMING_REPO","/home/pi/streaming-project"))
    verify_release(repo)


if __name__=="__main__":main()
