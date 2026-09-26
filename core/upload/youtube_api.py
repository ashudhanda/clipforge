"""YouTube Data API upload for ClipForge.

Resumable 1 MB-chunk upload with retries (adapted from the yt-automation
audit: HttpError < 500 raises immediately — auth/validation errors are
never retried; other failures retried up to 5 times with linear backoff
5/10/15/20/25 s). Plus a quota guard: since 2026-06-01 YouTube uses
granular quota buckets — videos.insert has its OWN bucket of
100 calls/day, separate from the 10,000-unit general pool
(Dec 2025: upload cost cut from ~1,600 to ~100 units, then moved to its
own bucket). So we stop after 100 uploads/day (Pacific-time quota day)
with a plain-language message.

Never invents success — API errors surface honestly.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from core.config import config_dir
from core.paths import ffprobe_path as _ffprobe_path

from . import oauth as oauth_mod

log = logging.getLogger("clipforge.upload.youtube_api")

# Granular quota buckets (since 2026-06-01, per Google's official revision
# history): videos.insert draws from its OWN bucket — 100 calls/day —
# separate from the 10,000-unit general pool. Old 1,600-unit math is dead.
MAX_UPLOADS_PER_DAY = 100
QUOTA_TZ = ZoneInfo("America/Los_Angeles")  # YouTube quota day = Pacific
QUOTA_FILE = "quota.json"

CHUNK_SIZE = 1024 * 1024  # 1 MB resumable chunks (audit value)

# Files at or below this size upload in one multipart POST instead of a
# resumable session: fewer round trips, and no resumable status-query for
# proxies to choke on. Above it, resumable chunked upload is worth it.
MULTIPART_MAX_BYTES = 64 * 1024 * 1024
MAX_ATTEMPTS = 5
VALID_PRIVACY = ("public", "unlisted", "private")


class QuotaExceeded(Exception):
    """Raised when today's free YouTube API quota is used up."""


class UploadError(Exception):
    """An honest upload failure (validation/auth/API)."""


# ---------------------------------------------------------------------------
# quota guard
# ---------------------------------------------------------------------------

def _pacific_today() -> str:
    return datetime.now(QUOTA_TZ).date().isoformat()


def quota_path() -> Path:
    return config_dir() / QUOTA_FILE


class QuotaTracker:
    """Tracks uploads/day against the free videos.insert bucket (Pacific day)."""

    def __init__(self, path: Path | None = None):
        self.path = path or quota_path()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def used_today(self) -> int:
        d = self._read()
        if d.get("date") != _pacific_today():
            return 0
        try:
            return max(0, int(d.get("uploads", 0)))
        except (TypeError, ValueError):
            return 0

    def remaining(self) -> int:
        return max(0, MAX_UPLOADS_PER_DAY - self.used_today())

    def can_upload(self) -> bool:
        return self.remaining() > 0

    def status(self) -> dict:
        return {"used": self.used_today(), "limit": MAX_UPLOADS_PER_DAY,
                "remaining": self.remaining(),
                "resets": "midnight Pacific Time"}

    def check_or_raise(self) -> None:
        if not self.can_upload():
            raise QuotaExceeded(
                f"Today's free YouTube upload quota is used up "
                f"({MAX_UPLOADS_PER_DAY}/{MAX_UPLOADS_PER_DAY} uploads). "
                "It resets at midnight Pacific Time — or upload manually "
                "via YouTube Studio (no quota needed).")

    def record_upload(self) -> None:
        today = _pacific_today()
        d = self._read()
        n = 0
        if d.get("date") == today:
            try:
                n = int(d.get("uploads", 0))
            except (TypeError, ValueError):
                n = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"date": today, "uploads": n + 1}),
                             encoding="utf-8")


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------

def _ca_bundle() -> str | None:
    """CA bundle for TLS: prefer certifi, else httplib2's default."""
    try:
        import certifi

        return certifi.where()
    except ImportError:
        return None


def _proxied_http():
    """httplib2.Http that honors HTTPS proxy env vars with a proper CA bundle.

    httplib2 tunnels all proxy connections through PySocks (even plain
    HTTP CONNECT proxies), so PySocks must be installed for proxy env
    vars to work at all. The CA bundle defaults to certifi so proxies
    that MITM TLS (corporate egress proxies) validate correctly.
    """
    import httplib2

    ca_certs = _ca_bundle()
    proxy_url = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    kwargs = {"ca_certs": ca_certs} if ca_certs else {}
    if proxy_url:
        kwargs["proxy_info"] = httplib2.proxy_info_from_url(proxy_url, "https")
    return httplib2.Http(**kwargs)


def _build_service(creds):
    """Seam for tests (monkeypatch to avoid network)."""
    from googleapiclient.discovery import build
    from google_auth_httplib2 import AuthorizedHttp

    # build() forbids passing both credentials and http, so authorize first.
    http = AuthorizedHttp(creds, http=_proxied_http())
    return build("youtube", "v3", http=http)


def _backoff(attempt: int) -> None:
    time.sleep(attempt * 5)  # 5/10/15/20/25 s (adapted from audit)


