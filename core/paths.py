"""Frozen-aware paths for ClipForge.

Works from a cloned repo AND from the installed .exe.

ffmpeg/ffprobe resolution order (first hit wins):
  1. CLIPFORGE_FFMPEG / CLIPFORGE_FFPROBE env vars (explicit override)
  2. Bundled binary inside the PyInstaller bundle (sys._MEIPASS)
  3. Repo-local packaging/bin/ (cloned repo after running fetch-ffmpeg.*)
  4. System PATH (shutil.which)

``is_frozen()`` reads ``sys.frozen``/``sys._MEIPASS`` dynamically (not at
import time) so tests can simulate the frozen layout by monkeypatching
``sys`` attributes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def is_frozen() -> bool:
    """True when running inside the PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None))


def _meipass() -> Path | None:
    mp = getattr(sys, "_MEIPASS", None)
    return Path(mp) if mp else None


def _exe_name(name: str) -> str:
    return name + (".exe" if sys.platform == "win32" else "")


def resource_path(*parts: str) -> Path:
    """Path to a bundled resource (templates/, assets/fonts/, ...).

    Works both from source and inside the frozen app.
    """
    if is_frozen():
        mp = _meipass()
        assert mp is not None
        return mp.joinpath(*parts)
    return Path(__file__).resolve().parent.parent.joinpath(*parts)


def _bundled_bin(name: str) -> Path | None:
    """ffmpeg/ffprobe shipped inside the installer bundle, if present."""
    mp = _meipass()
    if not is_frozen() or mp is None:
        return None
    exe = _exe_name(name)
    for cand in (mp / exe, mp / "bin" / exe):
        if cand.is_file():
            return cand
    return None


def _repo_bin(name: str) -> Path | None:
    """packaging/bin/ next to a cloned repo (fetch-ffmpeg.* output)."""
    cand = Path(__file__).resolve().parent.parent / "packaging" / "bin" / _exe_name(name)
    return cand if cand.is_file() else None


def _resolve_bin(name: str, env_var: str) -> tuple[str | None, str | None]:
    """Return (path, source). Source is one of env|bundled|repo|system."""
    env = os.environ.get(env_var, "").strip()
    if env:
        return env, "env"
    b = _bundled_bin(name)
    if b is not None:
        return str(b), "bundled"
    r = _repo_bin(name)
    if r is not None:
        return str(r), "repo"
    w = shutil.which(name)
    if w:
        return w, "system"
    return None, None


def ffmpeg_path() -> str | None:
    """Best ffmpeg binary path, or None if none found."""
    path, _src = _resolve_bin("ffmpeg", "CLIPFORGE_FFMPEG")
    return path


def ffprobe_path() -> str | None:
    """Best ffprobe binary path, or None if none found."""
    path, _src = _resolve_bin("ffprobe", "CLIPFORGE_FFPROBE")
    return path


def ffmpeg_dir() -> str | None:
    """Directory holding the ffmpeg binary (for yt-dlp's ``ffmpeg_location``)."""
    p = ffmpeg_path()
    return str(Path(p).parent) if p else None


def _bin_version(path: str) -> str | None:
    try:
        out = subprocess.run(
            [path, "-hide_banner", "-version"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout:
            return out.stdout.splitlines()[0].strip()[:120]
    except Exception:
        pass
    return None


def ffmpeg_status() -> dict:
    """Machine-readable ffmpeg/ffprobe health for the dashboard.

    Never raises: on any failure reports ``found: False`` with guidance.
    """
    ff_path, ff_src = _resolve_bin("ffmpeg", "CLIPFORGE_FFMPEG")
    fp_path, fp_src = _resolve_bin("ffprobe", "CLIPFORGE_FFPROBE")
    found = bool(ff_path and fp_path)
    return {
        "found": found,
        "ffmpeg": {"path": ff_path, "source": ff_src,
                   "version": _bin_version(ff_path) if ff_path else None},
        "ffprobe": {"path": fp_path, "source": fp_src,
                    "version": _bin_version(fp_path) if fp_path else None},
        "guidance": None if found else missing_ffmpeg_guidance(),
    }


def missing_ffmpeg_guidance() -> str:
    """Plain-language help shown when ffmpeg/ffprobe can't be found."""
    if is_frozen():
        return (
            "FFmpeg wasn't found next to the app. If you just installed or "
            "updated ClipForge, Windows Security may have quarantined the "
            "bundled ffmpeg.exe (it flags fresh unsigned builds). Open "
            "Windows Security → Protection history, look for ffmpeg.exe, "
            "choose Restore, then add the ClipForge install folder as an "
            "exclusion (Virus & threat protection → Manage settings → "
            "Exclusions). Reinstalling also restores the files."
        )
    return (
        "FFmpeg wasn't found. Either install it on your system "
        "(https://ffmpeg.org/download.html), run "
        "packaging/fetch-ffmpeg.ps1 (Windows) or packaging/fetch-ffmpeg.sh "
        "(Linux/macOS) to drop static builds into packaging/bin/, or point "
        "CLIPFORGE_FFMPEG at your ffmpeg binary."
    )
