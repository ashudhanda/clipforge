"""Tests for the Phase 8 analytics feedback loop.

All synthetic records — no network, no real stats. Verifies: tracker
roundtrips/persistence/corrupt-file tolerance, insight math, A/B strings,
cost accounting (no double-counting of shared usage), empty states, and
that feedback suggestions are data-only (never auto-applied).
"""

from __future__ import annotations

import json

import pytest

from core.analytics import (AnalyticsStore, build_insights,
                            tuning_suggestions)
from core.analytics.insights import (cost_summary, niche_ranking, pearson,
                                     score_calibration, style_comparisons,
                                     style_leaderboard)
from core.analytics.tracker import ClipRecord


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def make_rec(clip_id, niche="gaming", style="hormozi", score=80.0,
             views=None, likes=None, comments=None,
             usage=None, shared_usage=None, title="t"):
    r = ClipRecord(clip_id=clip_id, niche=niche, caption_style=style,
                   score=score, created_at=1.0,
                   source_url="https://youtu.be/x", title=title,
                   usage=usage or [], shared_usage=shared_usage)
    if views is not None or likes is not None or comments is not None:
        r.stats.update({"views": views, "likes": likes,
                        "comments": comments, "logged_at": 2.0})
    return r


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("CF2_CONFIG_DIR", str(tmp_path / ".clipforge2"))
    return AnalyticsStore()


# ---------------------------------------------------------------------------
# tracker
# ---------------------------------------------------------------------------

def test_record_and_get_roundtrip(store):
    rec = store.record_clip("a1_0", niche="gaming", caption_style="hormozi",
                            score=82.5, source_url="https://youtu.be/x",
                            title="Wow clip",
                            usage=[{"provider": "gemini", "model": "m",
                                    "est_prompt_tokens": 1200}],
                            shared_usage={"provider": "gemini", "model": "m",
                                          "est_prompt_tokens": 3000,
                                          "shared_among": 3})
    assert rec.clip_id == "a1_0"
    assert rec.stats["views"] is None  # never invented
    got = store.get("a1_0")
    assert got.title == "Wow clip"
    assert got.usage[0]["est_prompt_tokens"] == 1200
    assert got.shared_usage["shared_among"] == 3


def test_log_stats_and_validation(store):
    store.record_clip("a1_0", "gaming", "hormozi", 80,
                      "https://youtu.be/x", "t")
    assert store.log_stats("a1_0", views=1500, likes=40, comments=5)
    got = store.get("a1_0")
    assert (got.stats["views"], got.stats["likes"],
            got.stats["comments"]) == (1500, 40, 5)
    assert got.stats["logged_at"] is not None
    # partial update keeps the rest
    store.log_stats("a1_0", views=2000)
    assert store.get("a1_0").stats["views"] == 2000
    assert store.get("a1_0").stats["likes"] == 40
    with pytest.raises(ValueError):
        store.log_stats("a1_0", views=-3)
    with pytest.raises(ValueError):
        store.log_stats("a1_0", likes="lots")
    assert store.log_stats("missing_id", views=1) is False


def test_mark_uploaded_and_update(store):
    store.record_clip("a1_0", "gaming", "hormozi", 80,
                      "https://youtu.be/x", "t")
    assert store.mark_uploaded("a1_0", "ytABC123")
    got = store.get("a1_0")
    assert got.youtube_video_id == "ytABC123"
    assert got.uploaded_at is not None
    assert store.mark_uploaded("nope", "x") is False
    assert store.update_clip("a1_0", caption_style="beast")
    assert store.get("a1_0").caption_style == "beast"
    # unknown fields are ignored, not stored
    store.update_clip("a1_0", bogus_field="x")
    assert not hasattr(store.get("a1_0"), "bogus_field")


def test_persistence_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("CF2_CONFIG_DIR", str(tmp_path / ".clipforge2"))
    s1 = AnalyticsStore()
    s1.record_clip("a1_0", "gaming", "hormozi", 80,
                   "https://youtu.be/x", "t")
    s1.log_stats("a1_0", views=99)
    s2 = AnalyticsStore()  # fresh instance, same dir
    assert len(s2) == 1
    assert s2.get("a1_0").stats["views"] == 99


def test_corrupt_file_starts_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("CF2_CONFIG_DIR", str(tmp_path / ".clipforge2"))
    d = tmp_path / ".clipforge2"
    d.mkdir(parents=True)
    (d / "analytics.json").write_text("{not valid json[[[",
                                      encoding="utf-8")
    s = AnalyticsStore()
    assert len(s) == 0  # no crash, no invented records
    # backup kept, and new writes still work
    assert (d / "analytics.json.corrupt").exists()
    s.record_clip("a1_0", "gaming", "hormozi", 80, "https://youtu.be/x", "t")
    assert len(s) == 1


def test_config_dir_respected(tmp_path, monkeypatch):
    monkeypatch.setenv("CF2_CONFIG_DIR", str(tmp_path / "custom"))
    s = AnalyticsStore()
    s.record_clip("a1_0", "gaming", "hormozi", 80, "https://youtu.be/x", "t")
    assert (tmp_path / "custom" / "analytics.json").exists()


# ---------------------------------------------------------------------------
# insights
# ---------------------------------------------------------------------------

def test_pearson_math():
    assert pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)
    assert pearson([1, 2, 3], [6, 4, 2]) == pytest.approx(-1.0)
    assert pearson([1], [2]) is None          # < 2 points
    assert pearson([5, 5, 5], [1, 2, 3]) is None  # no variance


