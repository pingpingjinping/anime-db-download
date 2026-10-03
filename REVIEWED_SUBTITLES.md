# Reviewed subtitle release

The db-v1 ZIP keeps its canonical filename and exactly one SQLite database. The existing four-source catalog from the current release is preserved. The completed v6 subtitle registry and all 1,109 review bundles are added separately, retaining stable file IDs and physical archive paths.

AniAll review scope: 771 bundles / 9,983 subtitle files. 9,928 files have confirmed work and ReAnime links; 55 remain unlinked. Archive outcomes: 756 confirmed, 9 partially confirmed, 6 held. The physical `aniall/미확정/` path is deliberately retained even for newly confirmed files. Review status is in `subtitle_runtime_files`, not the folder name.

Registry totals: 55,530 subtitle files, 55,222 confirmed work links, 308 unlinked, 12,055 preserved confirmed episode rows. No new episode numbers are assigned. The monthly updater refreshes derived ReAnime lookups using declared exact IDs while leaving reviewed file and episode rows untouched. Ambiguous IDs remain manual candidates.

`scripts/merge-reviewed-runtime.py` is a one-time migration tool accepting a current release ZIP and a reviewed registry capsule. It verifies input hashes and preserved row hashes. Large migration payloads are excluded from the main branch.
