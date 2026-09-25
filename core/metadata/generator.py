"""Metadata generation for ClipForge: title / description / hashtags per clip.

Own implementation. Uses the shared LLM abstraction in core.moments.llm
(Gemini default, OpenAI optional) — no second provider layer here.

Honesty rules (hard):
- LLM/validation failures raise LLMError. We NEVER invent metadata for
  content the transcript doesn't support.
- "keywords_used" returned by the LLM must appear verbatim (case-insensitive)
  in the transcript, otherwise the result is rejected as invented SEO.
- Titles must never promise what the clip doesn't show — enforced via prompt
  plus the keyword check; structural rules (length, no hashtags) are
  validated in code.
"""

from __future__ import annotations

import logging
import re

from core.moments.llm import LLMError, estimate_tokens, get_provider

log = logging.getLogger("clipforge.metadata.generator")

MAX_TITLE_CHARS = 60
MIN_HASHTAGS = 3
MAX_HASHTAGS = 5
MIN_DESC_LINES = 2
MAX_DESC_LINES = 4
MAX_TRANSCRIPT_CHARS = 2000  # cost guard: truncate long transcripts in prompts

# Generic spam tags that add zero SEO value and look cheap.
BANNED_HASHTAGS = frozenset(
    {
        "viral",
        "fyp",
        "foryou",
        "foryoupage",
        "trending",
        "trend",
        "shorts",
        "short",
        "shortvideo",
        "video",
        "videos",
        "youtube",
        "yt",
        "like",
        "likes",
        "follow",
        "subscribe",
        "sub",
        "comment",
        "share",
        "new",
    }
)

_HASHTAG_TOKEN = re.compile(r"#[A-Za-z0-9_]+")
_VALID_TAG = re.compile(r"^[a-z0-9_]{2,30}$")
_WS = re.compile(r"\s+")


def transcript_to_text(clip_transcript) -> str:
    """Normalize transcript input to plain text.

    Accepts a plain string or a list of {start, end, text} dicts (Phase 1
    output). Raises ValueError on empty/unusable input.
    """
    if isinstance(clip_transcript, str):
        text = clip_transcript.strip()
    elif isinstance(clip_transcript, list):
        parts = []
        for item in clip_transcript:
            if isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]).strip())
            elif isinstance(item, str) and item.strip():
                parts.append(item.strip())
        text = " ".join(parts)
    else:
        raise ValueError(
            "clip_transcript must be a string or a list of {start,end,text}"
        )
    text = _WS.sub(" ", text).strip()
    if not text:
        raise ValueError("clip_transcript is empty")
    return text


def _build_prompts(transcript_text: str, niche: str, style_hints: dict) -> tuple[str, str]:
    tone = str(style_hints.get("tone", "punchy and curious"))
    language = str(style_hints.get("language", "English"))
    audience = str(style_hints.get("audience", "general short-form viewers"))

    shown = transcript_text
    truncated = False
    if len(shown) > MAX_TRANSCRIPT_CHARS:
        shown = shown[:MAX_TRANSCRIPT_CHARS].rsplit(" ", 1)[0] + "…"
        truncated = True

    system = (
        "You are a YouTube Shorts metadata writer. You write titles, "
        "descriptions and hashtags that are punchy but HONEST: every claim "
        "must be supported by the transcript below. Never invent events, "
        "numbers, quotes, names or outcomes that are not in the transcript. "
        "Reply with JSON only."
    )
    user = (
        f"Niche: {niche}\n"
        f"Tone: {tone}\n"
        f"Language: {language}\n"
        f"Audience: {audience}\n\n"
        "Write metadata for a short clip whose transcript is:\n\n"
        "---\n"
        f"{shown}\n"
        "---\n\n"
        "Rules:\n"
        f'- "title": punchy, max {MAX_TITLE_CHARS} characters, NO hashtags, '
        "NO clickbait lies — it must describe only what the clip actually shows.\n"
        '- "description": 2-4 short lines in natural language summarizing the '
        "moment, then exactly ONE engaging question to drive comments. Put "
        "hashtags ONLY on the final line, nowhere else in the description.\n"
        f'- "hashtags": {MIN_HASHTAGS}-{MAX_HASHTAGS} lowercase tags, relevant '
        "to the niche and the clip. Never use generic spam tags like #viral, "
        "#fyp, #trending or #shorts.\n"
        "- SEO: naturally work 1-2 real keywords or phrases FROM the transcript "
        "into the title and description.\n"
        '- "keywords_used": list the exact transcript keywords/phrases you used, '
        "copied EXACTLY as they appear in the transcript.\n\n"
        "Reply as a JSON object:\n"
        '{"title": "...", "description": "...", '
        '"hashtags": ["...", ...], "keywords_used": ["...", ...]}'
    )
    if truncated:
        log.info(
            "metadata prompt: transcript truncated to %d chars (cost guard)",
            MAX_TRANSCRIPT_CHARS,
        )
    return system, user


