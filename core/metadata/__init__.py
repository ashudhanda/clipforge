"""Metadata generation for ClipForge2: unique title/description/hashtags per clip.

- ``generate_metadata``: one clip -> {title, description, hashtags} via LLM.
- ``ensure_unique_titles``: batch dedupe of titles (LLM reword -> fallback).
- ``generate_for_clips``: end-to-end for a batch of clips.

LLM failures raise LLMError — metadata is never fabricated.
"""

from __future__ import annotations

import logging

from core.moments.llm import get_provider

from .generator import generate_metadata, transcript_to_text, validate_metadata
from .uniqueness import ensure_unique_titles

log = logging.getLogger("clipforge2.metadata")

__all__ = [
    "ensure_unique_titles",
    "generate_for_clips",
    "generate_metadata",
    "transcript_to_text",
    "validate_metadata",
]


def _clip_transcript(clip):
    """Extract the transcript from a clip dict or object."""
    if isinstance(clip, dict):
        transcript = clip.get("transcript")
    else:
        transcript = getattr(clip, "transcript", None)
    if transcript is None:
        raise ValueError(
            "clip has no transcript — pass dicts with a 'transcript' key or "
            "objects with a .transcript attribute"
        )
    return transcript


def generate_for_clips(
    clips,
    niche: str,
    style_hints: dict | None = None,
    provider=None,
) -> list[dict]:
    """Generate unique metadata for each clip in a batch.

    clips: iterable of dicts (with "transcript": list of {start,end,text} or
    plain string) or objects with a .transcript attribute. The original clip
    objects are never mutated.

    Returns [{"clip": <original>, "metadata": {...}, "usage": {...}}] in
    input order, with titles unique across the batch. Raises LLMError on
    LLM/validation failure (never fabricated); ValueError on bad input.
    """
    clips = list(clips)
    if not clips:
        raise ValueError("clips must be a non-empty list")
    niche = str(niche or "").strip()
    if not niche:
        raise ValueError("niche is required")

    provider = provider or get_provider()  # resolved once, reused for the batch

    items: list[dict] = []
    usages: list[dict] = []
    for clip in clips:
        meta, usage = generate_metadata(
            _clip_transcript(clip),
            niche,
            style_hints=style_hints,
            provider=provider,
        )
        items.append(
            {
                "metadata": meta,
                "transcript": transcript_to_text(_clip_transcript(clip)),
            }
        )
        usages.append(usage)

    unique = ensure_unique_titles(items, niche, provider=provider)
    return [
        {"clip": clip, "metadata": u["metadata"], "usage": usage}
        for clip, u, usage in zip(clips, unique, usages)
    ]
