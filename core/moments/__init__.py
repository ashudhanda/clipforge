"""Moment detection for ClipForge2: transcript -> scored clip candidates.

Two modes:
- ``mode="llm"`` (default): rubric-guided LLM scoring (needs GEMINI_API_KEY
  or OPENAI_API_KEY). Scores are 0-100 virality estimates.
- ``mode="offline"``: TextTiling-lite topic segmentation, zero API cost.
  Scores are topic-shift strength 0-100, NOT virality — see segmenter docs.

Both modes snap boundaries to sentences and dedupe overlaps.
"""

from __future__ import annotations

import logging

from .dedupe import dedupe_clips
from .llm import LLMError, get_provider
from .scorer import Clip, find_moments
from .segmenter import segment_offline

log = logging.getLogger("clipforge2.moments")

__all__ = [
    "Clip",
    "LLMError",
    "dedupe_clips",
    "detect_moments",
    "detect_moments_with_usage",
    "find_moments",
    "get_provider",
    "segment_offline",
]


def detect_moments_with_usage(
    transcript: list[dict],
    mode: str = "llm",
    provider=None,
    clips_per_minute: float = 1.0,
    min_clip_sec: float = 8.0,
    max_clip_sec: float = 100.0,
    max_overlap: float = 0.40,
) -> tuple[list[Clip], dict]:
    """Same as detect_moments, but also returns the LLM usage dict
    (est_prompt_tokens etc.) for cost tracking. Offline mode returns a
    zero-cost usage dict. Existing detect_moments() is unchanged."""
    if not isinstance(transcript, list) or not transcript:
        raise ValueError("transcript must be a non-empty list of {start,end,text}")
    if mode == "llm":
        clips, usage = find_moments(
            transcript,
            provider=provider,
            clips_per_minute=clips_per_minute,
            min_clip_sec=min_clip_sec,
            max_clip_sec=max_clip_sec,
        )
    elif mode == "offline":
        clips = segment_offline(
            transcript,
            clips_per_minute=clips_per_minute,
            min_clip_sec=min_clip_sec,
        )
        usage = {"provider": "offline", "model": "none",
                 "est_prompt_tokens": 0}
    else:
        raise ValueError(f"unknown mode {mode!r}; use 'llm' or 'offline'")
    return dedupe_clips(clips, max_overlap=max_overlap), usage


def detect_moments(
    transcript: list[dict],
    mode: str = "llm",
    provider=None,
    clips_per_minute: float = 1.0,
    min_clip_sec: float = 8.0,
    max_clip_sec: float = 100.0,
    max_overlap: float = 0.40,
) -> list[Clip]:
    """Detect clip moments. Returns clips sorted by score desc, deduped.

    Raises LLMError in llm mode when the LLM fails (never fakes results);
    ValueError on bad input/mode.
    """
    if not isinstance(transcript, list) or not transcript:
        raise ValueError("transcript must be a non-empty list of {start,end,text}")
    if mode == "llm":
        clips, _usage = find_moments(
            transcript,
            provider=provider,
            clips_per_minute=clips_per_minute,
            min_clip_sec=min_clip_sec,
            max_clip_sec=max_clip_sec,
        )
    elif mode == "offline":
        clips = segment_offline(
            transcript,
            clips_per_minute=clips_per_minute,
            min_clip_sec=min_clip_sec,
        )
    else:
        raise ValueError(f"unknown mode {mode!r}; use 'llm' or 'offline'")
    return dedupe_clips(clips, max_overlap=max_overlap)
