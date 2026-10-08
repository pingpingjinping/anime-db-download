# Monthly incremental source refresh

Anissia starts at page 0 of `/anime/list/{page}?q=`. The server orders this empty
query by `animeNo` descending. A nonempty page containing only IDs already in the
four-source DB counts as one known page. Any new ID anywhere on a page resets
the count. Stop after two consecutive entirely known pages, or at the last page.
All rows on fetched pages, including known rows, still refresh their metadata.
Previously stored rows outside those pages remain in the DB and pending work
matches are retried against the complete stored catalog. With no known IDs the
collector walks the whole listing. Repeated/overlapping page IDs are errors,
never another known-page checkpoint.

Anikoto uses `/filter?sort=latest-added&page=1` and the same two-page rule. The
known set comes from the saved **raw catalog**, including unmatched/rejected and
review-needed identities. It is not derived from compact runtime DB links.
Without a completed baseline for that site, it bootstraps a full scan. A restart
resumes at the next saved page and preserves the consecutive-known-page count.
Blocked/empty pages and pages with no new observations leave the scan incomplete
and prevent matching/publication. Full scans reject changed total-page counts
and any cross-page overlap. Incremental scans tolerate different total-page
counts and partial cross-page overlap (both observed in live responses), merging
duplicate observations. Only IDs in the original saved baseline count as known,
including after resume: newly collected IDs cannot become a false checkpoint.
Entirely repeated pages never count as the second known page.

New Anikoto crawl metadata and catalog membership tables migrate on crawl. Each
incremental snapshot keeps its prior completed baseline membership and merges
new observations. Older unvisited works keep their original observation dates;
export, matching, and runtime payload generation use the merged snapshot, not
just the latest fetched pages. Explicit full scans replace snapshot membership
while retaining old raw rows for history. Existing seed/Release catalogs remain
readable without rewriting them during read-only validation.

The monthly workflow uses incremental Anikoto collection automatically. For an
explicit full rebuild, use `crawl_anikoto.py --full --fresh` or select
`full_scan=true` in the manual catalog workflow with `mode=refresh`. No new Pi
timer, web build, or subtitle-download change is required.

ReAnime keeps its full year-sorted API traversal, so newly added older-year
works are not skipped. AniList, Onnada, and AniAll retain their existing refresh
policies. This change limits new-work discovery; it does not periodically
re-fetch all historical metadata or detect historical deletions during an
incremental run.
