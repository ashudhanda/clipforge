#!/usr/bin/env bash
# Local build for Linux: fetches FFmpeg, builds with PyInstaller, tarballs it.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v python3 >/dev/null || { echo "Install Python 3.10+ first"; exit 1; }

python3 -m pip install --quiet -r requirements.txt pyinstaller
./packaging/fetch-ffmpeg.sh linux64
pyinstaller packaging/clipforge2.spec

tar -czf "dist/ClipForge2-0.1.0-linux64.tar.gz" -C dist ClipForge2
echo "DONE: dist/ClipForge2-0.1.0-linux64.tar.gz"
