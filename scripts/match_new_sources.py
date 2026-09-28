"""Link newly discovered ReAnime and Anissia works without changing reviewed links.

Only identities recorded in monthly_match_pending are eligible.  The initial
reviewed snapshot is never swept for additional automatic matches.
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from datetime import date

MIN_CANDIDATE_SCORE = 70
MIN_AUTOMATIC_SCORE = 95


def identity(title):
    value = unicodedata.normalize("NFKC", title or "").casefold()
    return "".join(ch for ch in value if ch.isalnum())


def base_identity(title):
    value = unicodedata.normalize("NFKC", title or "").casefold()
    value = re.sub(r"\b(?:season|시즌|part|파트|cour|쿨)\s*0*\d{1,2}\b", " ", value)
    value = re.sub(r"(?<!\d)0*\d{1,2}\s*(?:기|期)\b|\bs\s*0*\d{1,2}\b", " ", value)
    value = re.sub(r"\b(?:ii|iii|iv|v|vi)\b\s*$", " ", value)
    return identity(value)


def season(title):
    title = unicodedata.normalize("NFKC", title or "")
    patterns = (
        r"(?:season|시즌|제)\s*0*(\d{1,2})(?:\s*기)?\b",
        r"(?<!\d)0*(\d{1,2})\s*(?:기|期)\b",
        r"\bs\s*0*(\d{1,2})\b",
        r"\b0*(\d{1,2})(?:st|nd|rd|th)\s+season\b",
    )
    for pattern in patterns:
        match = re.search(pattern, title, flags=re.I)
        if match:
            return int(match.group(1))
    roman = re.search(r"\b(II|III|IV|V|VI)\b\s*$", title, flags=re.I)
    return {"ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6}.get(roman.group(1).lower()) if roman else None


def part(title):
    match = re.search(r"(?:part|파트|cour|쿨)\s*0*(\d{1,2})\b", title or "", re.I)
    return int(match.group(1)) if match else None


def medium(row):
    value = str(row.get("format") or "").upper()
    title = " ".join(str(row.get(k) or "") for k in ("title_ko", "title_native", "title_english"))
    if "MOVIE" in value or "극장판" in title:
        return "MOVIE"
    if "OVA" in value or re.search(r"\bOVA\b", title, re.I):
        return "OVA"
    if "ONA" in value or re.search(r"\bONA\b", title, re.I):
        return "ONA"
    return "TV" if value in ("TV", "TV_SHORT") else None


def titles(row):
    return [str(row[k]) for k in ("title_ko", "title_native", "title_romaji", "title_english") if row[k]]


def year(row):
    return int(row["year"]) if row["year"] else None


def metadata_compatible(a, b):
    a_seasons = {value for title in titles(a) if (value := season(title)) is not None}
    b_seasons = {value for title in titles(b) if (value := season(title)) is not None}
    a_parts = {value for title in titles(a) if (value := part(title)) is not None}
    b_parts = {value for title in titles(b) if (value := part(title)) is not None}
    if len(a_seasons) > 1 or len(b_seasons) > 1 or len(a_parts) > 1 or len(b_parts) > 1:
        return False
    left = next(iter(a_seasons), None)
    right = next(iter(b_seasons), None)
    # An unlabeled base entry can represent S1; a later season needs its label.
    if left != right and {left, right} != {1, None}:
        return False
    if a_parts != b_parts and (a_parts or b_parts):
        return False
    af, bf = medium(a), medium(b)
    return not (af and bf and af != bf)


def indexed_keys(row):
    return {identity(title) for title in titles(row) if len(identity(title)) >= 4}


def base_keys(row):
    return {base_identity(title) for title in titles(row) if len(base_identity(title)) >= 4}


def load_rows(db, source):
    db.row_factory = __import__("sqlite3").Row
    return {row["source_record_id"]: dict(row) for row in db.execute(
        "SELECT * FROM entries WHERE source=?", (source,))}


def prepare_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS monthly_match_pending (
        source TEXT NOT NULL, source_record_id TEXT NOT NULL,
        PRIMARY KEY(source, source_record_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS monthly_auto_matches (
        reanime_id TEXT PRIMARY KEY, anissia_no INTEGER NOT NULL UNIQUE,
        korean_title TEXT NOT NULL, evidence_json TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS monthly_match_candidates (
        anissia_no INTEGER NOT NULL, reanime_id TEXT NOT NULL,
        score INTEGER NOT NULL, reason TEXT NOT NULL, evidence_json TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        PRIMARY KEY(anissia_no, reanime_id))""")


