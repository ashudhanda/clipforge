"""ClipForge upload layer.

``upload_clip(clip, mode)`` routes one built clip to its destination:

- ``mode="api"``     → YouTube Data API resumable upload. On quota
                       exhaustion it falls back to the browser bundle
                       (never fails silently, never fakes success).
- ``mode="browser"`` → no-API-quota fallback: returns everything for a
                       <1-minute manual YouTube Studio upload.
- ``mode="manual"``  → user downloads the file; returns file info only.
"""

from __future__ import annotations

import logging

from . import browser_upload, oauth
from .youtube_api import (MAX_UPLOADS_PER_DAY, QuotaExceeded, UploadError,
                          quota_status, upload_short)

log = logging.getLogger("clipforge.upload")

UPLOAD_MODES = ("api", "browser", "manual")


def upload_clip(clip: dict, mode: str = "api") -> dict:
    """Route one clip to its upload destination.

    Returns a dict with "ok": True and method-specific payload, e.g.
    {"ok": True, "method": "api", "video_id": ..., "url": ...}.
    Raises UploadError on honest failures (bad file, API rejection…).
    Quota exhaustion on mode="api" degrades gracefully to the browser
    bundle instead of raising.
    """
    if mode not in UPLOAD_MODES:
        raise UploadError(
            f"unknown upload mode {mode!r}; pick from {UPLOAD_MODES}")

    if mode == "manual":
        file_path = clip.get("file") or ""
        return {"ok": True, "method": "manual", "file": file_path,
                "note": "Download the clip and upload it yourself — "
                        "no YouTube sign-in needed."}

    if mode == "browser":
        manual = browser_upload.prepare_manual_upload(clip)
        return {"ok": True, "method": "browser", "manual": manual,
                "note": "YouTube Studio manual upload — no API quota used."}

    # mode == "api"
    if not oauth.is_connected():
        manual = browser_upload.prepare_manual_upload(clip)
        return {"ok": True, "method": "browser", "manual": manual,
                "note": "YouTube isn't connected — connect it in the "
                        "dashboard, or upload manually via Studio."}
    try:
        from core import config as _cfg

        privacy = _cfg.load_config().get("upload_privacy", "unlisted")
    except Exception:
        privacy = "unlisted"
    if privacy not in ("public", "unlisted", "private"):
        log.warning("ignoring invalid upload_privacy %r from config", privacy)
        privacy = "unlisted"
    try:
        result = upload_short(
            _clip_file(clip),
            title=clip.get("title") or "Untitled clip",
            description=clip.get("description") or "",
            hashtags=clip.get("hashtags") or [],
            privacy=privacy,
        )
        return {"ok": True, "method": "api", **result}
    except QuotaExceeded as e:
        log.info("API quota exhausted, falling back to browser bundle")
        manual = browser_upload.prepare_manual_upload(clip)
        return {"ok": True, "method": "browser", "manual": manual,
                "note": str(e)}


def _clip_file(clip: dict) -> str:
    from core.config import config_dir

    file_path = str(clip.get("file") or "").strip()
    if file_path:
        return file_path
    v = str(clip.get("video") or "").strip()
    if v.startswith("/clips/"):
        stem = v[len("/clips/"):].replace("/", "_")
        if stem.endswith(".mp4"):
            return str(config_dir() / "clips" / stem)
    raise UploadError("Can't find this clip's video file on disk.")


__all__ = [
    "UPLOAD_MODES",
    "MAX_UPLOADS_PER_DAY",
    "QuotaExceeded",
    "UploadError",
    "browser_upload",
    "oauth",
    "quota_status",
    "upload_clip",
    "upload_short",
]
