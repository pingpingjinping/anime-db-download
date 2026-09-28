#!/usr/bin/env bash
set -euo pipefail

STREAMING_REPO="${STREAMING_REPO:-/home/pi/streaming-project}"
ARCHIVE_REPO="${ARCHIVE_REPO:-/home/pi/anime-db-download}"

if [[ ! -f "$STREAMING_REPO/scripts/update-anime-db-release.py" ]]; then
  echo "Missing streaming-project DB updater: $STREAMING_REPO/scripts/update-anime-db-release.py" >&2
  exit 1
fi
if [[ ! -f "$ARCHIVE_REPO/scripts/pi_sync_subtitle_releases.py" ]]; then
  echo "Missing subtitle Release sync: $ARCHIVE_REPO/scripts/pi_sync_subtitle_releases.py" >&2
  exit 1
fi

echo "=== [1/2] Anime DB Release ==="
python3 "$STREAMING_REPO/scripts/update-anime-db-release.py"

echo
echo "=== [2/2] Subtitle delta Releases ==="
python3 "$ARCHIVE_REPO/scripts/pi_sync_subtitle_releases.py" \
  --target "$STREAMING_REPO/subtitle-data"

echo
echo "Monthly anime DB + subtitle sync completed."
