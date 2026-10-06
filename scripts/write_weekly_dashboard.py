#!/usr/bin/env python3
"""Write the latest Pi subtitle sync result for the streaming admin dashboard."""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
MAX_RUNS = 12


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def parse_time(value: object) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed.astimezone(KST)


def iter_state_items(state: dict):
    for bucket_name in ("success", "retry", "waiting_url", "erulabo_pending"):
        bucket = state.get(bucket_name)
        if not isinstance(bucket, dict):
            continue
        for item_id, row in bucket.items():
            if not isinstance(row, dict):
                continue
            item = row.get("item")
            if isinstance(item, dict):
                yield str(item_id), item


def item_lookup(*states: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for state in states:
        for item_id, item in iter_state_items(state):
            out[item_id] = item
    return out


def files_from_success(row: dict) -> list[str]:
    raw = row.get("files")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for value in raw:
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, dict):
            path = str(value.get("path") or value.get("name") or "").strip()
            if path:
                out.append(path)
    return out


def completed_at(row: dict, finished_at: str) -> str:
    value = str(row.get("completed_at") or "").strip()
    return value or finished_at


def download_record(item_id: str, row: dict, item: dict, finished_at: str) -> dict:
    title = str(item.get("anime_title") or item.get("title") or "").strip()
    if not title:
        verification = row.get("verification")
        if isinstance(verification, dict):
            title = str(verification.get("work_title") or "").strip()
    return {
        "id": item_id,
        "source": str(row.get("source") or item.get("source") or "unknown"),
        "title": title or item_id,
        "episode": str(item.get("episode") or ""),
        "creator": str(item.get("creator") or ""),
        "sourceUrl": str(row.get("source_url") or item.get("source_url") or ""),
        "files": files_from_success(row),
        "completedAt": completed_at(row, finished_at),
    }


def read_failures(path: Path, started: dt.datetime, finished: dt.datetime, lookup: dict[str, dict]) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    out: list[dict] = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        attempted = parse_time(row.get("attempted_at"))
        if attempted is None or attempted < started or attempted > finished + dt.timedelta(minutes=2):
            continue
        item_id = str(row.get("id") or "")
        item = lookup.get(item_id, {})
        out.append({
            "id": item_id,
            "source": str(row.get("source") or item.get("source") or "unknown"),
            "title": str(item.get("anime_title") or item.get("title") or item_id),
            "episode": str(item.get("episode") or ""),
            "creator": str(item.get("creator") or ""),
            "sourceUrl": str(row.get("source_url") or item.get("source_url") or ""),
            "reason": str(row.get("reason") or "unknown"),
            "error": str(row.get("error") or ""),
            "attemptedAt": str(row.get("attempted_at") or ""),
        })
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--before-state", type=Path, required=True)
    parser.add_argument("--failure-log", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--finished-at", required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    args = parser.parse_args()

    started = parse_time(args.started_at)
    finished = parse_time(args.finished_at)
    if started is None or finished is None:
        raise SystemExit("Invalid run timestamp")

    before = load_json(args.before_state)
    after = load_json(args.state)
    lookup = item_lookup(before, after)

    before_success = before.get("success") if isinstance(before.get("success"), dict) else {}
    after_success = after.get("success") if isinstance(after.get("success"), dict) else {}
    new_ids = [item_id for item_id in after_success if item_id not in before_success]
    downloads = [
        download_record(str(item_id), row, lookup.get(str(item_id), {}), args.finished_at)
        for item_id in new_ids
        for row in [after_success.get(item_id)]
        if isinstance(row, dict)
    ]
    downloads.sort(key=lambda row: (row.get("completedAt") or "", row.get("title") or ""), reverse=True)

    failures = read_failures(args.failure_log, started, finished, lookup)
    downloaded_files = sum(len(row["files"]) for row in downloads)

    if args.exit_code == 0 and not failures:
        status = "success"
    elif downloads:
        status = "partial"
    else:
        status = "failed"

    duration = max(0, int((finished - started).total_seconds()))
    retry = after.get("retry") if isinstance(after.get("retry"), dict) else {}
    waiting = after.get("waiting_url") if isinstance(after.get("waiting_url"), dict) else {}
    erulabo = after.get("erulabo_pending") if isinstance(after.get("erulabo_pending"), dict) else {}

    run = {
        "id": args.started_at,
        "startedAt": args.started_at,
        "finishedAt": args.finished_at,
        "status": status,
        "exitCode": args.exit_code,
        "durationSeconds": duration,
        "downloadedItems": len(downloads),
        "downloadedFiles": downloaded_files,
        "failedItems": len(failures),
        "retryItems": len(retry),
        "waitingUrlItems": len(waiting),
        "erulaboPendingItems": len(erulabo),
        "downloads": downloads,
        "failures": failures,
    }

    previous = load_json(args.output)
    old_runs = previous.get("runs") if isinstance(previous.get("runs"), list) else []
    runs = [run]
    for old in old_runs:
        if not isinstance(old, dict) or old.get("id") == run["id"]:
            continue
        runs.append(old)
        if len(runs) >= MAX_RUNS:
            break

    payload = {
        "version": 1,
        "generatedAt": args.finished_at,
        "schedule": {
            "label": "매주 일요일 04:00",
            "timezone": "Asia/Seoul",
            "weekday": 0,
            "hour": 4,
            "minute": 0,
        },
        "runs": runs,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(args.output.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(args.output)


if __name__ == "__main__":
    main()