def _is_portrait(video_path: str) -> bool | None:
    """True if the video is portrait. None when ffprobe can't tell.

    Dashboard-built Shorts are always 1080x1920; this is a safety net
    for foreign files, never a blocker.
    """
    ffprobe = _ffprobe_path()
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "json",
             video_path],
            capture_output=True, text=True, timeout=30)
        dims = json.loads(out.stdout).get("streams", [{}])[0]
        w, h = int(dims.get("width", 0)), int(dims.get("height", 0))
        if w > 0 and h > 0:
            return h > w
    except Exception:  # noqa: BLE001 — probe failure must never block
        pass
    return None


def _execute_with_retry(request):
    """Run an upload request; return the final response dict."""
    from googleapiclient.errors import HttpError

    # FakeRequest in tests has no .resumable attr -> default True keeps the
    # next_chunk() path; real non-resumable requests have resumable=None.
    resumable = getattr(request, "resumable", True)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            if resumable:
                _status, response = request.next_chunk()
            else:
                response = request.execute()
            if response is not None and isinstance(response, dict) \
                    and "id" in response:
                return response
            # next_chunk returned without a final response — keep polling.
            # (A None response with no error just means "not done yet".)
        except HttpError as e:
            code = getattr(getattr(e, "resp", None), "status", None)
            if code == 411 and hasattr(request, "_in_error_state"):
                # Some egress proxies refuse googleapiclient's resumable
                # status-query (an empty PUT with Content-Length: 0), so one
                # transient error would otherwise doom every retry to repeat
                # the blocked query. Clear the library's error flag so the
                # next attempt re-sends the current media chunk instead —
                # the server de-duplicates by byte range.
                request._in_error_state = False
                log.info("proxy blocked the resumable status query "
                         "(HTTP 411); resuming from the current chunk…")
            elif code is not None and code < 500:
                # Auth/validation errors are never retried (audit lesson).
                raise UploadError(
                    f"YouTube rejected the upload (HTTP {code}): "
                    f"{e}") from e
            if attempt >= MAX_ATTEMPTS:
                raise UploadError(
                    f"YouTube upload failed after {MAX_ATTEMPTS} tries: "
                    f"{e}") from e
            log.info("upload chunk failed (attempt %d/%d), retrying…",
                     attempt, MAX_ATTEMPTS)
            _backoff(attempt)
        except Exception as e:  # noqa: BLE001 — network blips etc.
            if attempt >= MAX_ATTEMPTS:
                raise UploadError(
                    f"YouTube upload failed after {MAX_ATTEMPTS} tries: "
                    f"{e}") from e
            log.info("upload error (attempt %d/%d), retrying: %s",
                     attempt, MAX_ATTEMPTS, e)
            _backoff(attempt)
    raise UploadError("YouTube upload did not complete.")


def upload_short(video_path: str, title: str, description: str = "",
                 hashtags: list | None = None,
                 privacy: str = "public",
                 progress_cb=None,
                 _service=None,
                 _quota: QuotaTracker | None = None) -> dict:
    """Upload one vertical clip as a YouTube Short.

    Returns {"video_id": ..., "url": ...}. Raises QuotaExceeded when the
    daily free quota is used up, UploadError on honest API failures.
    """
    path = Path(video_path)
    if not path.is_file():
        raise UploadError(f"Video file not found: {video_path}")
    if privacy not in VALID_PRIVACY:
        raise UploadError(
            f"privacy must be one of {VALID_PRIVACY}, got {privacy!r}")

    title = (title or "").strip()[:100] or "Untitled clip"
    description = (description or "").strip()[:5000]
    tags = sorted({t.strip("# ").lower() for t in (hashtags or [])
                   if t and str(t).strip()})
    tags_str = ",".join(tags)[:500]

    quota = _quota or QuotaTracker()
    quota.check_or_raise()

    portrait = _is_portrait(str(path))
    if portrait is False:
        raise UploadError(
            "This video isn't vertical — YouTube won't treat it as a "
            "Short. Rebuild it as 9:16 first.")

    creds = oauth_mod.get_credentials()
    service = _service if _service is not None else _build_service(creds)

    from googleapiclient.http import MediaFileUpload

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": "22",  # People & Blogs (generic, safe default)
        },
        "status": {"privacyStatus": privacy},
    }
    media = MediaFileUpload(
        str(path),
        # Small files go up in one multipart POST — no resumable session,
        # no status-query for proxies to block. Big files stay resumable.
        chunksize=CHUNK_SIZE,
        resumable=path.stat().st_size > MULTIPART_MAX_BYTES,
    )
    request = service.videos().insert(part="snippet,status", body=body,
                                      media_body=media)
    if progress_cb is not None:
        # Wrap next_chunk so the dashboard can show progress.
        orig_next = request.next_chunk

        def _wrapped():
            st, resp = orig_next()
            try:
                progress_cb(st)
            except Exception:  # noqa: BLE001 — callback never breaks upload
                pass
            return st, resp

        request.next_chunk = _wrapped

    response = _execute_with_retry(request)
    video_id = response.get("id", "")
    if not video_id:
        raise UploadError(
            "YouTube finished without returning a video id — "
            "check YouTube Studio; the upload may need attention.")
    quota.record_upload()
    log.info("uploaded %s -> https://youtu.be/%s", path.name, video_id)
    return {"video_id": video_id, "url": f"https://youtu.be/{video_id}"}


def quota_status() -> dict:
    """Plain status dict for the dashboard (never raises)."""
    try:
        return {"ok": True, **QuotaTracker().status()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
