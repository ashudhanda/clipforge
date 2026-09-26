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
# Pinned BtbN build tag — NOT "latest". The rolling master changes daily
# (fresh unsigned binaries = maximum antivirus false-positive risk and
# zero reproducibility). Bump deliberately after testing a new build.
# Override: FFMPEG_BUILD_TAG=autobuild-... ./fetch-ffmpeg.sh
BUILD_TAG="${FFMPEG_BUILD_TAG:-autobuild-2026-09-25-15-37}"
case "$PLATFORM" in
  linux64)
    echo "-> finding BtbN FFmpeg-Builds release for tag $BUILD_TAG..."
    API="https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/tags/$BUILD_TAG"
    # NOTE: match the *static* (non-shared) build — the -shared variant needs
    # DLLs/.so files that we do not bundle. Python avoids grep/head SIGPIPE
    # races under `set -o pipefail`.
    URL=$(curl -fsSL "${AUTH[@]}" "$API" | python3 -c "
import json, sys, re
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
for a in data.get('assets', []):
    n = a.get('name', '')
    if re.search(r'linux64-lgpl\.tar\.xz$', n):
        print(a['browser_download_url'])
        break
")
    if [ -z "$URL" ]; then echo "no matching linux64-lgpl asset found for tag $BUILD_TAG"; exit 1; fi

    echo "-> downloading $(basename "$URL")..."
    curl -fSL -o package.arc "$URL"

    echo "-> extracting ffmpeg + ffprobe..."
    rm -rf stage && mkdir stage
    tar -xJf package.arc --no-same-owner -C stage
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
