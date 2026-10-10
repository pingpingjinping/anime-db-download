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
3. **Day 1, 03:00 KST**: GitHub `monthly-db.yml` verifies that the
   previous month's Pi-reviewed overlay has been published, updates
   the catalog with latest source records, and stages a SHA-256-verified
   `monthly-db-candidate-YYYY-MM` *Actions artifact*. It does **not**
   replace the public `db-v1` release yet.
4. **Day 1, 04:00 KST**: GitHub `monthly-db-publish.yml` loads only the
   successfully completed first-day 03:00 candidate. It compares its
   baseline checksum with the live catalog ZIP, keeps a rollback backup,
   publishes the candidate to `db-v1`, and finally publishes the
   `monthly-db-refresh-YYYY-MM.json` completion marker. If 03:00 failed
   or is still running, do not upload; manually rerun this publisher
   after the candidate is ready. Actions schedules can be delayed.
5. **Day 2, 02:00 KST**: Pi's `anime-monthly-sync.timer` checks the
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
Keep it in the Pi user's private env file (owner pi, mode 0600); do not paste it
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
The first-day phases run separately: a 03:00 failure **must never**
be mistaken for a successful 04:00 publish.
