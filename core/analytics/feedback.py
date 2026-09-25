"""Scorer-tuning suggestions from performance insights.

These are DATA for Ashu to approve — nothing here ever changes the
scorer, the quality gate, or any config on its own. Each suggestion
carries a confidence level so he can judge how seriously to take it.
"""

from __future__ import annotations

from .insights import (MIN_CALIBRATION_CLIPS, MIN_GROUP_CLIPS,
                       build_insights)
from .tracker import ClipRecord


def _conf(n: int) -> str:
    if n >= 15:
        return "high"
    if n >= 6:
        return "medium"
    return "low"


def tuning_suggestions(records: list[ClipRecord]) -> list[dict]:
    """Return [{suggestion, reason, confidence, kind}]. ``kind`` is one of
    'tune' (change something), 'info' (good news, no action), 'data'
    (log more stats first)."""
    ins = build_insights(records)
    out: list[dict] = []
    n_stats = ins["clips_with_stats"]

    # 1. Not enough data — the honest gate before any advice.
    if n_stats < MIN_CALIBRATION_CLIPS:
        out.append({
            "kind": "data",
            "confidence": "high",
            "suggestion": (f"Log views for {MIN_CALIBRATION_CLIPS - n_stats} "
                           f"more clip(s) to unlock tuning advice."),
            "reason": (f"Only {n_stats} clip(s) have stats so far — "
                       f"suggestions on less data would be noise."),
        })
        return out

    # 2. A caption style is clearly winning -> suggest making it default.
    lb = ins["style_leaderboard"]
    if len(lb) >= 2:
        top, runner = lb[0], lb[1]
        n = top["clips"] + runner["clips"]
        if runner["avg_views"] > 0 and \
                top["avg_views"] / runner["avg_views"] >= 1.5:
            out.append({
                "kind": "tune",
                "confidence": _conf(n),
                "suggestion": (f"Make '{top['style']}' the default caption "
                               f"style for new clips."),
                "reason": (f"It averages "
                           f"{top['avg_views'] / runner['avg_views']:.1f}x "
                           f"the views of '{runner['style']}' "
                           f"({top['clips']} vs {runner['clips']} clips)."),
            })

    # 3. Score calibration verdict -> rubric / gate advice.
    cal = ins["score_calibration"]
    corr = cal.get("correlation")
    if corr is not None and corr < 0.4:
        out.append({
            "kind": "tune",
            "confidence": _conf(cal["n"]),
            "suggestion": ("Review the moment-scoring rubric weights "
                           "(or lower the autopilot quality gate) — "
                           "high scores aren't predicting views."),
            "reason": cal["verdict"],
        })
    elif corr is not None and corr >= 0.7:
        out.append({
            "kind": "info",
            "confidence": _conf(cal["n"]),
            "suggestion": "Scorer is healthy — no tuning needed.",
            "reason": cal["verdict"],
        })

    # 4. A niche performs despite low scores -> rubric may misjudge it.
    by_niche: dict[str, list[ClipRecord]] = {}
    for r in records:
        if r.stats.get("views") is not None:
            by_niche.setdefault(r.niche or "unknown", []).append(r)
    for niche, rs in by_niche.items():
        if len(rs) < MIN_GROUP_CLIPS:
            continue
        avg_score = sum(r.score for r in rs) / len(rs)
        avg_views = sum(r.stats["views"] for r in rs) / len(rs)
        overall_views = (sum(r.stats["views"] for r in
                             _all_with_views(records)) /
                         max(1, len(_all_with_views(records))))
        if avg_score < 60 and overall_views > 0 and \
                avg_views >= 1.5 * overall_views:
            out.append({
                "kind": "tune",
                "confidence": _conf(len(rs)),
                "suggestion": (f"Consider a lower quality gate (or a "
                               f"niche-specific tweak) for '{niche}'."),
                "reason": (f"Its clips average only {avg_score:.0f} score "
                           f"but {avg_views:,.0f} views "
                           f"({avg_views / overall_views:.1f}x the overall "
                           f"average) — the rubric undervalues this niche."),
            })

    if not out:
        out.append({
            "kind": "info",
            "confidence": "medium",
            "suggestion": "Nothing to tune right now.",
            "reason": ("No style dominates, scores track views acceptably, "
                       "and no niche is being misjudged. Keep logging stats."),
        })
    return out


def _all_with_views(records: list[ClipRecord]) -> list[ClipRecord]:
    return [r for r in records if r.stats.get("views") is not None]
