"""Whisper fallback: audio-only download + local faster-whisper transcription.

Own implementation. Used only when :mod:`captions` finds no usable YouTube
subtitles. Audio is cached by video ID so repeat runs never re-download.
"""

from __future__ import annotations

import glob
import logging
import os
from typing import Optional

from yt_dlp import YoutubeDL

from .ytdlp_helper import base_opts, extract_video_id

log = logging.getLogger(__name__)

DEFAULT_CACHE_SUBDIR = "audio"


def _cache_dir(cache_dir: Optional[str]) -> str:
    base = cache_dir or os.path.join(os.path.expanduser("~"), ".cache", "clipforge")
    path = os.path.join(base, DEFAULT_CACHE_SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def _find_cached_audio(cache: str, video_id: str) -> Optional[str]:
    matches = sorted(glob.glob(os.path.join(cache, f"audio_{video_id}.*")))
    for path in matches:
        if os.path.getsize(path) > 0:
            return path
    return None


def download_audio(video_url: str, cache_dir: Optional[str] = None) -> str:
    """Download best-audio only; return the file path (cached by video ID)."""
    video_id = extract_video_id(video_url)
    if not video_id:
        raise ValueError(f"could not extract video id from {video_url!r}")
    cache = _cache_dir(cache_dir)
    hit = _find_cached_audio(cache, video_id)
    if hit:
        log.info("audio cache hit for %s", video_id)
        return hit

    opts = base_opts(
        {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(cache, f"audio_{video_id}.%(ext)s"),
        }
    )
    with YoutubeDL(opts) as ydl:
        ydl.extract_info(video_url, download=True)

    hit = _find_cached_audio(cache, video_id)
    if not hit:
        raise RuntimeError(f"audio download produced no file for {video_id}")
    return hit


def _looks_like_vad_failure(exc: Exception) -> bool:
    """True when the exception is the Silero VAD / onnxruntime model failing
    to load (e.g. the ``silero_vad_v6.onnx`` asset missing from a packaged
    build). VAD only improves word timestamps; it must never kill a job."""
    msg = f"{type(exc).__name__}: {exc}".lower()
    return (
        "onnx" in msg
        or "no_suchfile" in msg
        or "silero" in msg
        or "vad" in msg
    )


def _whisper_model_cls():
    """Indirection for the faster-whisper class (lazy import, test-seam)."""
    from faster_whisper import WhisperModel

    return WhisperModel


def _pick_device(requested: str) -> str:
    if requested != "auto":
        return requested
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def transcribe_audio(
    video_url: str,
    model: str = "small",
    device: str = "auto",
    language: str = "en",
    cache_dir: Optional[str] = None,
    vad_filter: bool = True,
) -> list[dict]:
    """Transcribe via local faster-whisper; return word-level timestamps.

    ``model`` is a faster-whisper model name (default ``"small"``) and is a
    plain parameter — pick per machine (tiny/base/small/medium/large-v3).
    Returns ``[{"start", "end", "text"}]`` per word, times in seconds.
    """
    audio_path = download_audio(video_url, cache_dir=cache_dir)
    dev = _pick_device(device)
    compute_type = "float16" if dev == "cuda" else "int8"
    log.info("transcribing %s with faster-whisper %s (%s)", audio_path, model, dev)

    wm = _whisper_model_cls()(model, device=dev, compute_type=compute_type)
    tx_kwargs = dict(language=language, word_timestamps=True, vad_filter=vad_filter)
    try:
        segments, _info = wm.transcribe(audio_path, **tx_kwargs)
    except Exception as exc:
        # The Silero VAD onnx asset can be missing from a packaged build
        # (PyInstaller doesn't bundle faster_whisper's data files unless told
        # to). VAD only refines timestamps — retry without it instead of
        # failing the whole job.
        if vad_filter and _looks_like_vad_failure(exc):
            log.warning(
                "VAD model unavailable (%s); retrying transcription without VAD",
                exc,
            )
            segments, _info = wm.transcribe(
                audio_path, language=language, word_timestamps=True, vad_filter=False
            )
        else:
            raise
    words: list[dict] = []
    for seg in segments or []:
        for w in seg.words or []:
            text = (w.word or "").strip()
            if text and w.end > w.start:
                words.append(
                    {
                        "start": round(float(w.start), 3),
                        "end": round(float(w.end), 3),
                        "text": text,
                    }
                )
    words.sort(key=lambda w: w["start"])
    log.info("transcribed %d words from %s", len(words), audio_path)
    return words
