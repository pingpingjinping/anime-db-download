"""Conservative title keys and independent installment/type evidence.

Unknown metadata is distinct from a confirmed conflict. Raw catalog titles are
never rewritten by these helpers.
"""
import json
import re
import unicodedata
from functools import lru_cache

TITLE_FIELDS = ("title_ko", "title_native", "title_romaji", "title_english")
SEASON_PATTERNS = (
    r"第\s*0*(\d{1,2})\s*期",
    r"\b0*(\d{1,2})(?:st|nd|rd|th)\s+season\b",
    r"(?:\bseason|시즌|제)\s*0*(\d{1,2})(?:\s*기)?\b",
    r"(?<!\d)0*(\d{1,2})\s*기\b",
    r"\bs\s*0*(\d{1,2})\b",
)
PART_PATTERNS = (
    r"(?:\bpart|파트|\bcour|쿨)\s*0*(\d{1,2})\b",
    r"第\s*0*(\d{1,2})\s*(?:クール|部)",
    r"\b0*(\d{1,2})(?:st|nd|rd|th)\s+(?:part|cour)\b",
    r"(?<!\d)0*(\d{1,2})\s*쿨\b",
)
PART_ROMAN = r"(?:\bpart|파트|\bcour|쿨)\s*(ii|iii|iv|v|vi)\b"
ROMAN_PATTERN = r"\b(ii|iii|iv|v|vi)\b\s*$"
ROMAN = {"ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6}
TYPE_PREFIX = r"^(?:映画\s+|劇場版\s*|영화\s+|극장판\s*)"
TYPE_SUFFIX = r"\b(?:tvsp|tv\s*special|specials?|sp|ova|oad|ona)\b\s*$"


def normalized(value):
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def identity(title):
    # These symbols can distinguish a sequel/special from the original work.
    return "".join(c for c in normalized(title) if c.isalnum() or c in "+☆★*∞♭♯")


@lru_cache(maxsize=65536)
def parse_title(title):
    value = normalized(title)
    seasons, parts = set(), set()
    for pattern in SEASON_PATTERNS:
        seasons.update(int(m.group(1)) for m in re.finditer(pattern, value, re.I))
        value = re.sub(pattern, " ", value, flags=re.I)
    for pattern in PART_PATTERNS:
        parts.update(int(m.group(1)) for m in re.finditer(pattern, value, re.I))
        value = re.sub(pattern, " ", value, flags=re.I)
    parts.update(ROMAN[m.group(1)] for m in re.finditer(PART_ROMAN, value))
    value = re.sub(PART_ROMAN, " ", value)
    match = re.search(ROMAN_PATTERN, value)
    if match:
        seasons.add(ROMAN[match.group(1)])
        value = value[:match.start()]
    value = re.sub(TYPE_PREFIX, " ", value)
    value = re.sub(TYPE_SUFFIX, " ", value)
    return identity(value), frozenset(seasons), frozenset(parts)


def base_identity(title):
    return parse_title(title or "")[0]


def season(title):
    values = parse_title(title or "")[1]
    return next(iter(values)) if len(values) == 1 else None


def part(title):
    values = parse_title(title or "")[2]
    return next(iter(values)) if len(values) == 1 else None


def titles(row):
    return [str(row.get(f)) for f in TITLE_FIELDS if row.get(f)]


def metadata(row):
    value = json.loads(row.get("source_metadata_json") or "{}")
    return value if isinstance(value, dict) else {}


def positive_int(value):
    return int(str(value)) if re.fullmatch(r"[1-9]\d*", str(value or "")) else None


def year(row):
    value = positive_int(row.get("year"))
    return value if value and 1900 <= value <= 2200 else None


def episodes(row):
    meta = metadata(row)
    return next((n for f in ("episodes_total", "onnada_episodes", "episodes")
                 if (n := positive_int(meta.get(f))) is not None), None)


def declared_anilist_ids(row):
    meta = metadata(row)
    # A fresh provider declaration supersedes stale IDs previously unioned into
    # the catalog. This does not mutate any reviewed cross-source links.
    values = [meta['anilist_id']] if 'anilist_id' in meta else json.loads(
        row.get('cross_source_ids_json') or '{}').get('anilist_ids', [])
    return {str(x) for x in values if positive_int(x)}


def installment(row):
    seasons, parts = set(), set()
    for title in titles(row):
        _, ss, ps = parse_title(title)
        seasons.update(ss)
        parts.update(ps)
    return seasons, parts


def title_medium(title):
    value = normalized(title)
    if re.search(TYPE_PREFIX, value):
        return "MOVIE"
    if re.search(r"\b(?:tvsp|tv\s*special|specials?|sp)\b\s*$|스페셜\s*$|特別編\s*$", value):
        return "SPECIAL"
    if re.search(r"\b(?:ova|oad)\b\s*$", value):
        return "OVA"
    if re.search(r"\bona\b\s*$", value):
        return "ONA"
    return None


def medium(row):
    raw = str(row.get("format") or "").upper().replace(" ", "_")
    declared = {"TV_SHORT": "TV", "TV_SPECIAL": "SPECIAL", "OAD": "OVA"}.get(raw, raw)
    meta = metadata(row)
    category = meta.get("category") or meta.get("onnada_category")
    category = {"TV 시리즈": "TV", "극장판": "MOVIE", "영화": "MOVIE", "OVA": "OVA",
                "ONA": "ONA", "스페셜": "SPECIAL", "TV 스페셜": "SPECIAL", "특전": "SPECIAL"}.get(category)
    types = {title_medium(t) for t in titles(row)} - {None}
    if declared in {"TV", "MOVIE", "OVA", "ONA", "SPECIAL", "MUSIC"}:
        types.add(declared)
    if category:
        types.add(category)
    return next(iter(types)) if len(types) == 1 else "CONFLICT" if len(types) > 1 else None


def indexed_keys(row):
    return {identity(t) for t in titles(row) if len(identity(t)) >= 4}


def row_title_keys(row, title):
    """Use row metadata to disambiguate remake years and corroborated digits."""
    value = normalized(title)
    # A release-year annotation is removable only when the row confirms it.
    if year(row):
        value = re.sub(r"\s*\(" + str(year(row)) + r"\)\s*$", "", value)
    key = base_identity(value)
    ss, _ = installment(row)
    trailing = re.search(r"(?<!\d)([2-9]|[1-9]\d)\s*$", value)
    if trailing and not parse_title(value)[1] and ss == {int(trailing.group(1))}:
        key = base_identity(value[:trailing.start()])
    return {key} if len(key) >= 4 else set()


def base_keys(row):
    return set().union(*(row_title_keys(row, t) for t in titles(row)))


def native_keys(row):
    return row_title_keys(row, row.get("title_native"))


def compatibility(a, b):
    conflicts, missing = [], []
    for label, left, right in zip(("season", "part"), installment(a), installment(b)):
        if len(left) > 1 or len(right) > 1:
            conflicts.append(label + "_inconsistent")
        elif left and right and left != right:
            conflicts.append(label + "_conflict")
        elif bool(left) != bool(right):
            missing.append(label + "_missing")
    af, bf = medium(a), medium(b)
    if "CONFLICT" in (af, bf) or af and bf and af != bf:
        conflicts.append("format_conflict")
    # Anissia commonly omits the format entirely. A known ReAnime type plus
    # exact title is usable; an explicit source type with no target type is not.
    elif not bf:
        missing.append("format_missing")
    ay, by = year(a), year(b)
    if ay and by and ay != by:
        conflicts.append("year_conflict")
    elif not ay or not by:
        missing.append("year_missing")
    ae, be = episodes(a), episodes(b)
    if ae and be and ae != be:
        conflicts.append("episode_count_conflict")
    return conflicts, missing


def validated_reference(r, a):
    conflicts, missing = compatibility(r, a)
    # Never borrow a title or group URLs from an uncorroborated declared ID.
    return not conflicts and not missing and bool(base_keys(r) & base_keys(a))
