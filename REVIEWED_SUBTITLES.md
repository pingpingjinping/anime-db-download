# Reviewed subtitle release

The reviewed v6 subtitle registry is distributed independently from the monthly anime catalog.

- Catalog asset: `db-v1/anime_four_sources_multilingual_prepared.zip`
- Registry asset: `db-v1/anime_reviewed_subtitle_registry_v1.zip`
- Rollback asset created by the migration workflow: `anime_four_sources_multilingual_prepared_combined_rollback.zip`

The catalog asset keeps the four-source reviewed catalog and Anikoto runtime tables. It does not contain the large reviewed subtitle file registry after migration. The registry asset keeps the completed v6 file/archive/review/episode tables in their normalized form. The Pi importer reconstructs the runtime rows from those tables and fills only missing catalog-derived title/format/ReAnime fields from PostgreSQL, so the catalog is not duplicated inside the registry asset.

AniAll review scope: 771 bundles / 9,983 subtitle files. 9,928 files have confirmed work and ReAnime links; 55 remain unlinked. Archive outcomes: 756 confirmed, 9 partially confirmed, 6 held. The physical `aniall/미확정/` path is deliberately retained even for newly confirmed files. Review status is data, not the folder name.

Registry totals: 55,530 subtitle files, 55,222 confirmed work links, 308 unlinked, 12,055 preserved confirmed episode rows. No new episode numbers are assigned. On the Pi, catalog import refreshes only derived ReAnime lookup rows and fills missing derived metadata; existing non-empty reviewed ReAnime values and every `is_manual=true` subtitle row are preserved.

## Migration and rollback

`scripts/split_db_v1_release.py` verifies SQLite integrity and row hashes while splitting the existing combined asset. Every non-registry table in the catalog is hashed before/after the split, and every registry source table is copied with row-hash verification. The Release migration workflow defaults to preview-only.

**Actions → Split db-v1 release assets**:

- `mode=migrate, publish=false`: download the live combined Release, split it, and report exact sizes without changing the Release.
- `mode=migrate, publish=true`: first preserve the original combined asset under the rollback filename, then publish the catalog-only canonical asset and standalone registry asset.
- `mode=rollback, publish=true`: restore the saved combined asset to the canonical filename and remove the split registry/validation assets.

The streaming-project updater accepts both layouts. This means the consumer can be deployed before the Release migration and the producer can be rolled back without losing reviewed registry data.

`scripts/merge-reviewed-runtime.py` remains a legacy one-time tool for constructing a combined database from the original v6 capsule. Large migration payloads are excluded from the main branch.
