"""YouTube subtitle fetching WITHOUT downloading any video.

Own implementation. Design notes (logic adapted from public caption tools):
- Two passes: manual subtitles first, automatic captions second. Never mixed,
  so a human-written track always wins over auto-generated text.
- Fully non-fatal: timedtext 429s, missing tracks, network hiccups all return
  ``None`` instead of raising, so the pipeline can fall back to Whisper.
"""

from __future__ import annotations

import glob
import logging
import os
import re
import tempfile
from typing import Optional

from yt_dlp import YoutubeDL

from .ytdlp_helper import base_opts, extract_video_id

log = logging.getLogger(__name__)

_TIMESTAMP_RE = re.compile(
    r"(?P<start>\d{2,}:\d{2}:\d{2}\.\d{3}|\d{2}:\d{2}\.\d{3})"
    r"\s*-->\s*"
    r"(?P<end>\d{2,}:\d{2}:\d{2}\.\d{3}|\d{2}:\d{2}\.\d{3})"
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _to_seconds(ts: str) -> float:
    parts = ts.split(":")
    if len(parts) == 2:
        minutes, rest = parts
        hours = 0
    else:
        hours, minutes, rest = parts
    seconds = float(rest)
    return int(hours) * 3600 + int(minutes) * 60 + seconds


def _clean_text(raw: str) -> str:
    text = _TAG_RE.sub("", raw)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return _WS_RE.sub(" ", text).strip()


def parse_vtt(vtt_text: str) -> list[dict]:
    """Parse WebVTT into ``[{"start", "end", "text"}]`` (seconds, sorted).

    Merges consecutive cues whose cleaned text is identical (typical of
    auto-generated rolling captions), extending the end time.
    """
    cues: list[dict] = []
    lines = vtt_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = _TIMESTAMP_RE.search(line)
        if m:
            start = _to_seconds(m.group("start"))
            end = _to_seconds(m.group("end"))
            i += 1
            text_parts = []
            while i < len(lines) and lines[i].strip():
                if lines[i].strip().isdigit() and not text_parts:
                    i += 1
                    continue  # cue number line
                text_parts.append(lines[i])
                i += 1
            text = _clean_text(" ".join(text_parts))
            if text and end > start:
                if cues and cues[-1]["text"] == text and start <= cues[-1]["end"] + 0.05:
                    cues[-1]["end"] = max(cues[-1]["end"], end)
                else:
                    cues.append({"start": start, "end": end, "text": text})
        else:
            i += 1
    cues.sort(key=lambda c: c["start"])
    return cues


def _download_subtitle_file(
    video_url: str, video_id: str, automatic: bool, languages: tuple, tmpdir: str
) -> Optional[str]:
    """Download one subtitle file via yt-dlp; return its path or ``None``."""
    opts = base_opts(
        {
            "skip_download": True,
            "writesubtitles": not automatic,
            "writeautomaticsub": automatic,
            "subtitleslangs": list(languages),
            "subtitlesformat": "vtt",
            "outtmpl": os.path.join(tmpdir, "%(id)s"),
        }
    )
    with YoutubeDL(opts) as ydl:
        ydl.extract_info(video_url, download=True)

    candidates = sorted(glob.glob(os.path.join(tmpdir, f"{video_id}*.vtt")))
    if not candidates:
        return None

    def rank(path: str) -> tuple:
        name = os.path.basename(path)
        # e.g. "<id>.en.vtt" -> lang "en"
        stem = name[: -len(".vtt")]
        lang = stem[len(video_id) :].lstrip("._-") if stem.startswith(video_id) else ""
        for idx, want in enumerate(languages):
            if lang == want or lang.startswith(want + "-") or lang.startswith(want + "_"):
                return (idx, len(lang))
        return (len(languages), len(lang))

    candidates.sort(key=rank)
    return candidates[0]


def fetch_captions(
    video_url: str, languages: tuple = ("en",), prefer_manual: bool = True
) -> Optional[list[dict]]:
    """Fetch YouTube subtitles without downloading video.

    Returns ``[{"start", "end", "text"}]`` in seconds, or ``None`` when
    subtitles are unavailable or anything goes wrong (never raises for
    network/extractor failures).
    """
    video_id = extract_video_id(video_url)
    if not video_id:
        log.warning("could not extract video id from %r", video_url)
        return None

    passes = [False, True] if prefer_manual else [True]
    try:
        with tempfile.TemporaryDirectory(prefix="cf2subs_") as tmpdir:
            for automatic in passes:
                path = _download_subtitle_file(
                    video_url, video_id, automatic, languages, tmpdir
                )
                if not path:
                    continue
                with open(path, encoding="utf-8", errors="replace") as fh:
                    cues = parse_vtt(fh.read())
                if cues:
                    kind = "auto" if automatic else "manual"
                    log.info(
                        "got %d %s caption cues for %s", len(cues), kind, video_id
                    )
                    return cues
    except Exception as exc:  # non-fatal by design (429s, blocks, timeouts)
        log.warning("caption fetch failed for %s: %s", video_id, exc)
        return None
    return None
