# Multi-work DB review — 2026-10-15 (REVIEW ONLY)

This branch contains two **data-only** CSV exports, reconstructed from the 2026-10-14 09:46 KST `db-v1` release's 236 direct multiwork records and review decisions made in conversations.

- `data/matching/multiwork-reviewed-20261015.csv`: 236 rows, preserving **original release number** and **original source key**; do not use conversational ordinal number for database writes.
- `data/matching/multiwork-decisions-20261015.csv`: 54 records with decisions, review holds or explicit changes.

**CAUTION: This commit does not rebuild or publish either ZIP and must not be treated as a database migration.** Existing other-work-20261014 modifications are neither replayed nor taken as a base from local Pi. A release update requires fetching the current published `anime_four_sources_multilingual_prepared.zip` and `anime_reviewed_subtitle_registry_v1.zip`, verifying SHA256 fingerprints, testing every change against row-level expected old values, preserving manual overrides and subtitle-episode mappings, and republishing only after all checks succeed. Do not merge and claim DB is updated.

Resolved decisions: Penguin Drum Re:cycle Anikoto ep1=Your Train Is the Survival Strategy, ep2=I Love You; Slime S3 has 24 standard episodes plus recap specials Diablo's Journal (0) and Luminus Memories (17.5); You're Under Arrest TV episode total remains 51 and OVA 1–4 must not create 55 episodes. The user instructed preserving all unresolved holds, including the 201–250 list.

Priority changes to implement once the live release ZIP is accessible: Onnada:384 fix 1980 Tetsujin 28 to AniList:4208; Onnada:6169 preferred working ReAnime:attack-on-titan-the-last-attack-np7je8; ReAnime:hamelin-no-violin-hiki-jxynbq movie-only AniList:2207; Gintama Porori/Shirogane no Tamashii distinct; and season/part/title corrections, without invalidating manual subtitle overrides.
