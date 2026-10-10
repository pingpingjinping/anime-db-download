# Monthly anime catalog: last day -> day 1 -> day 2 (KST)

This workflow is staged. **Merging GitHub PRs alone does not install or
enable new systemd timers on an existing WalnutPi.**

## Schedule

1. **Last calendar day, 18:00 KST**: Pi's
   `anime-monthly-review-export.timer` exports *only* confirmed
   cross-source work IDs, title, season and media kind from
   `anime_admin_subtitle_links` and `anime_admin_subtitle_link_members`
   to `db-v1` Release asset `pi-reviewed-work-groups-YYYY-MM.json`.
   It NEVER uploads local paths, subtitle contents, usernames or secrets.
   It fails rather than silently rewriting an already published different
   manifest.
2. **Last day, 23:15 KST**: GitHub `monthly-reviewed-works.yml`
   validates IDs against the canonical SQLite catalog, keeps multi-season
   source records separate and inserts a durable
   `pi_reviewed_work_groups` overlay in the `db-v1` catalog ZIP.
   A conflict or missing Pi asset fails this workflow without replacing
   the published catalog. No source records are overwritten.
3. **Day 1, 10:17 KST**: Existing GitHub monthly catalog refresh first
   checks the previous month's submitted Pi overlay. It then collects
   new source records, preserving the reviewed overlay. On successful
   completion it publishes a `monthly-db-refresh-YYYY-MM.json` marker
   containing the release catalog asset SHA-256.
4. **Day 2, 02:00 KST**: Pi's `anime-monthly-sync.timer` checks the
   current-month completion marker and catalog SHA-256 *before* importing
   the release. If missing/mismatched, it fails closed. After validation,
   Pi imports the canonical catalog + durable groups and the AniAll queue.

Individual administrator subtitle assignments remain **immediately active**
on Pi between release cycles. The monthly SQLite overlay is a durable
catalog identity registry, not a backup for Pi's local file paths.

## One-time Pi setup after both PRs are merged

Update the two repositories first:

```bash
cd ~/streaming-project && git pull --ff-only
cd ~/anime-db-download && git pull --ff-only
```

The existing `/home/pi/.config/streaming-project/anime-db.env` already
supplies `ANIME_DB_GITHUB_TOKEN` for the private release download.
Additionally supply **a separate fine-grained GitHub token**
`ANIME_DB_GITHUB_UPLOAD_TOKEN`, scoped *only* to
`pingpingjinping/anime-db-download` with **Contents: Read and write**.
Keep it only in the Pi's root-owner-readable env file; do not paste it
into chat, logs, GitHub source, or a public commit. GitHub release assets
are treated as public metadata; NEVER put private paths in them.

Install both timers:

```bash
sudo install -m 644 ~/anime-db-download/ops/systemd/anime-monthly-review-export.service /etc/systemd/system/
sudo install -m 644 ~/anime-db-download/ops/systemd/anime-monthly-review-export.timer /etc/systemd/system/
sudo install -m 644 ~/anime-db-download/ops/systemd/anime-monthly-sync.service /etc/systemd/system/
sudo install -m 644 ~/anime-db-download/ops/systemd/anime-monthly-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now anime-monthly-review-export.timer anime-monthly-sync.timer
systemctl list-timers --all | grep anime-monthly
```

Dry-run the Pi export (no upload):

```bash
cd ~/anime-db-download
python3 scripts/pi_export_reviewed_work_groups.py --dry-run --force
```

Inspect last-run output via:

```bash
journalctl -u anime-monthly-review-export.service -n 80 --no-pager
journalctl -u anime-monthly-sync.service -n 80 --no-pager
```

On a failure: do not run the day-two importer against a stale release.
Fix the review conflict / GitHub outage and manually rerun the failed
workflow. Do not delete or reset the Pi's administrator mappings to
force a successful export. GitHub Actions scheduled workflows can run
late; the preflight marker is required precisely to prevent stale imports.
