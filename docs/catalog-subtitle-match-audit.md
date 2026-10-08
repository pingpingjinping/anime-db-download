# Read-only catalog and subtitle matching audit

The V4 production matcher handles Anissia ingestion. This command adapts unlinked
Onnada entries and existing AniAll/Naver review hints into temporary Anissia rows
in an in-memory replay. It does not install a new production matcher.

```bash
python3 scripts/audit_catalog_subtitle_matches.py \
  --database /path/to/anime_four_sources_multilingual_prepared.db \
  --registry /path/to/anime_reviewed_subtitle_registry_v1.db \
  --output-dir /path/to/audit-output
python3 -m unittest discover -s scripts -p 'test_audit_catalog_subtitle_matches.py'
```

Both input databases open in read-only mode. SHA-256 is checked before and after.
Existing Onnada ReAnime links and Anikoto links, including links through shared
AniList/ReAnime IDs, are excluded from replay. Existing subtitle work bindings
and confirmed episode numbers are preserved. Kept subtitle files alone count;
fonts and other attachments do not. Filenames are recorded for later review but
never used to invent a work identity, year, season or format.

The CSV files and audit.json distinguish `unique_rule_candidate_review_required`
from `hold`. Neither status is a confirmed correction. Rule-eligible proposals
retain the original engine evidence. Candidate Anikoto IDs are recorded lookups,
not independently validated stream links. The temporary replay disables historical
reservations and retains the full foreign catalog, including Onnada itself; an
Onnada bridge is therefore not independent corroboration for an Onnada input.
Internet validation and any eventual production connection require separate review.

The command has no apply option and never invokes an updater, Release write,
Pi import, episode assignment or production matching refresh. Its output is a
proposal inventory, not a fresh accuracy review of protected existing links.
