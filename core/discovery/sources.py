"""Niche -> YouTube source discovery via yt-dlp search. No API key needed.

Own implementation. How it works:
1. Build search queries from per-niche templates (customizable dict below).
2. Run ``ytsearchN:`` with ``extract_flat`` (fast, one request per query) to
   collect candidate video IDs.
3. Fetch full info (no download) for the most promising candidates to check
   captions/subtitles and get accurate metadata.
4. Apply hard filters (Shorts, age-restricted, paid/auth-only, live/upcoming).
5. Rank with our own heuristic (documented in :func:`rank_score`) and return
   the top ``per_niche`` unseen videos per niche.

YouTube respect rules: modest query counts, per-request try/except so a
429/bot-check on one query never kills the whole run — we return what we got
and log the rest. Never invents metadata: everything comes from yt-dlp output.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Callable, Optional

from yt_dlp import YoutubeDL

from .. import niches as niches_mod
from ..ingest.ytdlp_helper import base_opts, extract_video_id

log = logging.getLogger(__name__)

# --- tuning (not hardcoded secrets; plain search/ranking policy) ------------
MIN_DURATION = 60        # hard skip: anything shorter is a Short / too short
PREFERRED_DURATION = 480  # 8+ minutes is ideal long-form for clipping
SEARCH_RESULTS = 10      # ytsearchN per query
INFO_FETCH_MULTIPLIER = 3  # fetch full info for per_niche * this many

# Default query templates. {name} = niche display name (or custom niche text).
QUERY_TEMPLATES: tuple[str, ...] = (
    "{name} full video",
    "{name} best moments",
    "{name} highlights 2026",
)

# Per-niche overrides where generic templates are weak. niche_id -> templates.
# Users can extend this dict for their own niches (e.g. via custom niche text).
NICHE_QUERY_OVERRIDES: dict[str, tuple[str, ...]] = {
    "gta6-breakdowns": (
        "GTA 6 trailer breakdown analysis",
        "GTA 6 new details explained",
        "GTA 6 gameplay analysis",
    ),
    "speedruns": (
        "world record speedrun full run",
        "speedrun explained documentary",
    ),
    "stand-up": (
        "stand up comedy full special",
        "best stand up comedy 2026",
    ),
    "movie-explainers": (
        "movie explained in hindi",  # overridden per custom text in practice
        "movie recap full story",
    ),
    "horror-stories": (
        "horror stories narrated full",
        "scary stories to tell",
    ),
    "true-crime": (
        "true crime documentary full",
        "true crime cases explained",
    ),
    "motivational-speeches": (
        "motivational speech full",
        "best motivational speeches compilation",
    ),
    "celebrity-interviews": (
        "celebrity interview full episode",
        "podcast interview highlights",
    ),
}

# Availability values that mean "can't freely watch/download".
_BLOCKED_AVAILABILITY = {
    "premium_only", "subscriber_only", "needs_auth", "needs_premium",
    "unplayable",
}
# Live states that mean "not a finished VOD".
_BLOCKED_LIVE = {"is_live", "is_upcoming", "is_post_live"}


def build_queries(niche_id: str, custom_niche: str = "") -> list[str]:
    """Render search queries for a niche. Pure function — easy to test."""
    if niche_id == "custom":
        name = (custom_niche or "").strip() or "videos"
    else:
        name = niches_mod.niche_name(niche_id)
    templates = NICHE_QUERY_OVERRIDES.get(niche_id, QUERY_TEMPLATES)
    return [t.format(name=name) for t in templates]


def _ydl_factory_default(opts: dict) -> YoutubeDL:
    return YoutubeDL(opts)


def _flat_search(ydl_factory: Callable[[dict], YoutubeDL],
                 query: str, n: int = SEARCH_RESULTS) -> list[dict]:
    """One ytsearch query, flat extraction. Returns raw entry dicts."""
    opts = base_opts({"extract_flat": True, "skip_download": True})
    with ydl_factory(opts) as ydl:
        data = ydl.extract_info(f"ytsearch{n}:{query}", download=False) or {}
    entries = data.get("entries") or []
    return [e for e in entries if isinstance(e, dict) and e.get("id")]


def _video_info(ydl_factory: Callable[[dict], YoutubeDL],
                video_id: str) -> Optional[dict]:
    """Full info for one video, no download. None on any failure."""
    opts = base_opts({"skip_download": True})
    url = f"https://www.youtube.com/watch?v={video_id}"
    with ydl_factory(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return info if isinstance(info, dict) else None


def _has_captions(info: dict) -> bool:
    return bool(info.get("subtitles")) or bool(info.get("automatic_captions"))


def _age_days(upload_date: Optional[str], now: Optional[float] = None) -> Optional[float]:
    """upload_date is YYYYMMDD. Returns age in days, None if unknown."""
    if not upload_date or len(str(upload_date)) != 8 or not str(upload_date).isdigit():
        return None
    try:
        ts = time.mktime(time.strptime(str(upload_date), "%Y%m%d"))
    except (ValueError, OverflowError):
        return None
    return max(0.0, ((now if now is not None else time.time()) - ts) / 86400.0)


def rank_score(*, has_captions: bool, duration: Optional[float],
               age_days: Optional[float], view_count: Optional[int]) -> tuple[float, list[str]]:
    """Our own ranking heuristic. Returns (score, human-readable reasons).

    score = 2.0 * captions
          + duration_bonus   (1.0 if >= 8 min, else duration/480; unknown -> 0.25)
          + recency_bonus    (1.0 if <= 30 days old, 0.5 if <= 180 days, else 0)
          + velocity_bonus   (min(2.0, log10(views_per_day + 1)); unknown -> 0)

    Captions are worth double because the captions-first ingest path is our
    cheapest and most reliable. Fresh, fast-moving videos rank higher because
    they carry more clip-worthy momentum.
    """
    reasons: list[str] = []
    score = 0.0

    if has_captions:
        score += 2.0
        reasons.append("has captions")

    if duration is None:
        score += 0.25
    elif duration >= PREFERRED_DURATION:
        score += 1.0
        reasons.append("long-form (8+ min)")
    elif duration > 0:
        score += duration / PREFERRED_DURATION
        reasons.append(f"{duration / 60:.0f} min")

    if age_days is not None:
        if age_days <= 30:
            score += 1.0
            reasons.append("fresh (<=30d)")
        elif age_days <= 180:
            score += 0.5
            reasons.append("recent (<=6mo)")

    if view_count and age_days:
        vpd = view_count / max(age_days, 1.0)
        vel = min(2.0, math.log10(vpd + 1))
        score += vel
        if vel >= 1.0:
            reasons.append("trending views")

    return round(score, 3), reasons


def _passes_filters(info: dict) -> Optional[str]:
    """Hard filters. Returns None if the video passes, else a skip reason."""
    dur = info.get("duration")
    if isinstance(dur, (int, float)) and dur < MIN_DURATION:
        return "short (<60s)"
    if (info.get("age_limit") or 0) > 0:
        return "age-restricted"
    if str(info.get("availability") or "").lower() in _BLOCKED_AVAILABILITY:
        return f"not freely watchable ({info.get('availability')})"
    if str(info.get("live_status") or "") in _BLOCKED_LIVE:
        return f"live/upcoming ({info.get('live_status')})"
    return None


def _candidate_from_info(info: dict, niche_id: str,
                         now: Optional[float] = None) -> dict:
    vid = str(info.get("id") or "")
    age = _age_days(info.get("upload_date"), now=now)
    caps = _has_captions(info)
    score, reasons = rank_score(
        has_captions=caps,
        duration=info.get("duration"),
        age_days=age,
        view_count=info.get("view_count"),
    )
    dur = info.get("duration")
    return {
        "video_id": vid,
        "url": f"https://www.youtube.com/watch?v={vid}",
        "title": str(info.get("title") or "Untitled"),
        "duration": dur,
        "duration_str": _fmt_duration(dur),
        "view_count": info.get("view_count"),
        "upload_date": info.get("upload_date"),
        "has_captions": caps,
        "niche": niche_id,
        "score": score,
        "reasons": reasons,
    }


def _fmt_duration(dur) -> str:
    if not isinstance(dur, (int, float)) or dur <= 0:
        return "?"
    m, s = divmod(int(dur), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _discover_niche(ydl_factory: Callable[[dict], YoutubeDL],
                   niche_id: str,
                   per_niche: int,
                   max_queries: int,
                   custom_niche: str,
                   is_seen,
                   skip_seen: bool,
                   now: Optional[float]) -> list[dict]:
    """One niche: search, fetch info, filter, rank. Pure discovery step.

    ``skip_seen`` controls whether videos already in the seen-store are
    skipped (fresh pass) or kept (reuse fallback pass).
    """
    queries = build_queries(niche_id, custom_niche)[:max_queries]
    flat_ids: list[str] = []
    seen_ids: set[str] = set()
    for q in queries:
        try:
            entries = _flat_search(ydl_factory, q)
        except Exception as exc:  # 429 / bot-check / network — non-fatal
            log.warning("discovery search failed for %r: %s", q, exc)
            continue
        for e in entries:
            vid = extract_video_id(str(e.get("id") or "")) or str(e.get("id"))
            if vid and vid not in seen_ids:
                seen_ids.add(vid)
                flat_ids.append(vid)

    # Full info for the most promising flat IDs (captions need it).
    infos: list[dict] = []
    for vid in flat_ids[: max(1, per_niche * INFO_FETCH_MULTIPLIER)]:
        if skip_seen and is_seen(vid):
            continue
        try:
            info = _video_info(ydl_factory, vid)
        except Exception as exc:  # non-fatal, same as above
            log.warning("discovery info fetch failed for %s: %s", vid, exc)
            continue
        if not info:
            continue
        skip_reason = _passes_filters(info)
        if skip_reason:
            log.debug("discovery skipping %s: %s", vid, skip_reason)
            continue
        infos.append(info)

    return sorted(
        (_candidate_from_info(i, niche_id, now=now) for i in infos),
        key=lambda c: c["score"], reverse=True,
    )[:per_niche]


def discover_sources(niches: list[str],
                     per_niche: int = 3,
                     max_queries: int = 3,
                     custom_niche: str = "",
                     seen: Optional[object] = None,
                     ydl_factory: Optional[Callable[[dict], YoutubeDL]] = None,
                     now: Optional[float] = None) -> list[dict]:
    """Discover candidate source videos for niches. Never raises on
    YouTube-side failures — a throttled query is logged and skipped, and we
    return whatever we managed to collect.

    ``seen``: optional object with ``is_seen(video_id) -> bool`` (e.g.
    :class:`core.discovery.seen.SeenStore`). ``ydl_factory`` is injectable
    for tests (mocked yt-dlp, zero network).

    Reuse fallback: when a niche yields zero *fresh* candidates (everything
    found was already processed), we do a second pass *without* the
    seen-filter and return those videos marked ``seen_before=True``.
    Showing a previously-used video beats showing nothing — the UI marks
    them so the user can decide.
    """
    factory = ydl_factory or _ydl_factory_default
    is_seen = getattr(seen, "is_seen", None) or (lambda _vid: False)
    candidates: list[dict] = []

    for niche_id in niches or []:
        niche_cands = _discover_niche(
            factory, niche_id, per_niche=per_niche, max_queries=max_queries,
            custom_niche=custom_niche, is_seen=is_seen, skip_seen=True,
            now=now)
        if not niche_cands:
            # Fallback: reuse is acceptable, an empty list is not.
            niche_cands = _discover_niche(
                factory, niche_id, per_niche=per_niche, max_queries=max_queries,
                custom_niche=custom_niche, is_seen=is_seen, skip_seen=False,
                now=now)
            for c in niche_cands:
                c["seen_before"] = True
        candidates.extend(niche_cands)

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates
