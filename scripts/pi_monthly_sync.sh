#!/usr/bin/env bash
set -euo pipefail

STREAMING_REPO="${STREAMING_REPO:-/home/pi/streaming-project}"
ARCHIVE_REPO="${ARCHIVE_REPO:-/home/pi/anime-db-download}"
PI_PYTHON="${PI_PYTHON:-/home/pi/.local/share/anime-db-download/venv/bin/python}"

if [[ ! -f "$STREAMING_REPO/scripts/update-anime-db-release.py" ]]; then
  echo "Missing streaming-project DB updater: $STREAMING_REPO/scripts/update-anime-db-release.py" >&2
  exit 1
fi
if [[ ! -f "$ARCHIVE_REPO/scripts/pi_download_subtitle_queues.py" ]]; then
  echo "Missing Pi subtitle queue downloader: $ARCHIVE_REPO/scripts/pi_download_subtitle_queues.py" >&2
  exit 1
fi
if [[ ! -f "$ARCHIVE_REPO/scripts/pi_download_erulabo.py" ]]; then
  echo "Missing Erulabo browser helper: $ARCHIVE_REPO/scripts/pi_download_erulabo.py" >&2
  exit 1
fi

if [[ ! -x "$PI_PYTHON" ]]; then
  PI_PYTHON="/usr/bin/python3"
fi
if ! "$PI_PYTHON" -c 'import requests, bs4' >/dev/null 2>&1; then
  echo "Missing Pi subtitle Python dependencies." >&2
  echo "Run: python3 -m venv ~/.local/share/anime-db-download/venv && ~/.local/share/anime-db-download/venv/bin/pip install -r ~/anime-db-download/requirements-pi-subtitles.txt" >&2
  exit 1
fi

echo "=== [1/4] Anime DB Release ==="
/usr/bin/python3 "$STREAMING_REPO/scripts/update-anime-db-release.py"

echo
echo "=== [2/4] Import new AniAll matches ==="
/usr/bin/python3 "$ARCHIVE_REPO/scripts/pi_import_aniall_matches.py"

echo
echo "=== [3/4] Download queued subtitles from Pi/home IP ==="
if command -v ionice >/dev/null 2>&1; then
  nice -n 15 ionice -c3 "$PI_PYTHON" "$ARCHIVE_REPO/scripts/pi_download_subtitle_queues.py" \
    --target "$STREAMING_REPO/subtitle-data"
else
  nice -n 15 "$PI_PYTHON" "$ARCHIVE_REPO/scripts/pi_download_subtitle_queues.py" \
    --target "$STREAMING_REPO/subtitle-data"
fi

echo
echo "=== [4/4] Prepare pending Erulabo subtitle page ==="
"$PI_PYTHON" "$ARCHIVE_REPO/scripts/pi_download_erulabo.py"

echo
echo "Monthly anime DB + Pi subtitle download completed."
