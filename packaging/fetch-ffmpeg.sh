#!/usr/bin/env bash
# Fetch static FFmpeg + ffprobe for the installer.
# Linux: BtbN/FFmpeg-Builds static LGPL tarballs.
# macOS: static-ffmpeg PyPI package (BtbN publishes no macOS builds).
# Never commit the binaries — they are downloaded at build time only.
# Usage: ./fetch-ffmpeg.sh [linux64|macos]
set -euo pipefail

mkdir -p "$(dirname "$0")/bin"
OUT_DIR="$(cd "$(dirname "$0")/bin" && pwd)"
cd "$OUT_DIR"

# Authenticated API calls get a much higher rate limit on shared CI runners.
AUTH=()
if [ -n "${GITHUB_TOKEN:-}" ]; then AUTH=(-H "Authorization: Bearer $GITHUB_TOKEN"); fi

PLATFORM="${1:-linux64}"
case "$PLATFORM" in
  linux64)
    ASSET_PAT="linux64-lgpl.*\.tar\.xz$"
    echo "-> finding latest BtbN FFmpeg-Builds release..."
    API="https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest"
    URL=$(curl -fsSL "${AUTH[@]}" "$API" | grep -oE "\"browser_download_url\": *\"[^\"]*${ASSET_PAT}\"" \
          | head -1 | cut -d'"' -f4)
    if [ -z "$URL" ]; then echo "no matching asset found"; exit 1; fi

    echo "-> downloading $(basename "$URL")..."
    curl -fSL -o package.arc "$URL"

    echo "-> extracting ffmpeg + ffprobe..."
    rm -rf stage && mkdir stage
    tar -xJf package.arc -C stage
    FF=$(find stage -name ffmpeg -type f | head -1)
    FP=$(find stage -name ffprobe -type f | head -1)
    cp "$FF" "$OUT_DIR/ffmpeg"
    cp "$FP" "$OUT_DIR/ffprobe"
    chmod +x "$OUT_DIR/ffmpeg" "$OUT_DIR/ffprobe"
    rm -rf stage package.arc
    ;;
  macos)
    echo "-> fetching static macOS ffmpeg/ffprobe via static-ffmpeg..."
    python3 -m pip install -q static-ffmpeg
    python3 - "$OUT_DIR" <<'EOF'
import os, shutil, stat, sys
from static_ffmpeg import run
out = sys.argv[1]
ffmpeg, ffprobe = run.get_or_fetch_platform_executables_else_raise()
for src, name in ((ffmpeg, "ffmpeg"), (ffprobe, "ffprobe")):
    dst = os.path.join(out, name)
    shutil.copy2(src, dst)
    os.chmod(dst, os.stat(dst).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print("copied:", name)
EOF
    ;;
  *) echo "unknown platform: $PLATFORM (use linux64|macos)"; exit 1 ;;
esac

echo "OK: ffmpeg + ffprobe ready in $OUT_DIR"
"$OUT_DIR/ffmpeg" -hide_banner -version | head -1