def validate_metadata(meta: dict, transcript_text: str) -> dict:
    """Validate + normalize LLM metadata. Raises LLMError on any violation.

    Returns {"title", "description", "hashtags"} with hashtags normalized
    (lowercase, deduped, banned tags removed) and the description's hashtag
    line rebuilt canonically as the final line.
    """
    if not isinstance(meta, dict):
        raise LLMError("LLM metadata was not a JSON object — refusing to guess")

    # --- title ------------------------------------------------------------
    title = str(meta.get("title") or "").strip()
    title = _HASHTAG_TOKEN.sub("", title)  # no hashtags in titles, ever
    title = _WS.sub(" ", title).strip()
    if not title:
        raise LLMError("LLM returned an empty title")
    if len(title) > MAX_TITLE_CHARS:
        raise LLMError(
            f"title too long ({len(title)} > {MAX_TITLE_CHARS} chars): {title!r}"
        )

    # --- hashtags ---------------------------------------------------------
    raw_tags = meta.get("hashtags")
    if not isinstance(raw_tags, list):
        raise LLMError("LLM 'hashtags' was not a list")
    tags: list[str] = []
    for raw in raw_tags:
        tag = str(raw or "").strip().lower().lstrip("#").strip()
        tag = _WS.sub("", tag)
        if not tag or tag in BANNED_HASHTAGS:
            continue
        if not _VALID_TAG.match(tag):
            continue
        if tag not in tags:
            tags.append(tag)
    if not MIN_HASHTAGS <= len(tags) <= MAX_HASHTAGS:
        raise LLMError(
            f"need {MIN_HASHTAGS}-{MAX_HASHTAGS} valid hashtags, got {len(tags)} "
            f"from {raw_tags!r}"
        )

    # --- description ------------------------------------------------------
    desc = str(meta.get("description") or "")
    body_lines: list[str] = []
    for line in desc.split("\n"):
        line = _HASHTAG_TOKEN.sub("", line)  # hashtags live on the last line only
        line = _WS.sub(" ", line).strip()
        if line:
            body_lines.append(line)
    if not MIN_DESC_LINES <= len(body_lines) <= MAX_DESC_LINES:
        raise LLMError(
            f"description needs {MIN_DESC_LINES}-{MAX_DESC_LINES} content lines, "
            f"got {len(body_lines)}"
        )
    body = "\n".join(body_lines)
    if "?" not in body:
        raise LLMError("description has no engaging question ('?' missing)")

    # --- SEO honesty: keywords must exist verbatim in the transcript -------
    keywords = meta.get("keywords_used") or []
    if not isinstance(keywords, list):
        raise LLMError("LLM 'keywords_used' was not a list")
    lowered = transcript_text.lower()
    for kw in keywords:
        kw_norm = _WS.sub(" ", str(kw or "")).strip().lower()
        if kw_norm and kw_norm not in lowered:
            raise LLMError(
                f"keyword {kw!r} not found verbatim in transcript — "
                "refusing invented SEO"
            )

    hashtag_line = " ".join(f"#{t}" for t in tags)
    return {
        "title": title,
        "description": f"{body}\n{hashtag_line}",
        "hashtags": tags,
    }


def generate_metadata(
    clip_transcript,
    niche: str,
    style_hints: dict | None = None,
    provider=None,
    max_attempts: int = 2,
) -> tuple[dict, dict]:
    """Generate {title, description, hashtags} for one clip's transcript.

    Returns (metadata_dict, usage_dict). Raises LLMError on LLM failure or
    validation failure (never fabricates metadata); ValueError on bad input.
    """
    text = transcript_to_text(clip_transcript)
    niche = str(niche or "").strip()
    if not niche:
        raise ValueError("niche is required")
    style_hints = dict(style_hints or {})

    provider = provider or get_provider()  # raises LLMError if no API key
    system, user = _build_prompts(text, niche, style_hints)
    log.info(
        "metadata.generate via %s: ~%d prompt tokens",
        provider.name,
        estimate_tokens(system + user),
    )

    last_err: LLMError | None = None
    for attempt in range(1, max_attempts + 1):
        # Transport/parse failures propagate immediately — honest, never faked.
        parsed, usage = provider.generate_json(system, user)
        try:
            return validate_metadata(parsed, text), usage
        except LLMError as e:
            last_err = e
            log.warning(
                "metadata validation failed (attempt %d/%d): %s",
                attempt,
                max_attempts,
                e,
            )
    raise LLMError(
        f"LLM metadata failed validation after {max_attempts} attempts: {last_err}"
    )
