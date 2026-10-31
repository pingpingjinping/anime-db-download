#!/usr/bin/env bash
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
python_bin="/home/pi/.local/share/anime-db-download/venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
  echo "Install the weekly subtitle Python environment before enabling this worker." >&2
  exit 1
fi
"$python_bin" -c 'import requests, bs4, playwright, PIL'
sudo install -m 0644 "$repo/ops/systemd/anime-erulabo-web.service" /etc/systemd/system/anime-erulabo-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now anime-erulabo-web.service
sudo systemctl status anime-erulabo-web.service --no-pager -l
