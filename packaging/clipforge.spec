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

# --- bundled data -----------------------------------------------------------
datas = [
    (os.path.join(ROOT, "templates"), "templates"),
]
# previews/ only exists once style-preview videos are generated locally;
# skip it when absent so the build never breaks on a fresh checkout.
_previews = os.path.join(ROOT, "previews")
if os.path.isdir(_previews):
    datas.append((_previews, "previews"))

# --- app icon (packaging/icon.ico); None until the icon is generated --------
_ICON = os.path.join(ROOT, "packaging", "icon.ico")
APP_ICON = _ICON if os.path.isfile(_ICON) else None

# --- tzdata: zoneinfo loads it via importlib.resources (no direct import),
# so PyInstaller's import analysis misses its zone files. Windows has no
# system tz database — without this the frozen app crashes at startup
# (ZoneInfoNotFoundError: America/Los_Angeles). Collected explicitly.
try:
    from PyInstaller.utils.hooks import collect_data_files

    datas += collect_data_files("tzdata")
except Exception:
    pass  # local dev without PyInstaller/tzdata: zoneinfo falls back to system tzdata

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
            "CFBundleShortVersionString": "0.1.0",
        },
    )
