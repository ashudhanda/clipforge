"""Frozen-aware paths for ClipForge.

When running from source, resources live next to this file. When packaged
with PyInstaller (one-click installer), resources live inside the bundle
(sys._MEIPASS) and ffmpeg/ffprobe are bundled binaries, not PATH tools.

Priority for ffmpeg/ffprobe:
  1. CLIPFORGE_FFMPEG / CLIPFORGE_FFPROBE env vars (explicit override)
  2. Bundled binary next to the frozen app (packaging puts them there)
  3. shutil.which fallback (running from source with system ffmpeg)
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

_FROZEN = getattr(sys, "frozen", False)
_MEIPASS = getattr(sys, "_MEIPASS", None)


def is_frozen() -> bool:
    """True when running inside the PyInstaller bundle."""
    return bool(_FROZEN and _MEIPASS)


def resource_path(*parts: str) -> Path:
    """Path to a bundled resource (templates/, previews/, ...).

    Works both from source and inside the frozen app.
    """
    if is_frozen():
        return Path(_MEIPASS, *parts)  # type: ignore[arg-type]
    return Path(__file__).resolve().parent.parent.joinpath(*parts)


def _bundled_bin(name: str) -> Path | None:
    """ffmpeg/ffprobe shipped inside the installer bundle, if present."""
    if not is_frozen():
        return None
    exe = name + (".exe" if sys.platform == "win32" else "")
    for cand in (Path(_MEIPASS, exe),  # type: ignore[arg-type]
                 Path(_MEIPASS, "bin", exe)):  # type: ignore[arg-type]
        if cand.is_file():
            return cand
    return None


def ffmpeg_path() -> str | None:
    """Best ffmpeg binary path, or None if none found."""
    env = os.environ.get("CLIPFORGE_FFMPEG", "").strip()
    if env:
        return env
    b = _bundled_bin("ffmpeg")
    if b is not None:
        return str(b)
    return shutil.which("ffmpeg")


def ffprobe_path() -> str | None:
    """Best ffprobe binary path, or None if none found."""
    env = os.environ.get("CLIPFORGE_FFPROBE", "").strip()
    if env:
        return env
    b = _bundled_bin("ffprobe")
    if b is not None:
        return str(b)
    return shutil.which("ffprobe")
