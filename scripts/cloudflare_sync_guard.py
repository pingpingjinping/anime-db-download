#!/usr/bin/env python3
"""Check whether a D1 search projection needs replacing, and verify an import.

This intentionally does not contact Cloudflare itself: parse Wrangler --json
results already fetched by GitHub Actions. Fail closed on unexpected output.
"""
import argparse
import json
from pathlib import Path


def manifest_data(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Invalid search manifest")
    digest = data.get("sql_sha256", "")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("Invalid SQL SHA256 in manifest")
    for key in ("works", "aliases"):
        if type(data.get(key)) is not int or data[key] < 1:
            raise ValueError(f"Invalid {key} count in search manifest")
    return data


def wrangler_rows(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError("Unexpected Wrangler JSON output (no command results)")
    rows = []
    for result in data:
        if not isinstance(result, dict) or result.get("success") is not True:
            raise ValueError("Wrangler query failed or returned malformed JSON")
        records = result.get("results")
        if not isinstance(records, list):
            raise ValueError("Wrangler JSON missing results array")
        rows.extend(records)
    return rows


def can_skip(manifest, rows):
    if len(rows) != 1 or not isinstance(rows[0], dict):
        return False
    row = rows[0]
    return (
        row.get("sql_sha256") == manifest["sql_sha256"]
        and all(type(row.get(col)) is int and row[col] == manifest[expected]
                for col, expected in (
                    ("works", "works"), ("aliases", "aliases"),
                    ("actual_works", "works"), ("actual_aliases", "aliases"),
                ))
    )


def verify_counts(manifest, rows):
    if len(rows) != 2 or not all(isinstance(row, dict) for row in rows):
        raise ValueError("Unexpected D1 verification row count")
    if type(rows[0].get("works")) is not int or rows[0]["works"] != manifest["works"]:
        raise ValueError(f"D1 work count differs from export: {rows[0]}")
    if type(rows[1].get("aliases")) is not int or rows[1]["aliases"] != manifest["aliases"]:
        raise ValueError(f"D1 alias count differs from export: {rows[1]}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "verify"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--wrangler-json", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    manifest = manifest_data(args.manifest)
    rows = wrangler_rows(args.wrangler_json)
    if args.mode == "verify":
        verify_counts(manifest, rows)
        print(f"D1 import verified: {manifest['works']} works and {manifest['aliases']} aliases")
    else:
        skipped = can_skip(manifest, rows)
        print("Cloudflare sync " + ("skipped: identical verified D1 index" if skipped
                                   else "required: index changed or no verified previous state"))
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as out:
                out.write(f"skip={'true' if skipped else 'false'}\n")


if __name__ == "__main__":
    main()
