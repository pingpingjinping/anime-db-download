#!/usr/bin/env python3
"""Upload reviewed two-link correction to current db-v1 with rollback on failure."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from split_db_v1_release import sha256

ROOT=Path(__file__).resolve().parents[1]
PLAN=ROOT/"data/matching/reanime-corrections-20261015.json"
OUT=ROOT/"reanime-corrections-20261015-output"
CAT="anime_four_sources_multilingual_prepared.zip"
REG="anime_reviewed_subtitle_registry_v1.zip"


def gh(*args):
    subprocess.run(["gh",*map(str,args),"--repo",os.environ["GITHUB_REPOSITORY"]],check=True)


def main():
    plan=json.loads(PLAN.read_text(encoding="utf-8"))
    validation=json.loads((OUT/"reanime-corrections-validation-20261015.json").read_text(encoding="utf-8"))
    if validation["batch"]!=plan["batch"] or validation["reviewed_ledger_rows"]!=236:
        raise ValueError("Expected reviewed source release not present")
    if len(validation["source_link_changes"])!=2 or validation["new_episode_assignments"]!=0:
        raise ValueError("Unexpected modification scope")
    for name,digest in validation["result_sha256"].items():
        if sha256(OUT/name)!=digest:
            raise ValueError("Verified build artifacts unexpectedly changed")
    with tempfile.TemporaryDirectory(prefix="reanime-corrections-") as temp:
        root=Path(temp)
        gh("release","download","db-v1","--pattern",CAT,"--pattern",REG,"--dir",root)
        for name,expected in ((CAT,plan["base_catalog_sha256"]),(REG,plan["base_registry_sha256"])):
            if sha256(root/name)!=expected:
                raise ValueError("Published db-v1 changed; refusing to overwrite "+name)
        backup=root/"anime_four_sources_multilingual_prepared.before-reanime-fix-20261015.zip"
        shutil.copyfile(root/CAT,backup)
        gh("release","upload","db-v1",backup)
        try:
            gh("release","upload","db-v1",OUT/CAT,"--clobber")
            verified=root/"verified";verified.mkdir()
            gh("release","download","db-v1","--pattern",CAT,"--pattern",REG,"--dir",verified)
            for name,digest in validation["result_sha256"].items():
                if sha256(verified/name)!=digest:
                    raise ValueError("New published release hash mismatch: "+name)
            gh("release","upload","db-v1",OUT/"reanime-corrections-validation-20261015.json")
        except Exception:
            gh("release","upload","db-v1",root/CAT,"--clobber")
            raise
    print("Updated db-v1 source identities, registry unchanged; re-download hashes verified")


if __name__=="__main__":
    main()
