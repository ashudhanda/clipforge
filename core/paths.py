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

import importlib.util
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


def _package_dir(package: str) -> Path | None:
    """Directory a package's data files live in, without importing it.

    Frozen: the package's data root inside the bundle (sys._MEIPASS), which
    is exactly where the runtime lookups (importlib.resources,
    cv2.data.haarcascades, googleapiclient's DISCOVERY_DOC_DIR) resolve.
    Dev: the installed package's directory via the import system.
    Returns None when the package can't be located.
    """
    mp = _meipass()
    if is_frozen() and mp is not None:
        return mp / package
    try:
        spec = importlib.util.find_spec(package)
    except Exception:
        return None
    if spec is None:
        return None
    origin = getattr(spec, "origin", None)
    if origin:
        return Path(origin).parent
    locs = getattr(spec, "submodule_search_locations", None)
    if locs:
        return Path(next(iter(locs)))
    return None


def bundle_self_check() -> list[dict]:
    """First-run verification of critical bundled assets.

    Never raises. Returns a list of findings, each
    ``{"level": "error"|"warning", "asset": str, "detail": str}``:

    - ``error`` — a core job cannot run without this (ffmpeg/ffprobe,
      templates/, the YouTube discovery doc). Surface LOUDLY at startup.
    - ``warning`` — graceful degradation (fonts, Haar cascade, VAD model:
      callers fall back without crashing, but output quality drops).

    Call this at startup — before serving traffic or running jobs — so a
    broken bundle fails LOUDLY with a clear message instead of dying
    mid-job (the v0.1.6 class of incident). Wiring it into app startup is a
    product decision; this helper only reports.
    """
    findings: list[dict] = []

    def _add(level: str, asset: str, detail: str) -> None:
        findings.append({"level": level, "asset": asset, "detail": detail})

    # 1. ffmpeg + ffprobe: no binary, no clips, no transcription, no upload.
    for name, env_var in (("ffmpeg", "CLIPFORGE_FFMPEG"),
                          ("ffprobe", "CLIPFORGE_FFPROBE")):
        try:
            path, _src = _resolve_bin(name, env_var)
        except Exception as exc:  # resolution itself must not kill startup
            _add("error", name, f"binary lookup crashed ({exc}).")
            continue
        if not path:
            _add("error", name, missing_ffmpeg_guidance())
            continue
        if os.name == "posix":
            # Stat bits, not os.access(): deterministic even as root, where
            # access(X_OK) is always true. A binary that lost its exec bit
            # (bad unzip, broken fetch step) must fail loudly, not mid-job.
            try:
                executable = bool(os.stat(path).st_mode & 0o111)
            except OSError:
                executable = False
            if not executable:
                _add("error", name,
                     f"found at {path} but it is not executable — "
                     "reinstall ClipForge (or chmod +x the binary).")

    # 2. bundled resources (templates/, assets/fonts/, previews/).
    for parts, asset, level, detail in (
        (("templates",), "templates/", "error",
         "dashboard pages cannot render without templates/ — reinstall ClipForge."),
        (("assets", "fonts"), "assets/fonts/", "warning",
         "caption fonts missing: captions fall back to system fonts, "
         "so styles may look different on each machine."),
        (("previews",), "previews/", "warning",
         "style-preview videos missing: regenerate them from the dashboard."),
    ):
        try:
            if not resource_path(*parts).is_dir():
                _add(level, asset, detail)
        except Exception as exc:
            _add("warning", asset, f"could not verify ({exc}).")

    # 3. third-party data files PyInstaller doesn't collect on its own.
    for package, rel_parts, level, detail in (
        ("faster_whisper", ("assets", "silero_vad_v6.onnx"), "warning",
         "VAD model missing: transcription still runs (retries without VAD) "
         "but quality drops — reinstall ClipForge."),
        ("cv2", ("data", "haarcascade_frontalface_default.xml"), "warning",
         "face-detection data missing: smart crop silently falls back to "
         "center crop — reinstall ClipForge."),
        ("googleapiclient",
         ("discovery_cache", "documents", "youtube.v3.json"), "error",
         "YouTube discovery doc missing: every upload fails with "
         "UnknownApiNameOrVersion — reinstall ClipForge."),
    ):
        try:
            d = _package_dir(package)
            if d is None or not d.joinpath(*rel_parts).is_file():
                _add(level, f"{package}:{'/'.join(rel_parts)}", detail)
        except Exception as exc:
            _add("warning", package, f"could not verify ({exc}).")

    return findings
