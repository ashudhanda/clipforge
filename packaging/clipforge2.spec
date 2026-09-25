# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the ClipForge2 one-click installer.

Build:  pyinstaller packaging/clipforge2.spec
Output: dist/ClipForge2/  (onedir; .app bundle added on macOS)

FFmpeg/ffprobe must be in packaging/bin/ first — run
packaging/fetch-ffmpeg.sh (Linux/macOS) or fetch-ffmpeg.ps1 (Windows).
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

# --- bundled data -----------------------------------------------------------
datas = [
    (os.path.join(ROOT, "templates"), "templates"),
    (os.path.join(ROOT, "previews"), "previews"),
]

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
        "sklearn",
        "sklearn.ensemble",
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
    name="ClipForge2",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=console,
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
    name="ClipForge2",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="ClipForge2.app",
        icon=None,
        bundle_identifier="tech.aditiweb.clipforge2",
        info_plist={
            "NSHighResolutionCapable": True,
            "CFBundleShortVersionString": "0.1.0",
        },
    )
