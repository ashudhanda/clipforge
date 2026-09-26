# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the ClipForge one-click installer.

Build:  pyinstaller packaging/clipforge.spec
Output: dist/ClipForge/  (onedir; .app bundle added on macOS)

FFmpeg/ffprobe must be in packaging/bin/ first — run
packaging/fetch-ffmpeg.sh (Linux/macOS) or fetch-ffmpeg.ps1 (Windows).
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

# --- version: single source of truth is core/version.py ----------------------
sys.path.insert(0, ROOT)
try:
    from core.version import __version__ as APP_VERSION
except Exception:
    APP_VERSION = "0.0.0-dev"

# --- bundled data -----------------------------------------------------------
datas = [
    (os.path.join(ROOT, "templates"), "templates"),
]
# Caption fonts (assets/fonts/) — skip when absent so a fresh checkout
# without the font files still builds (captions then use system fonts).
_fonts = os.path.join(ROOT, "assets", "fonts")
if os.path.isdir(_fonts):
    datas.append((_fonts, os.path.join("assets", "fonts")))
# previews/ only exists once style-preview videos are generated locally;
# skip it when absent so the build never breaks on a fresh checkout.
_previews = os.path.join(ROOT, "previews")
if os.path.isdir(_previews):
    datas.append((_previews, "previews"))

# --- app icon (packaging/icon.ico); None until the icon is generated --------
_ICON = os.path.join(ROOT, "packaging", "icon.ico")
APP_ICON = _ICON if os.path.isfile(_ICON) else None

# --- faster_whisper: the Silero VAD onnx asset (assets/silero_vad_v6.onnx)
# is a *data* file, so PyInstaller's import analysis misses it even though
# the package itself is a hiddenimport. Without it every transcription with
# vad_filter=True dies with [ONNXRuntimeError] NO_SUCHFILE on user machines
# (v0.1.6). Collected explicitly alongside tzdata below.
# --- tzdata: zoneinfo loads it via importlib.resources (no direct import),
# so PyInstaller's import analysis misses its zone files. Windows has no
# system tz database — without this the frozen app crashes at startup
# (ZoneInfoNotFoundError: America/Los_Angeles). Collected explicitly.
# --- cv2 Haar cascades (cv2/data/*.xml): hook-cv2.py does NOT collect them,
# so without this every frozen build silently loses face-aware smart crop
# (CascadeClassifier loads empty -> detect_face_centers() returns [] and the
# 9:16 crop degrades to center crop with no error anywhere).
# --- googleapiclient static discovery doc: build("youtube", "v3") uses
# static_discovery=True by default and RAISES UnknownApiNameOrVersion (no
# network fallback) when youtube.v3.json is missing — YouTube upload would be
# 100% broken in the frozen app. Collect ONLY youtube.v3.json: the full
# documents/ dir is ~100MB of API docs we never call.
try:
    from PyInstaller.utils.hooks import collect_data_files
except ImportError:
    collect_data_files = None  # spec linted/parsed without PyInstaller: skip

if collect_data_files is not None:
    # NOTE: collection failures must FAIL THE BUILD LOUDLY. The v0.1.6
    # incident was a silently-skipped data file — never swallow these again.
    datas += collect_data_files("faster_whisper")
    datas += collect_data_files("tzdata")
    datas += collect_data_files("cv2", subdir="data")
    datas += collect_data_files(
        "googleapiclient",
        subdir=os.path.join("discovery_cache", "documents"),
        includes=["youtube.v3.json"],
    )

# --- bundled binaries (ffmpeg/ffprobe land at the bundle root) --------------
binaries = []
bin_dir = os.path.join(ROOT, "packaging", "bin")
if os.path.isdir(bin_dir):
    for f in sorted(os.listdir(bin_dir)):
        if f.startswith(("ffmpeg", "ffprobe")):
            binaries.append((os.path.join(bin_dir, f), "."))

a = Analysis(
    [os.path.join(ROOT, "app.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        # Flask/Jinja bits PyInstaller's analysis commonly misses
        "jinja2",
        "jinja2.ext",
        "werkzeug.serving",
        "engineio.async_drivers.threading",
        # heavy optional deps with dynamic imports
        "faster_whisper",
        "cv2",
        "yt_dlp",
        "googleapiclient",
        "googleapiclient.discovery",
        "google_auth_oauthlib",
        "google_auth_oauthlib.flow",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

# No console window on Windows/macOS (non-tech users shouldn't see a terminal).
# Crashes still get logged to a file — see app.py's frozen file-logging.
console = sys.platform not in ("win32", "darwin")

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="ClipForge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=console,
    icon=APP_ICON,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ClipForge",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="ClipForge.app",
        icon=None,
        bundle_identifier="tech.aditiweb.clipforge",
        info_plist={
            "NSHighResolutionCapable": True,
            "CFBundleShortVersionString": APP_VERSION,
        },
    )
