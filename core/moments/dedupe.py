"""Overlap dedupe for clip candidates.

Adapted from the Phase 0 audit (cutawan's dedupeClips: reject a lower-scored
clip when its overlap with a kept clip exceeds 40% of the shorter clip) —
rewritten in Python. Input is sorted by score desc defensively.
"""

from __future__ import annotations

import logging

from .scorer import Clip

log = logging.getLogger("clipforge.moments.dedupe")

DEFAULT_MAX_OVERLAP = 0.40


def _overlap(a: Clip, b: Clip) -> float:
    return max(0.0, min(a.end, b.end) - max(a.start, b.start))


def dedupe_clips(
    clips: list[Clip], max_overlap: float = DEFAULT_MAX_OVERLAP
) -> list[Clip]:
    """Drop clips overlapping >max_overlap (fraction of the shorter clip).

    Higher-scored clips win. Returns the kept clips in score-desc order.
    """
    ordered = sorted(clips, key=lambda c: c.score, reverse=True)
    kept: list[Clip] = []
    for clip in ordered:
        clash = False
        for k in kept:
            ov = _overlap(clip, k)
            if ov <= 0:
                continue
            shorter = min(clip.duration(), k.duration())
            if shorter > 0 and ov / shorter > max_overlap:
                clash = True
                log.debug(
                    "dedupe: dropping %.1f-%.1f (score %d), %.0f%% overlap with %.1f-%.1f",
                    clip.start,
                    clip.end,
                    clip.score,
                    100 * ov / shorter,
                    k.start,
                    k.end,
                )
                break
        if not clash:
            kept.append(clip)
    log.info("dedupe_clips: %d/%d kept", len(kept), len(clips))
    return kept
