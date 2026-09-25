"""LLM-based moment scoring for ClipForge2.

Logic adapted from the Phase 0 audit of cutawan's highlights pipeline
(rubric-guided LLM selection, sentence-boundary snapping, ~1 clip/minute) —
rewritten in Python with our OWN rubric text, weights and thresholds.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from . import llm as _llm
from .llm import LLMError, LLMProvider

log = logging.getLogger("clipforge2.moments.scorer")


@dataclass
class Clip:
    """A candidate short-form clip moment."""

    start: float  # seconds, snapped to a sentence start
    end: float  # seconds, snapped to a sentence end
    score: int  # 0-100 (llm: virality score; offline: topic-shift strength)
    title: str
    hook_line: str
    reason: str
    source: str = "llm"  # "llm" | "offline"

    def duration(self) -> float:
        return self.end - self.start


# --- Sentence grouping ------------------------------------------------------
# Phase 1 hands us word- OR sentence-level {start, end, text} items. The LLM
# and the snapping logic both need true sentences, so we group here.

_SENT_END = re.compile(r"[.!?\u2026\u0964]+$")  # . ! ? … ।
_MAX_SENT_CHARS = 240


@dataclass
class _Sentence:
    start: float
    end: float
    text: str


def group_sentences(transcript: list[dict]) -> list[_Sentence]:
    """Group word/caption items into sentences.

    Splits on sentence-ending punctuation; a single item holding several
    sentences is split with time distributed proportionally by character
    length. Over-long runs are force-cut so no sentence exceeds the cap.
    """
    words: list[tuple[float, float, str]] = []
    for item in transcript:
        try:
            s, e, t = float(item["start"]), float(item["end"]), str(item["text"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if not t or e <= s:
            continue
        # Split multi-sentence items, sharing time by character proportion.
        parts = re.split(r"(?<=[.!?\u2026\u0964])\s+", t)
        if len(parts) > 1:
            total = sum(len(p) for p in parts) or 1
            cur = s
            for p in parts:
                share = (e - s) * len(p) / total
                words.append((cur, cur + share, p.strip()))
                cur += share
        else:
            words.append((s, e, t))

    sentences: list[_Sentence] = []
    buf: list[tuple[float, float, str]] = []
    buf_chars = 0

    def flush() -> None:
        if not buf:
            return
        sentences.append(
            _Sentence(
                start=buf[0][0],
                end=buf[-1][1],
                text=" ".join(w[2] for w in buf).strip(),
            )
        )
        buf.clear()

    for s, e, t in words:
        buf.append((s, e, t))
        buf_chars += len(t)
        if _SENT_END.search(t) or buf_chars >= _MAX_SENT_CHARS:
            flush()
            buf_chars = 0
    flush()
    # Drop empties and enforce ordering.
    out = [x for x in sentences if x.text]
    out.sort(key=lambda x: x.start)
    return out


# --- Our own virality rubric (own text, own weights) -------------------------
# Concept adapted from cutawan (weighted rubric -> 0-100); every word and every
# weight below is written fresh for ClipForge2.


def build_rubric() -> str:
    return """\
Score each candidate clip 0-100 using these five signals (weights sum to 100):

1. OPENING HOOK (0-25) — Does the first line stop a scroller? Bold claim,
   surprise, direct question, or visible stakes in the first 3 seconds.
2. EMOTIONAL INTENSITY (0-20) — Strong feeling in the delivery or content:
   excitement, awe, humour, anger, tension, suspense. Flat recaps score low.
3. USEFUL OR NOVEL (0-20) — A takeaway the viewer can use, or an insight
   fresh enough to make sharing it feel smart.
4. COMPLETE ARC (0-20) — Hook, build, and payoff ALL inside the clip. A viewer
   with zero context must understand it; it must not end mid-thought or on
   setup for something later.
5. DISCUSSION SPARK (0-15) — Would viewers comment, tag a friend, or repost
   to signal who they are?

