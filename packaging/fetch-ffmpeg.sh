#!/usr/bin/env bash
# Fetch static LGPL FFmpeg + ffprobe builds for the installer (BtbN/FFmpeg-Builds).
# Never commit the binaries — they are downloaded at build time only.
# Usage: ./fetch-ffmpeg.sh [linux64|macos-arm64|macos-x86_64]
set -euo pipefail

OUT_DIR="$(cd "$(dirname "$0")/bin" && pwd)"
mkdir -p "$OUT_DIR"
cd "$OUT_DIR"

PLATFORM="${1:-linux64}"
case "$PLATFORM" in
  linux64)      ASSET_PAT="linux64-lgpl.*\.tar\.xz$" ;;
  macos-arm64)  ASSET_PAT="macos-arm64-lgpl.*\.zip$" ;;
  macos-x86_64) ASSET_PAT="macos64-lgpl.*\.zip$" ;;
  *) echo "unknown platform: $PLATFORM (use linux64|macos-arm64|macos-x86_64)"; exit 1 ;;
esac

echo "→ finding latest BtbN FFmpeg-Builds release…"
API="https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest"
URL=$(curl -fsSL "$API" | grep -oE "\"browser_download_url\": *\"[^\"]*${ASSET_PAT}\"" \
      | head -1 | cut -d'"' -f4)
if [ -z "$URL" ]; then echo "no matching asset found"; exit 1; fi

echo "→ downloading $(basename "$URL")…"
curl -fSL -o package.arc "$URL"

echo "→ extracting ffmpeg + ffprobe…"
rm -rf stage && mkdir stage
if [[ "$URL" == *.tar.xz ]]; then
  tar -xJf package.arc -C stage
else
  unzip -q -o package.arc -d stage
fi
FF=$(find stage -name ffmpeg -type f | head -1)
FP=$(find stage -name ffprobe -type f | head -1)
cp "$FF" "$OUT_DIR/ffmpeg"
cp "$FP" "$OUT_DIR/ffprobe"
chmod +x "$OUT_DIR/ffmpeg" "$OUT_DIR/ffprobe"
rm -rf stage package.arc
echo "✓ ffmpeg + ffprobe ready in $OUT_DIR"
"$OUT_DIR/ffmpeg" -hide_banner -version | head -1