def test_niche_ranking_and_style_leaderboard():
    recs = [
        make_rec("a", niche="gaming", style="hormozi", views=1000),
        make_rec("b", niche="gaming", style="hormozi", views=3000),
        make_rec("c", niche="finance", style="minimal", views=500),
        make_rec("d", niche="finance", style="minimal", views=700),
        make_rec("e", niche="comedy", style="beast", views=9999),  # 1 clip: excluded
        make_rec("f", niche="gaming", style="hormozi"),            # no stats: excluded
    ]
    rank = niche_ranking(recs)
    assert [r["niche"] for r in rank] == ["gaming", "finance"]
    assert rank[0]["avg_views"] == 2000
    lb = style_leaderboard(recs)
    assert [r["style"] for r in lb] == ["hormozi", "minimal"]
    comps = style_comparisons(lb)
    assert any("hormozi averages 3.3x more views than minimal" in c
               for c in comps)


def test_style_comparisons_skip_ties():
    lb = [{"style": "a", "clips": 2, "avg_views": 100, "total_views": 200},
          {"style": "b", "clips": 2, "avg_views": 105, "total_views": 210}]
    assert style_comparisons(lb) == []  # 1.05x is noise, not a result


def test_score_calibration_needs_data():
    recs = [make_rec("a", views=100), make_rec("b", views=200)]
    cal = score_calibration(recs)
    assert cal["correlation"] is None
    assert "Not enough data" in cal["verdict"]


def test_score_calibration_strong_and_weak():
    strong = [make_rec(f"s{i}", score=50 + i * 10, views=100 * (i + 1))
              for i in range(6)]
    cal = score_calibration(strong)
    assert cal["correlation"] == pytest.approx(1.0)
    assert "Strong link" in cal["verdict"]
    weak = [make_rec(f"w{i}", score=90 - i, views=100 + (i % 2) * 900)
            for i in range(6)]
    cal2 = score_calibration(weak)
    assert cal2["correlation"] < 0.4
    assert ("Weak link" in cal2["verdict"] or
            "Negative link" in cal2["verdict"])  # both = scores mislead


def test_cost_summary_no_double_count():
    recs = [
        make_rec("a", usage=[{"provider": "gemini", "model": "m",
                              "est_prompt_tokens": 1000}],
                 shared_usage={"provider": "gemini", "model": "m",
                               "est_prompt_tokens": 3000,
                               "shared_among": 3}),
        make_rec("b", usage=[{"provider": "gemini", "model": "m",
                              "est_prompt_tokens": 1000}],
                 shared_usage={"provider": "gemini", "model": "m",
                               "est_prompt_tokens": 3000,
                               "shared_among": 3}),
    ]
    cost = cost_summary(recs)
    # per-clip: 1000 + 3000/3 = 2000 tokens each; total 4000, not 8000
    assert cost["total_est_prompt_tokens"] == 4000
    assert cost["avg_tokens_per_clip"] == 2000
    assert cost["total_est_usd"] > 0
    assert len(cost["per_clip"]) == 2


def test_build_insights_empty_state():
    ins = build_insights([make_rec("a"), make_rec("b")])
    assert ins["has_data"] is False
    assert ins["clips_with_stats"] == 0
    assert "No stats logged yet" in ins["empty_message"]
    assert ins["niche_ranking"] == []
    assert ins["style_leaderboard"] == []
    # cost still tracked for clips without stats
    assert ins["cost"]["total_clips"] == 2


# ---------------------------------------------------------------------------
# feedback — suggestions are data, never auto-applied
# ---------------------------------------------------------------------------

def test_feedback_needs_data_first():
    recs = [make_rec("a", views=100)]
    sugs = tuning_suggestions(recs)
    assert len(sugs) == 1
    assert sugs[0]["kind"] == "data"
    assert "unlock tuning advice" in sugs[0]["suggestion"]


def test_feedback_style_winner():
    recs = ([make_rec(f"h{i}", style="hormozi", views=2000)
             for i in range(4)] +
            [make_rec(f"m{i}", style="minimal", views=500)
             for i in range(4)])
    sugs = tuning_suggestions(recs)
    tune = [s for s in sugs if s["kind"] == "tune"]
    assert any("hormozi" in s["suggestion"] and "default" in s["suggestion"]
               for s in tune)
    for s in sugs:  # every suggestion carries confidence + reason
        assert s["confidence"] in ("high", "medium", "low")
        assert s["reason"]


def test_feedback_low_correlation_suggests_rubric_review():
    recs = [make_rec(f"w{i}", score=95 - i, views=100 + (i % 3) * 50)
            for i in range(6)]
    sugs = tuning_suggestions(recs)
    assert any("rubric" in s["suggestion"] for s in sugs
               if s["kind"] == "tune")


def test_feedback_healthy_scorer_reports_ok():
    recs = [make_rec(f"s{i}", score=50 + i * 8, views=200 * (i + 1))
            for i in range(6)]
    sugs = tuning_suggestions(recs)
    assert any(s["kind"] == "info" and "healthy" in s["suggestion"]
               for s in sugs)


def test_feedback_niche_misjudged():
    recs = ([make_rec(f"g{i}", niche="gaming", score=85, views=1000)
             for i in range(4)] +
            [make_rec(f"f{i}", niche="finance", score=45, views=3000)
             for i in range(4)])
    sugs = tuning_suggestions(recs)
    assert any("finance" in s["suggestion"] and "quality gate" in s["suggestion"]
               for s in sugs if s["kind"] == "tune")


def test_feedback_never_mutates_records():
    recs = [make_rec(f"h{i}", style="hormozi", views=2000) for i in range(4)] + \
           [make_rec(f"m{i}", style="minimal", views=500) for i in range(4)]
    before = json.dumps([r.to_dict() for r in recs], sort_keys=True)
    tuning_suggestions(recs)
    build_insights(recs)
    after = json.dumps([r.to_dict() for r in recs], sort_keys=True)
    assert before == after  # suggestions are data-only, nothing applied
