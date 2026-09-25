# Third-party notices (ClipForge2 installers)

ClipForge2 itself is MIT-licensed. The one-click installers bundle the
following third-party components, fetched at build time (never committed
to this repo):

## FFmpeg (LGPL builds)

- Source: [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds)
  (`*-lgpl` variants), which build from [ffmpeg.org](https://ffmpeg.org)
  plus [cisco/openh264](https://github.com/cisco/openh264).
- License: **GNU Lesser General Public License (LGPL)** — the `lgpl`
  builds exclude GPL-only encoders (libx264/x265) and use OpenH264 instead,
  which is all ClipForge2 needs for Shorts.
- ClipForge2 runs FFmpeg/ffprobe as **separate processes** (subprocess) —
  it does not link against them — so the MIT license of ClipForge2's own
  code is unaffected (mere aggregation).
- The exact build scripts and source revisions are pinned in
  `packaging/fetch-ffmpeg.sh` / `fetch-ffmpeg.ps1`; LGPL source for the
  FFmpeg version used is available from the links above.

## PyInstaller bootloader

- Source: [pyinstaller/pyinstaller](https://github.com/pyinstaller/pyinstaller)
- License: **GPL with a bootloader exception** — applications built with
  PyInstaller may be distributed under any license (including MIT). Only
  modifications to PyInstaller itself would need to be shared; we ship it
  unmodified.

## CPython

- Source: [python/cpython](https://github.com/python/cpython)
- License: **Python Software Foundation License** (permissive) — bundled
  inside the PyInstaller bootloader payload.
