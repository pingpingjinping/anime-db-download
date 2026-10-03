#!/usr/bin/env bash
set -euo pipefail

STREAMING_REPO="${STREAMING_REPO:-/home/pi/streaming-project}"
ARCHIVE_REPO="${ARCHIVE_REPO:-/home/pi/anime-db-download}"
PI_PYTHON="${PI_PYTHON:-/home/pi/.local/share/anime-db-download/venv/bin/python}"

if [[ ! -f "$ARCHIVE_REPO/scripts/pi_download_subtitle_queues.py" ]]; then
  echo "Missing Pi subtitle queue downloader: $ARCHIVE_REPO/scripts/pi_download_subtitle_queues.py" >&2
  exit 1
fi
if [[ ! -f "$ARCHIVE_REPO/scripts/pi_download_erulabo.py" ]]; then
  echo "Missing Erulabo browser helper: $ARCHIVE_REPO/scripts/pi_download_erulabo.py" >&2
  exit 1
fi
if [[ ! -f "$STREAMING_REPO/scripts/match-weekly-aniall.py" ]]; then
  echo "Update streaming-project first: missing match-weekly-aniall.py" >&2
  exit 1
fi

if [[ ! -x "$PI_PYTHON" ]]; then
  PI_PYTHON="/usr/bin/python3"
fi
if ! "$PI_PYTHON" -c 'import requests, bs4, playwright' >/dev/null 2>&1; then
  echo "Missing Pi subtitle Python dependencies." >&2
  echo "Run: python3 -m venv ~/.local/share/anime-db-download/venv && ~/.local/share/anime-db-download/venv/bin/pip install -r ~/anime-db-download/requirements-pi-subtitles.txt" >&2
  exit 1
fi

echo "=== [1/5] Download queued subtitles from Pi/home IP ==="
if command -v ionice >/dev/null 2>&1; then
  nice -n 15 ionice -c3 "$PI_PYTHON" "$ARCHIVE_REPO/scripts/pi_download_subtitle_queues.py" \
    --target "$STREAMING_REPO/subtitle-data"
else
  nice -n 15 "$PI_PYTHON" "$ARCHIVE_REPO/scripts/pi_download_subtitle_queues.py" \
    --target "$STREAMING_REPO/subtitle-data"
fi

echo
echo "=== [2/5] Process pending Erulabo subtitles ==="
"$PI_PYTHON" "$ARCHIVE_REPO/scripts/pi_download_erulabo.py" \
  --target "$STREAMING_REPO/subtitle-data"

echo
echo "=== [3/5] Import published exact AniAll mappings ==="
/usr/bin/python3 "$ARCHIVE_REPO/scripts/pi_import_aniall_matches.py"

echo
echo "=== [4/5] Match newly downloaded AniAll posts ==="
/usr/bin/python3 "$STREAMING_REPO/scripts/match-weekly-aniall.py" \
  --target "$STREAMING_REPO/subtitle-data"

echo
echo "=== [5/5] Remove identical viewing caches for new downloads ==="
if ! (cd "$STREAMING_REPO" && docker compose exec -T anime nice -n 15 node scripts/subtitle-cache/cleanup.cjs); then
  echo "Cache cleanup deferred: pending jobs kept. Ensure the updated anime image is running." >&2
fi

echo
echo "Weekly Pi subtitle sync completed."