def refresh_auto_matches(db):
    """Recheck pending primary works and store only unique, corroborated links."""
    prepare_tables(db)
    anissia = load_rows(db, "Anissia")
    reanime = load_rows(db, "ReAnime")
    onnada = load_rows(db, "Onnada")
    anilist = load_rows(db, "AniList")
    pending = {(r[0], r[1]) for r in db.execute(
        "SELECT source,source_record_id FROM monthly_match_pending")}

    reviewed_ani = {str(r[0]) for r in db.execute("SELECT anime_no FROM anissia_year_match")}
    reviewed_re = set()
    for row in anissia.values():
        if row["source_record_id"] not in reviewed_ani:
            continue
        reviewed_re.update(str(x) for x in json.loads(row["cross_source_ids_json"]).get("reanime_ids", []))
    reviewed_re.update(str(r[0]) for r in db.execute(
        "SELECT reanime_id FROM anissia_year_candidates WHERE reanime_id!='' "
        "AND anime_no IN (SELECT anime_no FROM anissia_year_match)"))

    re_keys = defaultdict(set)
    re_bases = defaultdict(set)
    for rid, row in reanime.items():
        if rid not in reviewed_re:
            aliases = [row]
            for aid in json.loads(row["cross_source_ids_json"]).get("anilist_ids", []):
                if str(aid) in anilist:
                    aliases.append(anilist[str(aid)])
            for source in aliases:
                for key in indexed_keys(source):
                    re_keys[key].add(rid)
                for key in base_keys(source):
                    re_bases[key].add(rid)

    on_by_ko = defaultdict(list)
    for row in onnada.values():
        if row["title_ko"]:
            on_by_ko[identity(row["title_ko"])].append(row)

    possible = defaultdict(dict)
    for aid, a in anissia.items():
        if aid in reviewed_ani:
            continue
        evidence_by_re = defaultdict(set)
        for key in indexed_keys(a):
            for rid in re_keys.get(key, ()):
                evidence_by_re[rid].add("exact_title")
        for key in base_keys(a):
            for rid in re_bases.get(key, ()):
                evidence_by_re[rid].add("same_base_title")
        # Onnada bridges a Korean Anissia name to the Japanese/romanized
        # ReAnime or linked AniList name without guessing a translation.
        for on in on_by_ko.get(identity(a.get("title_ko")), ()):
            for key in indexed_keys(on):
                for rid in re_keys.get(key, ()):
                    if key != identity(on.get("title_ko")) and metadata_compatible(on, reanime[rid]):
                        evidence_by_re[rid].add("onnada_bridge:" + on["source_record_id"])
        for rid, evidence in evidence_by_re.items():
            if ("Anissia", aid) not in pending and ("ReAnime", rid) not in pending:
                continue
            r = reanime[rid]
            ry, ay = year(r), year(a)
            support = [item for item in evidence if item.startswith("onnada_bridge:")
                       and year(onnada[item.split(":", 1)[1]]) == ry]
            compatible = metadata_compatible(a, r)
            score = 80 if "exact_title" in evidence else 78 if support else 74
            score += 5 if compatible else -5
            score += 10 if ry and ay and ry == ay else 4 if ry and ay and abs(ry - ay) == 1 else 0
            score += 10 if support else 0
            af, rf = medium(a), medium(r)
            score += 5 if af and rf and af == rf else -10 if af and rf else 0
            score = min(100, max(0, score))
            if score < MIN_CANDIDATE_SCORE:
                continue
            details = {"year": ry, "anissia_year": ay, "evidence": sorted(evidence),
                       "season_compatible": compatible, "onnada_year_support": bool(support)}
            possible[aid][rid] = (score, details)

    by_re = defaultdict(set)
    for aid, options in possible.items():
        for rid in options:
            by_re[rid].add(aid)
    accepted = {}
    candidates = []
    for aid, options in possible.items():
        high = [rid for rid, (score, details) in options.items()
                if score >= MIN_AUTOMATIC_SCORE and details["season_compatible"]]
        for rid, (score, evidence) in options.items():
            unique = len(options) == 1 and len(high) == 1 and len(by_re[rid]) == 1
            confirmed_title = "exact_title" in evidence["evidence"] or evidence["onnada_year_support"]
            auto = (score >= MIN_AUTOMATIC_SCORE and unique and confirmed_title
                    and evidence["season_compatible"] and anissia[aid]["title_ko"]
                    and year(anissia[aid]) and year(anissia[aid]) == year(reanime[rid]))
            if auto:
                accepted[rid] = (int(aid), anissia[aid]["title_ko"], json.dumps(evidence, ensure_ascii=False))
            else:
                reason = "season_or_format" if not evidence["season_compatible"] else (
                    "ambiguous" if not unique and score >= MIN_AUTOMATIC_SCORE else "candidate")
                candidates.append((int(aid), rid, score, reason,
                                   json.dumps(evidence, ensure_ascii=False), date.today().isoformat()))

    old = {tuple(row) for row in db.execute("SELECT reanime_id,anissia_no,korean_title,evidence_json FROM monthly_auto_matches")}
    new = {(rid, *values) for rid, values in accepted.items()}
    if old != new:
        db.execute("DELETE FROM monthly_auto_matches")
        db.executemany("INSERT INTO monthly_auto_matches VALUES (?,?,?,?)", sorted(new))
    db.execute("DELETE FROM monthly_match_candidates")
    db.executemany("INSERT INTO monthly_match_candidates VALUES (?,?,?,?,?,?)", candidates)
    return {"auto_matched": len(new), "needs_review": len(candidates), "changed": old != new}
