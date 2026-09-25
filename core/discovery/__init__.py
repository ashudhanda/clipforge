"""ClipForge2 source discovery — niche -> candidate long videos.

Public API:
    discover_for_autopilot(config) -> {niche_id: [candidate, ...]}
    discover_sources(...)           -> flat ranked candidate list
    SeenStore                       -> persistent "already processed" store
"""

from __future__ import annotations

from ..config import load_config
from .seen import SeenStore
from .sources import (
    NICHE_QUERY_OVERRIDES,
    QUERY_TEMPLATES,
    build_queries,
    discover_sources,
    rank_score,
)

__all__ = [
    "NICHE_QUERY_OVERRIDES",
    "QUERY_TEMPLATES",
    "SeenStore",
    "build_queries",
    "discover_for_autopilot",
    "discover_sources",
    "rank_score",
]


def discover_for_autopilot(config: dict | None = None,
                           per_niche: int = 3) -> dict[str, list[dict]]:
    """Discover fresh source videos for the niches in the user config.

    Returns {niche_id: [candidate, ...]} sorted by score, already filtered
    against the seen-store (never suggests a processed video twice).
    """
    cfg = config if config is not None else load_config()
    niches = list(cfg.get("niches") or [])
    custom = str(cfg.get("custom_niche") or "")
    seen = SeenStore()
    cands = discover_sources(niches, per_niche=per_niche, seen=seen,
                             custom_niche=custom)
    grouped: dict[str, list[dict]] = {}
    for c in cands:
        grouped.setdefault(c["niche"], []).append(c)
    return grouped
