#!/usr/bin/env python3
"""Atomically publish the verified 2026-10-15 catalog onto existing db-v1 release."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from split_db_v1_release import sha256

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "data/matching/multiwork-release-patch-20261015.json"
OUT = ROOT / "multiwork-20261015-output"
CAT = "anime_four_sources_multilingual_prepared.zip"
REG = "anime_reviewed_subtitle_registry_v1.zip"


def gh(*args):
    subprocess.run(["gh", *map(str, args), "--repo", os.environ["GITHUB_REPOSITORY"]], check=True)


def main():
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    validation = json.loads((OUT / "multiwork-validation-20261015.json").read_text(encoding="utf-8"))
    if validation["batch"] != plan["batch"] or validation["review_records"] != 236:
        raise ValueError("Review validation not the expected batch")
    if len(validation["applied_source_link_corrections"]) != len(plan["targeted"]):
        raise ValueError("Not all planned source link corrections were applied")
    for name, digest in validation["result_sha256"].items():
        if sha256(OUT / name) != digest:
            raise ValueError("Build artifact changed after verification: " + name)
    with tempfile.TemporaryDirectory(prefix="multiwork-publish-") as scratch:
        scratch = Path(scratch)
        gh("release", "download", "db-v1", "--pattern", CAT, "--pattern", REG, "--dir", scratch)
        for name, expected in plan["expected_assets"].items():
            if sha256(scratch / name) != expected:
                raise ValueError(f"Current db-v1 {name} changed; refusing overwrite")
        backup = scratch / "anime_four_sources_multilingual_prepared.before-multiwork-20261015.zip"
        shutil.copyfile(scratch / CAT, backup)
        gh("release", "upload", "db-v1", backup)
        try:
            gh("release", "upload", "db-v1", OUT / CAT, "--clobber")
            verify = scratch / "verify"
            verify.mkdir()
            gh("release", "download", "db-v1", "--pattern", CAT, "--pattern", REG, "--dir", verify)
            for name, expected in validation["result_sha256"].items():
                if sha256(verify / name) != expected:
                    raise ValueError(f"Published db-v1 {name} does not match reviewed artifact")
            gh("release", "upload", "db-v1", OUT / "multiwork-validation-20261015.json")
        except Exception:
            # Fail safe: reinstall baseline catalog if any post-upload check fails.
            gh("release", "upload", "db-v1", scratch / CAT, "--clobber")
            raise
    print("Existing db-v1 catalog verified and updated; registry was preserved bit-for-bit")


if __name__ == "__main__":
    main()