Be strict: most clips land 35-70. Reserve 85+ for genuinely exceptional
moments. Clips that fail signal 4 (incomplete arc) must not be selected."""


def format_transcript(sentences: list[_Sentence]) -> str:
    lines = [
        f"[{i}] [{s.start:.1f}-{s.end:.1f}] {s.text}" for i, s in enumerate(sentences)
    ]
    return "\n".join(lines)


# --- Boundary snapping -------------------------------------------------------


def snap_to_sentences(
    start: float, end: float, sentences: list[_Sentence]
) -> tuple[float, float] | None:
    """Snap (start, end) onto sentence boundaries.

    Start -> start of the sentence containing it, or the next sentence's
    start when it lands in an inter-sentence gap (clips open on speech, not
    silence). End -> end of the sentence containing it, or the previous
    sentence's end for gap landings. Returns None when no valid snap exists
    (never invents).
    """
    if not sentences or end <= start:
        return None
    s_sent = next((x for x in sentences if x.start <= start < x.end), None)
    if s_sent is None:
        s_sent = next((x for x in sentences if x.start >= start), None)
        if s_sent is None:
            return None
    e_sent = next((x for x in sentences if x.start < end <= x.end), None)
    if e_sent is None:
        prev = [x for x in sentences if x.end <= end]
        if not prev:
            return None
        e_sent = prev[-1]
    new_start, new_end = s_sent.start, e_sent.end
    if new_end - new_start < 1.0:
        return None
    return (new_start, new_end)


# --- Scoring -----------------------------------------------------------------


def _target_count(duration_s: float, clips_per_minute: float) -> int:
    return max(1, min(40, round(duration_s / 60 * clips_per_minute)))


def find_moments(
    transcript: list[dict],
    provider: LLMProvider | None = None,
    clips_per_minute: float = 1.0,
    min_clip_sec: float = 8.0,
    max_clip_sec: float = 100.0,
) -> tuple[list[Clip], dict]:
    """Score transcript moments with the LLM. Returns (clips, usage).

    Raises LLMError when the LLM fails — never returns fabricated clips.
    Clips come back sorted by score desc, boundaries snapped to sentences.
    """
    sentences = group_sentences(transcript)
    if not sentences:
        log.warning("find_moments: empty/unusable transcript -> no clips")
        return [], {"est_prompt_tokens": 0}
    duration = sentences[-1].end
    n = _target_count(duration, clips_per_minute)

    candidates, usage = _llm.score_moments(
        format_transcript(sentences), build_rubric(), n, provider=provider
    )

    clips: list[Clip] = []
    for c in candidates:
        try:
            raw_start, raw_end = float(c["start"]), float(c["end"])
            raw_score = int(c["score"])
        except (KeyError, TypeError, ValueError):
            log.warning("dropping candidate with missing/invalid fields: %r", c)
            continue
        snapped = snap_to_sentences(raw_start, raw_end, sentences)
        if snapped is None:
            log.warning(
                "dropping candidate (%.1f-%.1f): no valid sentence snap",
                raw_start,
                raw_end,
            )
            continue
        s, e = snapped
        dur = e - s
        if dur < min_clip_sec or dur > max_clip_sec:
            log.warning(
                "dropping candidate (%.1f-%.1f): snapped duration %.1fs out of range",
                s,
                e,
                dur,
            )
            continue
        clips.append(
            Clip(
                start=round(s, 2),
                end=round(e, 2),
                score=max(0, min(100, raw_score)),
                title=str(c.get("title", ""))[:80],
                hook_line=str(c.get("hook_line", ""))[:160],
                reason=str(c.get("reason", ""))[:240],
                source="llm",
            )
        )
    clips.sort(key=lambda c: c.score, reverse=True)
    log.info("find_moments: %d/%d candidates kept", len(clips), len(candidates))
    return clips, usage
