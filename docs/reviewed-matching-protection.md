# Reviewed matching protection (2026-10-11)

The accepted `db-v1` correction is already published. This change **does not
republish a Release, remap episodes, or deploy the app**. It protects the
approved catalog and the separate reviewed subtitle registry during later
automatic refreshes.

## Authoritative decisions

`data/matching/reviewed-corrections-20261011.json` is an ID-keyed, compact
projection of the verified `codex/publish-matching-20261011` branch's
`review-artifacts/20261011/release-plan.json` (plan SHA-256 recorded inside
the ledger). Its 1,041 changed rows cover:

| Database | Table | Decisions |
| --- | --- | ---: |
| Catalog | `anissia_year_match` | 87 |
| Catalog | `entries` | 87 |
| Registry | `packaged_files_v6` | 583 |
| Registry | `subsubsub_file_registry_v1` | 36 |
| Registry | `subsubsub_file_targets_v1` | 72 |
| Registry | `subtitle_confirmed_episodes` | 36 |
| Registry | `subtitle_review_details` | 13 |
| Registry | `subtitle_work_reanime_links` | 127 |

The ledger contains the old reviewed target (where applicable) and the new
expected ID/status, including deletions of rejected cross-source links.
It **does not contain any subtitle data, original user paths, original
archive names, account information or full annotation JSON**. The
unabridged evidence and exact row-level rollback remain in the original
review branch's plan and packaged audit.

There were **zero new episode assignments**. The 36 corrections to
`subtitle_confirmed_episodes` are holds, not new matches.

## Monthly safety

1. The workflow downloads both published DB archives and the Pi-reviewed
   candidate. The Pi-upload candidate is still verified against its manifest
   and source Release hash.
2. The ledger is validated against the candidate catalog and the published
   registry **before** source refresh.
3. The updater retains an approved work's precise cross-source IDs instead of
   appending unverified provider IDs. A review marker also reserves a held
   Anissia work from the new-source matcher, even if no
   `anissia_year_match` row exists.
4. Before any refresh, the updater snapshots the **actual incoming** reviewed
   links/status and any manual matching/file/episode tables that are present.
   This protects intentional later Pi edits as well as the original review:
   it compares against the state supplied *at the start of that update*, not
   solely the October ledger. A mismatch rolls back and leaves the original
   gzip snapshot unopened for publication.
5. Before ZIP repackaging / publication, the workflow validates the corrected
   IDs, holds and forbidden links again against both DBs. Any mismatch halts
   the monthly release; it never silently overwrites a manual mapping.

## Updating an approved decision

A new explicit review can supersede an earlier October ruling. Do **not**
hand-edit only the published DB or adjust the matcher to bypass the guard.
Record the new verified decision and evidence, update the ID-keyed ledger in
the same PR, and extend the matching regression tests. Keep file IDs,
original episode fields and user-configured mappings unless the new review
explicitly authorizes changing them. The strict ledger intentionally fails
closed on an unregistered override; the updater's independent before/after
snapshot still protects later manual edits while they are being reviewed.

To verify both archives locally:

```bash
python3 scripts/reviewed_matching_guard.py \
  --catalog anime_four_sources_multilingual_prepared.zip \
  --registry anime_reviewed_subtitle_registry_v1.zip
python3 -m unittest -v scripts.test_reviewed_matching_guard \
  scripts.test_update_db scripts.test_match_new_sources
```

This guard is separate from the `streaming-project` player, UI and
manually assigned watch-episode preference state. That work is in app PR #141.
