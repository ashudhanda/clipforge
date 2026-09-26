"""YouTube Data API upload for ClipForge.

Resumable 1 MB-chunk upload with retries (adapted from the yt-automation
audit: HttpError < 500 raises immediately — auth/validation errors are
never retried; other failures retried up to 5 *consecutive* times with
linear backoff 5/10/15/20/25 s, and a stall watchdog so a wedged upload
can never loop forever). Plus a quota guard: since 2026-06-01 YouTube uses
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
# YouTube Data API quota costs (published): videos.insert = 1,600 units,
# daily project quota = 10,000 units.
UNITS_PER_UPLOAD = 1600
DAILY_UNIT_LIMIT = 10000
QUOTA_TZ = ZoneInfo("America/Los_Angeles")  # YouTube quota day = Pacific
QUOTA_FILE = "quota.json"

CHUNK_SIZE = 1024 * 1024  # 1 MB resumable chunks (audit value)

# Files at or below this size upload in one multipart POST instead of a
# resumable session: fewer round trips, and no resumable status-query for
# proxies to choke on. Above it, resumable chunked upload is worth it.
MULTIPART_MAX_BYTES = 64 * 1024 * 1024
# Transient failures tolerated *in a row* before giving up (audit value).
MAX_ATTEMPTS = 5
# Resumable chunk calls that report no forward progress before the upload
# is declared stalled instead of looping forever (a hang is worse than an
# honest error).
STALL_LIMIT = 20
VALID_PRIVACY = ("public", "unlisted", "private")

# 403 reasons that mean "quota exhausted" (as opposed to other 403s like
# accessDenied). These degrade to the browser bundle, not an error.
_QUOTA_REASONS = frozenset({"quotaExceeded", "dailyLimitExceeded"})


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
        used = self.used_today()
        return {"used": used, "limit": MAX_UPLOADS_PER_DAY,
                "remaining": self.remaining(),
                "resets": "midnight Pacific Time",
                # YouTube Data API quota is 10,000 units/day and a
                # videos.insert costs 1,600 units. YouTube offers no
                # quota-usage endpoint, so this is an honest estimate
                # from the uploads this app performed today.
                "units_per_upload": UNITS_PER_UPLOAD,
                "units_used": used * UNITS_PER_UPLOAD,
                "units_limit": DAILY_UNIT_LIMIT}

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


def _stream_rotation(stream: dict) -> int:
    """Rotation in degrees from tags or display-matrix side data (0 if none).

    Phone videos are often stored landscape (e.g. 1920x1080) with a 90°
    rotation flag — ffprobe's width/height are the *coded* dims, so without
    this a genuinely vertical clip looks landscape.
    """
    try:
        tags = stream.get("tags") or {}
        rot = int(float(str(tags.get("rotate") or 0)))
        if rot:
            return rot % 360
    except (TypeError, ValueError):
        pass
    try:
        for side_data in stream.get("side_data_list") or []:
            if isinstance(side_data, dict) and "rotation" in side_data:
                return int(float(side_data["rotation"])) % 360
    except (TypeError, ValueError):
        pass
    return 0


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
             "-show_entries", "stream=width,height,tags,side_data_list",
             "-of", "json",
             video_path],
            capture_output=True, text=True, timeout=30)
        stream = json.loads(out.stdout).get("streams", [{}])[0]
        w, h = int(stream.get("width", 0)), int(stream.get("height", 0))
        if _stream_rotation(stream) in (90, 270):
            w, h = h, w  # displayed dims, not coded dims
        if w > 0 and h > 0:
            return h > w
    except Exception:  # noqa: BLE001 — probe failure must never block
        pass
    return None


def _is_quota_error(http_error) -> bool:
    """True when a 403 is really "quota exhausted", not another 403."""
    content = getattr(http_error, "content", None)
    if not content:
        return False
    try:
        payload = json.loads(content)
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    errors = (payload.get("error") or {}).get("errors", [])
    if not isinstance(errors, list):
        return False
    return any(isinstance(item, dict)
               and item.get("reason") in _QUOTA_REASONS
               for item in errors)


def _classify_http_error(e) -> str:
    """Map an HttpError to an action: "quota" | "fatal" | "retry".

    - "quota": the project's upload quota is exhausted — the caller
      degrades to the browser bundle instead of failing.
    - "fatal": auth/validation errors (< 500) are never retried, so a
      bad request can't burn quota or loop (audit lesson).
    - "retry": 5xx / transport-level failures may succeed on retry.
    """
    code = getattr(getattr(e, "resp", None), "status", None)
    if code == 403 and _is_quota_error(e):
        return "quota"
    if code is not None and code < 500:
        return "fatal"
    return "retry"


def _quota_exhausted_error() -> QuotaExceeded:
    return QuotaExceeded(
        "YouTube reports today's upload quota is used up (the API "
        "returned a quota error). It resets at midnight Pacific Time — "
        "or upload manually via YouTube Studio (no quota needed).")


def _response_complete(response) -> bool:
    return (response is not None and isinstance(response, dict)
            and "id" in response)


def _execute_with_retry(request):
    """Run an upload request; return the final response dict.

    Resumable uploads loop chunk-by-chunk until the server returns the
    video id — only *consecutive transient failures* count against
    MAX_ATTEMPTS, so a 60-chunk upload is never killed after 5 chunks.
    Single-shot (multipart) uploads retry execute() on transient
    failures. A response that arrives without a video id is an honest
    failure, never silently retried (retrying a finished multipart
    upload could publish a duplicate video).
    """
    # FakeRequest in tests has no .resumable attr -> default True keeps the
    # next_chunk() path; real non-resumable requests have resumable=None.
    if getattr(request, "resumable", True):
        return _execute_resumable_with_retry(request)
    return _execute_single_shot_with_retry(request)


def _execute_resumable_with_retry(request):
    from googleapiclient.errors import HttpError

    failures = 0
    no_progress = 0
    last_progress = None
    while True:
        try:
            _status, response = request.next_chunk()
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
                action = "retry"
            else:
                action = _classify_http_error(e)
            if action == "quota":
                raise _quota_exhausted_error() from e
            if action == "fatal":
                raise UploadError(
                    f"YouTube rejected the upload (HTTP {code}): "
                    f"{e}") from e
            failures += 1
            if failures >= MAX_ATTEMPTS:
                raise UploadError(
                    f"YouTube upload failed after {MAX_ATTEMPTS} tries: "
                    f"{e}") from e
            log.info("upload chunk failed (attempt %d/%d), retrying…",
                     failures, MAX_ATTEMPTS)
            _backoff(failures)
            continue
        except Exception as e:  # noqa: BLE001 — network blips etc.
            failures += 1
            if failures >= MAX_ATTEMPTS:
                raise UploadError(
                    f"YouTube upload failed after {MAX_ATTEMPTS} tries: "
                    f"{e}") from e
            log.info("upload error (attempt %d/%d), retrying: %s",
                     failures, MAX_ATTEMPTS, e)
            _backoff(failures)
            continue
        # A clean chunk resets the failure count: intermittent blips
        # don't accumulate against the limit.
        failures = 0
        if _response_complete(response):
            return response
        # Watchdog: next_chunk() must make forward progress. A request
        # whose byte position never advances would otherwise loop forever.
        progress = getattr(request, "resumable_progress", None)
        if progress is None or progress != last_progress:
            last_progress, no_progress = progress, 0
        else:
            no_progress += 1
            if no_progress >= STALL_LIMIT:
                raise UploadError(
                    "YouTube upload stalled: the server stopped accepting "
                    "chunks. Check YouTube Studio; the upload may need "
                    "attention.")


def _execute_single_shot_with_retry(request):
    """Multipart (non-resumable) upload with bounded retries."""
    from googleapiclient.errors import HttpError

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = request.execute()
        except HttpError as e:
            action = _classify_http_error(e)
            if action == "quota":
                raise _quota_exhausted_error() from e
            if action == "fatal":
                code = getattr(getattr(e, "resp", None), "status", None)
                raise UploadError(
                    f"YouTube rejected the upload (HTTP {code}): "
                    f"{e}") from e
            if attempt >= MAX_ATTEMPTS:
                raise UploadError(
                    f"YouTube upload failed after {MAX_ATTEMPTS} tries: "
                    f"{e}") from e
            log.info("upload failed (attempt %d/%d), retrying…",
                     attempt, MAX_ATTEMPTS)
            _backoff(attempt)
            continue
        except Exception as e:  # noqa: BLE001 — network blips etc.
            if attempt >= MAX_ATTEMPTS:
                raise UploadError(
                    f"YouTube upload failed after {MAX_ATTEMPTS} tries: "
                    f"{e}") from e
            log.info("upload error (attempt %d/%d), retrying: %s",
                     attempt, MAX_ATTEMPTS, e)
            _backoff(attempt)
            continue
        if _response_complete(response):
            return response
        # execute() returned without a video id: do NOT retry — for a
        # multipart upload a retry could publish a duplicate video.
        raise UploadError(
            "YouTube finished without returning a video id — "
            "check YouTube Studio; the upload may need attention.")
    raise UploadError("YouTube upload did not complete.")


def upload_short(video_path: str, title: str, description: str = "",
                 hashtags: list | None = None,
                 privacy: str = "public",
                 progress_cb=None,
                 _service=None,
                 _quota: QuotaTracker | None = None,
                 interactive: bool = True) -> dict:
    """Upload one vertical clip as a YouTube Short.

    Returns {"video_id": ..., "url": ...}. Raises QuotaExceeded when the
    daily free quota is used up, UploadError on honest API failures.

    ``interactive``: pass False from unattended contexts (crons). When the
    stored token can't be refreshed, a non-interactive call raises
    OAuthNotConfigured instead of opening a browser consent flow that
    would block for minutes on a headless machine.
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
    # YouTube caps the total tag text (~500 chars); trim the excess so a
    # hashtag-heavy clip isn't rejected with a 400.
    while len(",".join(tags)) > 500 and tags:
        tags.pop()

    quota = _quota or QuotaTracker()
    quota.check_or_raise()

    portrait = _is_portrait(str(path))
    if portrait is False:
        raise UploadError(
            "This video isn't vertical — YouTube won't treat it as a "
            "Short. Rebuild it as 9:16 first.")

    # Zero-arg call on the default path: get_credentials(interactive=True)
    # is the default, and this keeps zero-arg test doubles working.
    if interactive:
        creds = oauth_mod.get_credentials()
    else:
        creds = oauth_mod.get_credentials(interactive=False)
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
    # Bookkeeping must never turn a successful upload into a failure.
    try:
        quota.record_upload()
    except OSError as e:
        log.warning("upload succeeded but quota file couldn't be updated: %s",
                    e)
    log.info("uploaded %s -> https://youtu.be/%s", path.name, video_id)
    return {"video_id": video_id, "url": f"https://youtu.be/{video_id}"}


def quota_status() -> dict:
    """Plain status dict for the dashboard (never raises)."""
    try:
        return {"ok": True, **QuotaTracker().status()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
