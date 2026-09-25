"""ClipForge2 ingest layer — captions-first, range-only media acquisition.

Public entry point: :func:`ingest`.
"""

from .captions import fetch_captions
from .downloader import download_ranges, merge_segments
from .transcribe import download_audio, transcribe_audio
from .ytdlp_helper import base_opts, discover_impersonation, extract_video_id

__all__ = [
    "ingest",
    "fetch_captions",
    "transcribe_audio",
    "download_audio",
    "download_ranges",
    "merge_segments",
    "extract_video_id",
    "discover_impersonation",
    "base_opts",
]


def ingest(video_url, segments=None, cache_dir=None, languages=("en",), whisper_model="small"):
    """End-to-end ingest for one video.

    Always tries YouTube captions first (no video download). Falls back to
    audio-only download + local faster-whisper transcription when captions
    are unavailable. Downloads media ONLY for the given ``segments``
    (``(start, end)`` second tuples); with ``segments=None`` no media is
    downloaded at all.

    Returns ``{"transcript": [...], "media_paths": [...], "source": ...,
    "video_id": ...}`` where each transcript item is
    ``{"start": float, "end": float, "text": str}``.
    """
    from .ytdlp_helper import extract_video_id as _vid

    video_id = _vid(video_url) or "unknown"

    transcript = fetch_captions(video_url, languages=languages)
    source = "captions"
    if not transcript:
        transcript = transcribe_audio(
            video_url, model=whisper_model, cache_dir=cache_dir
        )
        source = "whisper"

    media_paths = (
        download_ranges(video_url, segments, cache_dir=cache_dir) if segments else []
    )

    return {
        "transcript": transcript,
        "media_paths": media_paths,
        "source": source,
        "video_id": video_id,
    }
