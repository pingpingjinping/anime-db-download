"""Exact, full-state monthly overlay of Pi administrator work-to-work corrections.

This is deliberately separate from subtitle attachment mappings and inferred
source links. A snapshot may be empty after administrators reset all overrides.
"""
import json
import re

RE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,199}$")
MAX_OVERRIDES = 20000


def checked_overrides(rows):
    if not isinstance(rows, list) or len(rows) > MAX_OVERRIDES:
        raise ValueError("Invalid manual work match snapshot")
    seen_re, seen_ani = set(), set()
    normalized = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"reanime_id", "anissia_no"}:
            raise ValueError("Manual work matches contain unexpected fields")
        rid, aid = row["reanime_id"], row["anissia_no"]
        if (not isinstance(rid, str) or not RE_ID.fullmatch(rid)
                or type(aid) is not int or not 1 <= aid <= 2147483647):
            raise ValueError("Invalid manual work match ID")
        if rid in seen_re or aid in seen_ani:
            raise ValueError("Ambiguous manual match: duplicate ReAnime or Anissia ID")
        seen_re.add(rid)
        seen_ani.add(aid)
        normalized.append((rid, aid))
    if normalized != sorted(normalized):
        raise ValueError("Manual match snapshot must be sorted by ReAnime ID")
    return normalized


def apply_overrides(db, manifest, month):
    """Stage exact IDs into a distinct table; never rewrite reviewed source rows."""
    if (manifest.get("schema") != 1 or manifest.get("month") != month
            or "manual_work_matches" not in manifest
            or type(manifest.get("manual_match_count")) is not int):
        raise ValueError("Missing or malformed full manual work match snapshot")
    matches = checked_overrides(manifest["manual_work_matches"])
    if manifest["manual_match_count"] != len(matches):
        raise ValueError("Manual work match count differs from manifest")
    for rid, aid in matches:
        if not db.execute("SELECT 1 FROM entries WHERE source='ReAnime' AND source_record_id=?", (rid,)).fetchone():
            raise ValueError("Manual ReAnime target is missing from canonical catalog: " + rid)
        if not db.execute("SELECT 1 FROM entries WHERE source='Anissia' AND source_record_id=?", (str(aid),)).fetchone():
            raise ValueError("Manual Anissia target is missing from canonical catalog: " + str(aid))
    db.execute("""CREATE TABLE IF NOT EXISTS pi_manual_work_match_overrides (
       reanime_id TEXT PRIMARY KEY,
       anissia_no INTEGER NOT NULL UNIQUE CHECK(anissia_no>0),
       reviewed_month TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS pi_manual_work_match_overrides_meta (
       key TEXT PRIMARY KEY, value TEXT NOT NULL
    )""")
    # A successful full-state export is authoritative. Removed local overrides
    # must *not* survive as stale Release links on next monthly sync.
    db.execute("DELETE FROM pi_manual_work_match_overrides")
    db.executemany(
        "INSERT INTO pi_manual_work_match_overrides VALUES (?,?,?)",
        [(rid, aid, month) for rid, aid in matches],
    )
    db.execute("""INSERT INTO pi_manual_work_match_overrides_meta VALUES('reviewed_month',?)
       ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (month,))
    return len(matches)


def assert_overlay(db, manifest, month):
    wanted = checked_overrides(manifest["manual_work_matches"])
    existing = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"pi_manual_work_match_overrides","pi_manual_work_match_overrides_meta"} <= existing:
        raise ValueError("Manual work match overlay was lost")
    recorded = db.execute(
        "SELECT value FROM pi_manual_work_match_overrides_meta WHERE key='reviewed_month'").fetchone()
    actual = db.execute(
        "SELECT reanime_id,anissia_no,reviewed_month "
        "FROM pi_manual_work_match_overrides ORDER BY reanime_id").fetchall()
    if (not recorded or recorded[0] != month
            or actual != [(rid, aid, month) for rid, aid in wanted]
            or manifest.get("manual_match_count") != len(wanted)):
        raise ValueError("Manual DB work match overlay differs from Pi snapshot")
    return len(wanted)
