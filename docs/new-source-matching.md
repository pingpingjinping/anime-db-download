# Pending-work matching

The monthly updater refreshes source metadata and re-evaluates pending work
links. It never rewrites the historical `anissia_year_match` decisions or the
reviewed subtitle registry. Pending identities and generated automatic links
are retried when another source gains a title, year, type, or target record.

## Collection

- Anissia starts at API page **0**, processes that response immediately, and
  scans every page. Already-known IDs do not stop the scan. Both new works and
  later edits to existing works are processed. Missing pages or an incomplete
  unique-ID count fail the update before replacing the release snapshot.
- ReAnime scans to the end of its listing. Known works sorted ahead of a newly
  added old-year work do not cause early termination. Repeated pages and the
  page limit fail closed.
- AniList retains new-ID discovery, includes SPECIAL records, and also refreshes
  recent/upcoming works, pending references, and recent ReAnime declarations
  below the watermark. This can fill references previously omitted by the old
  format filter.
- Onnada checks its full listing, reads new details, and refreshes the latest
  100 known details plus pending Korean-title bridges and recorded references.
  It does not re-fetch every historical detail each month.

## Title and identity evidence

`work_identity.py` keeps raw titles intact and derives title keys, season labels,
part/cour labels, and medium independently. It normalizes Unicode width and
case while preserving sequel symbols such as `+` and stars. Numeric title
content and arbitrary subtitles remain in the key. A trailing `(2025)` release
year is omitted only when the row itself confirms that same year.

Season equivalents include `2기`, `Season 2`, `2nd Season`, `S02`, `第2期`, and
trailing roman numerals. Part/cour labels have their own parser; `Part II` and
`第2クール` do not become season 2. Metadata from all four title languages is
combined, so an unlabelled Japanese title cannot erase a Korean season label.
Contradictory labels within a record require review.

Leading `映画`/`劇場版` labels and trailing TVSP/SPECIAL/OVA/OAD/ONA markers
are separated from the title and kept as type constraints. MOVIE, SPECIAL, OVA,
ONA, and TV are distinct; TV_SHORT is a compatible TV subtype. A missing label
is not assigned a default season or part number.

Candidate generation uses exact normalized title keys, validated AniList
aliases, and corroborated Onnada bridges. A Japanese native-title difference
requires alias review unless a bridge supplies the missing evidence. Latin titles
stored in the native field do not veto an exact match in another language.
A bare trailing digit becomes a season key only when another title language
explicitly confirms that same season. Numeric title content is preserved. Long/short
native-title containment can generate a review hint but never an automatic link.

## Selection and persistence

1. Confirmed season, part, type, episode-count, and year conflicts do not compete
   with an eligible exact candidate. Their reasons remain in the candidate table.
2. Fully corroborated candidates (exact title, known same year, compatible
   metadata and verified reference evidence) outrank incomplete alternatives.
   Unknown-year/season/type/reference candidates remain `weaker_candidate`
   suggestions rather than vetoing a unique complete candidate. Two equally
   complete candidates still require review. The same ranking is used for
   reverse Anissia competitors, including unpending rows.
   An identical complete native title with the same known year can corroborate
   a named sequel's explicit season from the target's other title languages
   when the source has no numeric season. Explicit conflicts remain conflicts;
   an unlabelled title is never automatically assigned season 1.
3. Work uniqueness is checked in both directions, including unpending Anissia
   competitors. Historical reviewed targets and validated alternate URLs for
   reviewed works are reserved.
4. Same-AniList ReAnime URLs are grouped only after title/metadata validation.
   Multiple eligible URLs are retained as `multiple_urls`; no arbitrary URL is
   chosen. A URL suffix alone is not a work ID.
5. Invalid/zero IDs cannot lend titles. Known but contradictory external
   references require review. An absent AniList row does not prevent a direct,
   independently confirmed Anissia-to-ReAnime link, but its ID is not declared
   validated. A fresh provider ID supersedes stale unioned IDs for matching
   without rewriting reviewed links.
6. Candidate-only changes count as `changed`, so they are saved even when no
   automatic link changes. Unchanged candidate evidence keeps its observation
   date, making repeated refreshes stable.

V3 automatic evidence includes `rule_version`, `validated_anilist_ids`,
`work_identity`, conflict/missing reasons, and candidate role. The streaming
consumer must use the validated ID list when expanding V2+ automatic aliases;
it must not borrow all IDs from a ReAnime declaration. Older evidence and
explicit manual review remain compatible.

`needs_review` counts stored candidate rows, including rejected alternatives.
`review_works` counts distinct Anissia works still lacking an automatic link.
An unmatched pending Anissia work gets a `no_catalog_entry` row with score 0.
The existing web importer selects scored candidates; score-0 rows remain in the
catalog audit rather than appearing as a fabricated watch link.

## Verification

The saved 2026 sample contains 50 distinct recent Anissia works with kana in the
original title: 25 selected with seed 20261008, then 25 with seed 20261009 from
the remaining 48 works of a 73-work population. This is a sampled replay, not
an accuracy estimate and not a 2027/unknown-date benchmark.

With the complete foreign catalog and all 3,548 observed Anissia competitors:

| Sample | V2 automatic | V3 automatic | V3 review |
|---|---:|---:|---:|
| 2026 recent 50 | 43 | 44 | 6 |
| 2025 random 50 | 37 | 46 | 4 |
| Additional 50 (2026/upcoming/unknown) | 20 | 21 | 29 |

Every V2 automatic target is retained. The Vermilion Mask gains a confirmed
same-year target while its unknown-year Part 2 alternative remains visible.
The 2025 gains cover multilingual season digits, Latin native fields, a
corroborated remake year, an identical full named-sequel title, and incomplete
future-season alternatives. Remaining holds include known year conflicts,
missing title/season evidence, and equally strong multiple URLs. There are no
per-work title exceptions. These are replay coverage counts, not an accuracy
estimate or playback test. The frozen SQLite input hash is unchanged.

The separate 2025 fixture records 50 fresh details observed on 2026-10-08,
random seed 20261011 from a 233-work population. Its curated targets and exact
expected links are also checked in for offline regression.

The JSON fixture is a curated target subset for offline regression. To replay
against the complete foreign catalog without modifying the input SQLite file:

```sh
python scripts/audit_new_source_matches.py --database /path/to/catalog.db --output /tmp/matching-audit.json
```

Without `--database`, the command uses the checked-in fixture. Both audit modes
disable historical reservations in memory and isolate the sampled Anissia rows
to model new ingestion; they are not a sweep of protected deployed links.

A separate updater replay inserted the 9 new IDs 3598–3606 from page 0 into a
copy of the deployed baseline. It added all nine, produced one unique automatic
link and eight holds, and preserved all 43,348 prior cross-source ID/status rows
and the hashes of nine historical review/registry tables. The input and release
assets were not changed. ReAnime live collection was not exercised because its
endpoint returned 403 in this environment; its pagination has mocked regression
coverage and matching uses the frozen release catalog.

Run the regression suite with:

```sh
python -m unittest discover -s scripts -p 'test_*.py' -v
```
