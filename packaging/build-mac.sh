#!/usr/bin/env bash
# Double-clickable local build for macOS: fetches FFmpeg, builds the .app,
# then wraps it into a .dmg. (First run: chmod +x packaging/build-mac.sh)
set -euo pipefail
cd "$(dirname "$0")/.."

command -v python3 >/dev/null || { echo "Install Python 3.10+ first: https://www.python.org/downloads/"; exit 1; }

python3 -m pip install --quiet -r requirements.txt pyinstaller

ARCH="$(uname -m)"
if [ "$ARCH" = "arm64" ]; then ./packaging/fetch-ffmpeg.sh macos-arm64
else ./packaging/fetch-ffmpeg.sh macos-x86_64; fi

pyinstaller packaging/clipforge.spec

DMG="dist/ClipForge-0.1.0.dmg"
rm -f "$DMG"
hdiutil create -volname "ClipForge" -srcfolder "dist/ClipForge.app" \
  -ov -format UDZO "$DMG"
echo "DONE: $DMG"
echo "Note: unsigned — first launch needs right-click → Open (Gatekeeper)."
