#!/usr/bin/env bash
set -euo pipefail

STREAMING_REPO="${STREAMING_REPO:-/home/pi/streaming-project}"
ARCHIVE_REPO="${ARCHIVE_REPO:-/home/pi/anime-db-download}"

if [[ ! -f "$STREAMING_REPO/scripts/update-anime-db-release.py" ]]; then
  echo "Missing streaming-project DB updater: $STREAMING_REPO/scripts/update-anime-db-release.py" >&2
  exit 1
fi
if [[ ! -f "$ARCHIVE_REPO/scripts/pi_import_aniall_matches.py" ]]; then
  echo "Missing AniAll mapping importer: $ARCHIVE_REPO/scripts/pi_import_aniall_matches.py" >&2
  exit 1
fi

echo "=== [1/2] Anime DB Release ==="
/usr/bin/python3 "$STREAMING_REPO/scripts/update-anime-db-release.py"

echo
echo "=== [2/2] Import new AniAll matches ==="
/usr/bin/python3 "$ARCHIVE_REPO/scripts/pi_import_aniall_matches.py"

echo
echo "Monthly anime DB sync completed."
