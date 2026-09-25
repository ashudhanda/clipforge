"""Batch title uniqueness for ClipForge2 metadata.

Own implementation. When two clips in one batch get the same title:
1. Ask the LLM to reword (max 2 retries).
2. Fall back to a deterministic, suffix-free rewrite built from a real
   keyword in that clip's own transcript (never "Title (2)").
3. If even that is impossible, raise LLMError — we never ship duplicates.
"""

from __future__ import annotations

import logging
import re

from core.moments.llm import LLMError, get_provider

from .generator import MAX_TITLE_CHARS

log = logging.getLogger("clipforge2.metadata.uniqueness")

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9']{3,}")

# Small stopword set for the deterministic fallback keyword picker.
_STOPWORDS = frozenset(
    {
        "the", "this", "that", "these", "those", "with", "from", "have",
        "has", "had", "will", "would", "should", "could", "there", "their",
        "they", "them", "then", "than", "when", "what", "which", "who",
        "whom", "your", "yours", "about", "into", "over", "under", "just",
        "like", "more", "most", "some", "such", "only", "also", "very",
        "been", "were", "was", "are", "and", "for", "not", "but", "you",
        "all", "can", "her", "his", "she", "him", "its", "our", "out",
        "because", "people", "thing", "things", "really", "much", "many",
        "know", "think", "want", "need", "going", "make", "made", "take",
        "well", "even", "back", "still", "does", "did", "doing", "done",
    }
)

# Deterministic fallback templates. {kw} is always a real word from the
# clip's own transcript, so the title stays honest. No numeric suffixes.
_FALLBACK_TEMPLATES = (
    "{kw} Explained",
    "The {kw} Breakdown",
    "{kw}: What Matters",
    "Why {kw} Matters",
    "The Truth About {kw}",
    "{kw} in Focus",
    "Understanding {kw}",
)


def _reword_via_llm(
    title: str,
    transcript: str,
    used: set[str],
    provider,
    max_retries: int,
) -> str | None:
    """Ask the LLM for a unique rewording. None if it can't comply."""
    excerpt = transcript[:600]
    system = (
        "You reword YouTube Shorts titles. Reply with JSON only. "
        "Never invent claims not supported by the transcript excerpt."
    )
    user = (
        f"Reword this title so it is unique, punchy, max {MAX_TITLE_CHARS} "
        "characters, no hashtags, and honest to the clip transcript excerpt "
        "below. Do NOT reuse any of these existing titles: "
        f"{sorted(used)}.\n\n"
        f"Original title: {title}\n\n"
        f"Transcript excerpt:\n{excerpt}\n\n"
        'Reply as a JSON object: {"title": "..."}'
    )
    for _ in range(max_retries):
        try:
            parsed, _usage = provider.generate_json(system, user)
        except LLMError as e:
            log.warning("uniqueness LLM reword failed: %s", e)
            return None
        cand = ""
        if isinstance(parsed, dict):
            cand = re.sub(r"\s+", " ", str(parsed.get("title") or "")).strip()
        if (
            cand
            and len(cand) <= MAX_TITLE_CHARS
            and "#" not in cand
            and cand.lower() not in used
        ):
            return cand
        log.warning("uniqueness LLM reword rejected (dup/long/hashtag): %r", cand)
    return None


def _fallback_keywords(transcript: str, limit: int = 12) -> list[str]:
    """Distinctive words from the transcript, first occurrence casing kept."""
    seen: set[str] = set()
    keywords: list[str] = []
    for m in _WORD.finditer(transcript):
        word = m.group(0)
        lowered = word.lower()
        if lowered in _STOPWORDS or lowered in seen:
            continue
        seen.add(lowered)
        keywords.append(word)
        if len(keywords) >= limit:
            break
    return keywords


def _fallback_title(transcript: str, used: set[str]) -> str:
    """Deterministic unique title from the clip's own transcript words.

    Tries template x keyword combinations in fixed order — same input always
    yields the same output. Raises LLMError if no combination is available.
    """
    for kw in _fallback_keywords(transcript):
        for tmpl in _FALLBACK_TEMPLATES:
            cand = tmpl.format(kw=kw)
            if len(cand) <= MAX_TITLE_CHARS and cand.lower() not in used:
                log.info("uniqueness deterministic fallback: %r", cand)
                return cand
    raise LLMError(
        "could not craft a unique title from transcript keywords — "
        "refusing to duplicate"
    )


def ensure_unique_titles(
    items: list[dict],
    niche: str,
    provider=None,
    max_retries: int = 2,
) -> list[dict]:
    """Ensure unique titles across a batch of clips.

    items: list of {"metadata": {"title","description","hashtags"},
    "transcript": <plain text>}. Returns a NEW list (inputs untouched) with
    unique titles (case-insensitive). Raises LLMError only when uniqueness is
    truly impossible; ValueError on bad input.
    """
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    provider = provider or get_provider()

    used: set[str] = set()
    out: list[dict] = []
    for item in items:
        meta = dict(item.get("metadata") or {})
        transcript = str(item.get("transcript") or "")
        title = str(meta.get("title") or "").strip()
        if not title:
            raise LLMError("metadata item has an empty title")
        if title.lower() not in used:
            used.add(title.lower())
            out.append({"metadata": meta, "transcript": transcript})
            continue
        log.info("duplicate title %r — rewording", title)
        new_title = _reword_via_llm(title, transcript, used, provider, max_retries)
        if new_title is None:
            new_title = _fallback_title(transcript, used)
        meta = {**meta, "title": new_title}
        used.add(new_title.lower())
        out.append({"metadata": meta, "transcript": transcript})
    return out
