"""Performance insights from clip records — plain language, no jargon.

Every number here comes from actually logged stats. Groups with too
little data are excluded (or flagged), never padded with guesses.
"""

from __future__ import annotations

import math

from .tracker import STAT_FIELDS, ClipRecord

# Minimum clips-with-stats before a niche/style shows up in a ranking.
MIN_GROUP_CLIPS = 2
# Minimum clips-with-stats before we trust a score↔views correlation.
MIN_CALIBRATION_CLIPS = 5

# Rough $ per 1M input prompt tokens (input only — we only log prompt
# tokens). Clearly estimates; free-tier usage is often $0 in reality.
PRICE_PER_MTOK = {
    "gemini": 0.10,
    "openai": 0.60,   # blended across mini/standard; rough
}
DEFAULT_PRICE_PER_MTOK = 0.10


def _with_views(records: list[ClipRecord]) -> list[ClipRecord]:
    return [r for r in records if r.stats.get("views") is not None]


def pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation, or None when undefined (< 2 points / no variance)."""
    n = len(xs)
    if n != len(ys) or n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def niche_ranking(records: list[ClipRecord]) -> list[dict]:
    """Per-niche average views, best first. Only niches with real stats."""
    groups: dict[str, list[int]] = {}
    for r in _with_views(records):
        groups.setdefault(r.niche or "unknown", []).append(r.stats["views"])
    rows = [{"niche": n, "clips": len(v),
             "avg_views": sum(v) / len(v),
             "total_views": sum(v)}
            for n, v in groups.items() if len(v) >= MIN_GROUP_CLIPS]
    rows.sort(key=lambda r: r["avg_views"], reverse=True)
    return rows


def style_leaderboard(records: list[ClipRecord]) -> list[dict]:
    """Per-caption-style average views, best first."""
    groups: dict[str, list[int]] = {}
    for r in _with_views(records):
        groups.setdefault(r.caption_style or "unknown", []).append(
            r.stats["views"])
    rows = [{"style": s, "clips": len(v),
             "avg_views": sum(v) / len(v),
             "total_views": sum(v)}
            for s, v in groups.items() if len(v) >= MIN_GROUP_CLIPS]
    rows.sort(key=lambda r: r["avg_views"], reverse=True)
    return rows


def style_comparisons(leaderboard: list[dict]) -> list[str]:
    """Plain-language A/B lines, e.g. 'hormozi averages 2.3x more views'."""
    lines = []
    for i, top in enumerate(leaderboard):
        for other in leaderboard[i + 1:]:
            if not other["avg_views"]:
                continue
            ratio = top["avg_views"] / other["avg_views"]
            if ratio >= 1.2:
                lines.append(
                    f"{top['style']} averages {ratio:.1f}x more views than "
                    f"{other['style']} "
                    f"({top['clips']} clips vs {other['clips']})")
            elif ratio <= 1 / 1.2:
                lines.append(
                    f"{other['style']} averages {1 / ratio:.1f}x more views "
                    f"than {top['style']} "
                    f"({other['clips']} clips vs {top['clips']})")
    return lines


def score_calibration(records: list[ClipRecord]) -> dict:
    """Does our moment score actually predict views? Returns correlation
    plus a plain-language verdict. Honest about sample size."""
    pts = [(r.score, r.stats["views"]) for r in _with_views(records)]
    n = len(pts)
    if n < MIN_CALIBRATION_CLIPS:
        return {"n": n, "correlation": None,
                "verdict": f"Not enough data yet — log stats for "
                           f"{MIN_CALIBRATION_CLIPS - n} more clip(s) and "
                           f"we'll check whether scores predict views."}
    corr = pearson([p[0] for p in pts], [p[1] for p in pts])
    if corr is None:
        return {"n": n, "correlation": None,
                "verdict": "Scores or views don't vary enough to judge "
                           "yet — keep logging."}
    corr = round(corr, 2)
    if corr >= 0.7:
        verdict = (f"Strong link (r={corr}): high-scoring clips really do "
                   f"get more views. The scorer is earning its keep.")
    elif corr >= 0.4:
        verdict = (f"Moderate link (r={corr}): scores point the right way "
                   f"but there's noise — normal for small samples.")
    elif corr >= 0.0:
        verdict = (f"Weak link (r={corr}): high scores aren't turning into "
                   f"views reliably. The scoring rubric may need tuning.")
    else:
        verdict = (f"Negative link (r={corr}): higher scores are getting "
                   f"FEWER views — the scorer is misleading and needs "
                   f"reweighting.")
    return {"n": n, "correlation": corr, "verdict": verdict}


def _usage_price(usage: dict) -> float:
    provider = str(usage.get("provider", "")).lower()
    rate = PRICE_PER_MTOK.get(provider, DEFAULT_PRICE_PER_MTOK)
    return usage.get("est_prompt_tokens", 0) / 1_000_000 * rate


def cost_summary(records: list[ClipRecord]) -> dict:
    """LLM token + rough $ cost. Shared (per-video) usage is split across
    that video's clips so nothing is double-counted."""
    total_tokens = 0
    total_usd = 0.0
    per_clip: list[dict] = []
    for r in records:
        toks = sum(int(u.get("est_prompt_tokens", 0) or 0)
                   for u in r.usage)
        usd = sum(_usage_price(u) for u in r.usage)
        shared = r.shared_usage or {}
        shared_n = max(1, int(shared.get("shared_among", 1) or 1))
        toks += int(shared.get("est_prompt_tokens", 0) or 0) // shared_n
        usd += _usage_price(shared) / shared_n if shared else 0.0
        total_tokens += toks
        total_usd += usd
        per_clip.append({"clip_id": r.clip_id, "est_prompt_tokens": toks,
                         "est_usd": round(usd, 4)})
    n = len(records)
    return {
        "total_clips": n,
        "total_est_prompt_tokens": total_tokens,
        "total_est_usd": round(total_usd, 4),
        "avg_tokens_per_clip": round(total_tokens / n, 1) if n else 0,
        "avg_usd_per_clip": round(total_usd / n, 4) if n else 0,
        "note": ("Rough estimate from prompt tokens only. If you're on a "
                 "provider's free tier the real cost may be $0."),
        "per_clip": per_clip,
    }


def build_insights(records: list[ClipRecord]) -> dict:
    """Everything the dashboard's Analytics section needs in one dict."""
    with_stats = _with_views(records)
    leaderboard = style_leaderboard(records)
    ranking = niche_ranking(records)
    calibration = score_calibration(records)
    return {
        "total_clips": len(records),
        "clips_with_stats": len(with_stats),
        "has_data": len(with_stats) > 0,
        "empty_message": ("No stats logged yet — build some clips, upload "
                          "them, then tap 📊 on any clip to log its "
                          "views/likes/comments.") if not with_stats else "",
        "niche_ranking": ranking,
        "style_leaderboard": leaderboard,
        "style_comparisons": style_comparisons(leaderboard),
        "score_calibration": calibration,
        "cost": cost_summary(records),
    }
