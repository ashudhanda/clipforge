"""Range-only video download: fetch ONLY selected timestamp ranges.

Own implementation. The full source video is never downloaded — each merged
``(start, end)`` segment is fetched independently via yt-dlp's
``download_ranges`` (single section per call, so files never collide) and
cached under a deterministic filename. Repeat runs are free.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from typing import Optional

from yt_dlp import YoutubeDL

from core.paths import ffprobe_path as _ffprobe_path

from .ytdlp_helper import base_opts, extract_video_id

log = logging.getLogger(__name__)

DEFAULT_CACHE_SUBDIR = "ranges"
DEFAULT_FORMAT = "bv*[height<=1080]+ba/b[height<=1080]/b"
# Minimum expected range length (s) for the post-download duration check.
# Shorter ranges are skipped: keyframe-quantized stream-copy cuts can
# legitimately deviate on very short spans.
_MIN_CHECK_SECONDS = 4.0


def _cache_dir(cache_dir: Optional[str]) -> str:
    base = cache_dir or os.path.join(os.path.expanduser("~"), ".cache", "clipforge")
    path = os.path.join(base, DEFAULT_CACHE_SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def merge_segments(
    segments: list[tuple[float, float]], gap: float = 1.0, pad: float = 0.5
) -> list[tuple[float, float]]:
    """Sort, merge overlapping/near-contiguous ranges, pad, clamp at 0.

    ``gap``: ranges this close (or overlapping) merge into one.
    ``pad``: extra seconds added each side so cuts never clip speech.
    """
    clean = [(float(s), float(e)) for s, e in segments if float(e) > float(s)]
    if not clean:
        return []
    clean.sort()
    merged: list[list[float]] = [[clean[0][0], clean[0][1]]]
    for s, e in clean[1:]:
        if s <= merged[-1][1] + gap:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [
        (max(0.0, round(s - pad, 3)), round(e + pad, 3)) for s, e in merged
    ]


def _range_path(cache: str, video_id: str, start: float, end: float) -> str:
    digest = hashlib.sha1(f"{video_id}|{start:.3f}|{end:.3f}".encode()).hexdigest()[:12]
    return os.path.join(cache, f"clip_{video_id}_{digest}.mp4")


def _probe_duration(path: str) -> Optional[float]:
    """Return media duration in seconds via ffprobe, or None if unavailable."""
    ffprobe = _ffprobe_path()
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30, check=True,
        )
        return float(out.stdout.strip())
    except Exception:
        return None


def _download_single_range(
    video_url: str, start: float, end: float, out_path: str, format: str
) -> str:
    """Fetch one ``(start, end)`` section; return the file path."""
    section = [{"start_time": start, "end_time": end, "title": "clip"}]
    opts = base_opts(
        {
            "format": format,
            "merge_output_format": "mp4",
            "outtmpl": out_path.replace(".mp4", ".%(ext)s"),
            "download_ranges": lambda _info, _ydl: section,
        }
    )
    with YoutubeDL(opts) as ydl:
        ydl.extract_info(video_url, download=True)
    if not (os.path.exists(out_path) and os.path.getsize(out_path) > 0):
        # yt-dlp may keep the merged ext; accept whatever it produced
        import glob as _glob

        alts = [p for p in _glob.glob(out_path.replace(".mp4", ".*"))
                if os.path.getsize(p) > 0]
        if alts:
            out_path = alts[0]
        else:
            raise RuntimeError(f"range download produced no file for {start}-{end}")

    # Guard against silently truncated downloads (e.g. flaky range
    # responses): a healthy stream-copy cut is never much shorter than asked.
    expected = end - start
    if expected >= _MIN_CHECK_SECONDS:
        duration = _probe_duration(out_path)
        if duration is not None and duration < 0.5 * expected:
            raise RuntimeError(
                f"range download truncated for {start}-{end}: "
                f"got {duration:.1f}s, expected ~{expected:.1f}s"
            )
    return out_path


def download_ranges(
    video_url: str,
    segments: list[tuple[float, float]],
    cache_dir: Optional[str] = None,
    format: str = DEFAULT_FORMAT,
    gap: float = 1.0,
    pad: float = 0.5,
) -> list[str]:
    """Download only the given ranges; return one cached mp4 path per range."""
    video_id = extract_video_id(video_url)
    if not video_id:
        raise ValueError(f"could not extract video id from {video_url!r}")
    merged = merge_segments(segments, gap=gap, pad=pad)
    if not merged:
        return []

    cache = _cache_dir(cache_dir)
    paths: list[str] = []
    for start, end in merged:
        out_path = _range_path(cache, video_id, start, end)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            log.info("range cache hit %s (%.1f-%.1f)", video_id, start, end)
        else:
            log.info("downloading range %.1f-%.1f of %s", start, end, video_id)
            out_path = _download_single_range(video_url, start, end, out_path, format)
        paths.append(out_path)
    return paths
