"""Shared yt-dlp helpers for the ingest layer.

Own implementation. Key lessons baked in:
- Never hardcode impersonation targets: :func:`discover_impersonation`
  probes what the installed yt-dlp actually supports at runtime and picks
  the newest Chrome-family target, degrading to no impersonation when the
  optional ``curl_cffi`` backend is missing.
- Never disable TLS verification: we rely on the system CA bundle
  (``$SSL_CERT_FILE``) plus the egress CA appended to this venv's certifi
  bundle at setup time.
"""

from __future__ import annotations

import functools
import logging
import re
from typing import Optional

from yt_dlp import YoutubeDL

log = logging.getLogger(__name__)

_VIDEO_ID_RES = (
    re.compile(r"(?:[?&]v=|/v/|/embed/|/shorts/|/live/)([A-Za-z0-9_-]{11})"),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
    re.compile(r"\A([A-Za-z0-9_-]{11})\Z"),
)


def extract_video_id(url: str) -> Optional[str]:
    """Extract the 11-char YouTube video ID from common URL shapes."""
    if not url:
        return None
    url = url.strip()
    for rx in _VIDEO_ID_RES:
        m = rx.search(url)
        if m:
            return m.group(1)
    return None


def _parse_version(text: str) -> int:
    digits = re.sub(r"\D", "", text or "")
    try:
        return int(digits) if digits else -1
    except ValueError:
        return -1


@functools.lru_cache(maxsize=1)
def discover_impersonation() -> Optional[str]:
    """Return the best usable ``--impersonate`` target, or ``None``.

    Probes the installed yt-dlp via its own ``_get_available_impersonate_targets``
    (the same mechanism ``--list-impersonate-targets`` uses), so this never
    depends on a hardcoded client/version list. Prefers the newest Chrome
    target; falls back to the first available target; returns ``None`` when
    impersonation is unavailable (e.g. ``curl_cffi`` not installed).
    """
    try:
        with YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            available = ydl._get_available_impersonate_targets() or []
    except Exception as exc:  # private API may change; degrade gracefully
        log.debug("impersonation discovery failed: %s", exc)
        return None

    chromes = [
        t for t, _handler in available if (t.client or "").lower() == "chrome"
    ]
    if chromes:
        best = max(chromes, key=_parse_version_key)
        return str(best)
    if available:
        return str(available[0][0])
    return None


def _parse_version_key(target) -> int:
    return _parse_version(getattr(target, "version", "") or "")


def base_opts(extra: Optional[dict] = None) -> dict:
    """Common YoutubeDL options: quiet, safe retries, sane timeouts."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
        "noprogress": True,
    }
    # Point yt-dlp at the bundled ffmpeg (frozen app) or the local one
    # (dev). Without this, section downloads fail on machines with no
    # system ffmpeg because yt-dlp only searches PATH by default.
    try:
        from core.paths import ffmpeg_dir as _ffmpeg_dir

        ffdir = _ffmpeg_dir()
    except Exception:
        ffdir = None
    if ffdir:
        opts["ffmpeg_location"] = ffdir
    impersonate = discover_impersonation()
    if impersonate:
        opts["impersonate"] = impersonate
    if extra:
        opts.update(extra)
    return opts
