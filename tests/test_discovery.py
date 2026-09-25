"""Phase 7 tests: discovery filters, ranking, seen-store, query templates.

yt-dlp is fully mocked via an injectable factory — zero network calls.
"""

import json

import pytest

from core.discovery import SeenStore, build_queries, discover_sources, rank_score
from core.discovery.sources import (
    _fmt_duration,
    _passes_filters,
    NICHE_QUERY_OVERRIDES,
    QUERY_TEMPLATES,
)


# ---------------------------------------------------------------------------
# fake yt-dlp
# ---------------------------------------------------------------------------

NOW = 1_788_000_000.0  # fixed "now" for deterministic age math


def _flat(*vids):
    return {"entries": [{"id": v, "title": f"Video {v}"} for v in vids]}


INFOS = {
    # long, captioned, fresh, popular -> top rank
    "vidAAA11111": {"id": "vidAAA11111", "title": "Great long video",
                    "duration": 1200, "view_count": 500_000,
                    "upload_date": "20260920",
                    "subtitles": {"en": []}, "automatic_captions": {}},
    # long, NO captions -> lower
    "vidBBB22222": {"id": "vidBBB22222", "title": "No captions here",
                    "duration": 900, "view_count": 500_000,
                    "upload_date": "20260920",
                    "subtitles": {}, "automatic_captions": {}},
    # short -> filtered out
    "vidCCC33333": {"id": "vidCCC33333", "title": "A short",
                    "duration": 45, "view_count": 9_999_999,
                    "upload_date": "20260924",
                    "subtitles": {"en": []}, "automatic_captions": {}},
    # age-restricted -> filtered out
    "vidDDD44444": {"id": "vidDDD44444", "title": "Restricted",
                    "duration": 1500, "view_count": 100,
                    "upload_date": "20260901", "age_limit": 18,
                    "subtitles": {"en": []}, "automatic_captions": {}},
    # upcoming live -> filtered out
    "vidEEE55555": {"id": "vidEEE55555", "title": "Premiere soon",
                    "duration": 3600, "view_count": 10,
                    "upload_date": "20260925", "live_status": "is_upcoming",
                    "subtitles": {}, "automatic_captions": {}},
    # premium-only -> filtered out
    "vidFFF66666": {"id": "vidFFF66666", "title": "Members only",
                    "duration": 2000, "view_count": 50_000,
                    "upload_date": "20260801", "availability": "premium_only",
                    "subtitles": {"en": []}, "automatic_captions": {}},
    # old but captioned long video -> mid rank
    "vidGGG77777": {"id": "vidGGG77777", "title": "Old classic",
                    "duration": 1800, "view_count": 2_000_000,
                    "upload_date": "20240101",
                    "subtitles": {"en": []}, "automatic_captions": {}},
}


class FakeYDL:
    """Mimics the YoutubeDL context-manager bits we use."""

    def __init__(self, opts, ids, fail_queries, fail_vids):
        self.opts = opts
        self.ids = ids
        self.fail_queries = fail_queries
        self.fail_vids = fail_vids

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        if "ytsearch" in url:
            q = url.split(":", 1)[1]
            if q in self.fail_queries:
                raise Exception("429 Too Many Requests")
            return _flat(*self.ids)
        vid = url.rsplit("v=", 1)[-1]
        if vid in self.fail_vids:
            raise Exception("Sign in to confirm you're not a bot")
        info = INFOS.get(vid)
        if info is None:
            raise Exception("not found")
        return dict(info)


def make_factory(ids=None, fail_queries=(), fail_vids=()):
    ids = list(ids if ids is not None else INFOS)

    def factory(opts):
        return FakeYDL(opts, ids=ids,
                       fail_queries=set(fail_queries),
                       fail_vids=set(fail_vids))
    return factory


# ---------------------------------------------------------------------------
# query templates
# ---------------------------------------------------------------------------

def test_build_queries_default_templates():
    qs = build_queries("ai-news")
    assert len(qs) == len(QUERY_TEMPLATES)
    assert all("AI news" in q for q in qs)


def test_build_queries_niche_override():
    assert "gta6-breakdowns" in NICHE_QUERY_OVERRIDES
    qs = build_queries("gta6-breakdowns")
    assert any("GTA 6" in q for q in qs)


def test_build_queries_custom_niche():
    qs = build_queries("custom", custom_niche="urban gardening")
    assert all("urban gardening" in q for q in qs)


def test_build_queries_unknown_niche_raises():
    with pytest.raises(ValueError):
        build_queries("nope-not-a-niche")


# ---------------------------------------------------------------------------
# filters + ranking
# ---------------------------------------------------------------------------

def test_passes_filters():
    assert _passes_filters(INFOS["vidAAA11111"]) is None
    assert _passes_filters(INFOS["vidCCC33333"]) == "short (<60s)"
    assert _passes_filters(INFOS["vidDDD44444"]) == "age-restricted"
    assert "live/upcoming" in _passes_filters(INFOS["vidEEE55555"])
    assert "not freely watchable" in _passes_filters(INFOS["vidFFF66666"])


def test_rank_score_captions_win():
    s_cap, _ = rank_score(has_captions=True, duration=1200,
                          age_days=5, view_count=500_000)
    s_nocap, _ = rank_score(has_captions=False, duration=1200,
                            age_days=5, view_count=500_000)
    assert s_cap - s_nocap == pytest.approx(2.0)


def test_rank_score_recency_and_velocity():
    fresh, _ = rank_score(has_captions=True, duration=1200,
                          age_days=5, view_count=100_000)
    stale, _ = rank_score(has_captions=True, duration=1200,
                          age_days=400, view_count=100)
    assert fresh > stale


def test_fmt_duration():
    assert _fmt_duration(65) == "1:05"
    assert _fmt_duration(3723) == "1:02:03"
    assert _fmt_duration(None) == "?"


# ---------------------------------------------------------------------------
# discover_sources end to end (mocked)
# ---------------------------------------------------------------------------

def test_discover_filters_and_ranks():
    cands = discover_sources(["ai-news"], per_niche=5,
                             ydl_factory=make_factory(), now=NOW)
    ids = [c["video_id"] for c in cands]
    # filtered out: short, age-restricted, upcoming, premium
    assert "vidCCC33333" not in ids
    assert "vidDDD44444" not in ids
    assert "vidEEE55555" not in ids
    assert "vidFFF66666" not in ids
    # kept
    assert "vidAAA11111" in ids and "vidGGG77777" in ids
    # captions beat no-captions at equal views/recency
    assert ids.index("vidAAA11111") < ids.index("vidBBB22222")
    # massive view velocity can outweigh recency (documented heuristic)
    assert ids == sorted(ids, key=lambda v: next(
        c["score"] for c in cands if c["video_id"] == v), reverse=True)
    # honest metadata, nothing invented (top = old classic, wins on velocity)
    top = cands[0]
    assert top["url"].endswith("vidGGG77777")
    assert top["has_captions"] is True
    assert top["duration_str"] == "30:00"
    assert "has captions" in top["reasons"]


def test_discover_per_niche_limit():
    cands = discover_sources(["ai-news"], per_niche=1,
                             ydl_factory=make_factory(), now=NOW)
    assert len(cands) == 1
    assert cands[0]["video_id"] == "vidAAA11111"


def test_discover_skips_seen(tmp_path):
    seen = SeenStore(path=tmp_path / "seen.json")
    seen._data = {"vidAAA11111": {"niche": "ai-news"}}
    seen._loaded = True
    cands = discover_sources(["ai-news"], per_niche=5, seen=seen,
                             ydl_factory=make_factory(), now=NOW)
    ids = [c["video_id"] for c in cands]
    assert "vidAAA11111" not in ids
    assert "vidGGG77777" in ids


def test_discover_throttled_query_is_nonfatal():
    # two queries 429 -> we still get candidates from the remaining query
    qs = build_queries("ai-news")
    factory = make_factory(fail_queries={qs[1], qs[2]})
    cands = discover_sources(["ai-news"], per_niche=5,
                             ydl_factory=factory, now=NOW)
    assert any(c["video_id"] == "vidAAA11111" for c in cands)


def test_discover_info_failure_is_nonfatal():
    factory = make_factory(fail_vids={"vidAAA11111"})
    cands = discover_sources(["ai-news"], per_niche=5,
                             ydl_factory=factory, now=NOW)
    ids = [c["video_id"] for c in cands]
    assert "vidAAA11111" not in ids  # info fetch failed, skipped honestly
    assert "vidGGG77777" in ids


def test_discover_empty_results():
    cands = discover_sources(["ai-news"], per_niche=3,
                             ydl_factory=make_factory(ids=[]), now=NOW)
    assert cands == []


def test_discover_empty_niches():
    assert discover_sources([], ydl_factory=make_factory(), now=NOW) == []


# ---------------------------------------------------------------------------
# seen store
# ---------------------------------------------------------------------------

def test_seen_store_roundtrip(tmp_path):
    p = tmp_path / "seen.json"
    s = SeenStore(path=p)
    assert not s.is_seen("abc123")
    s.mark_used("abc123", niche="ai-news", clips_made=2)
    assert s.is_seen("abc123")
    s2 = SeenStore(path=p)
    assert s2.is_seen("abc123")
    assert s2.count() == 1
    data = json.loads(p.read_text())
    assert data["abc123"]["clips_made"] == 2
    s2.update_clips("abc123", 5)
    assert json.loads(p.read_text())["abc123"]["clips_made"] == 5


def test_seen_store_corrupt_file_is_empty(tmp_path):
    p = tmp_path / "seen.json"
    p.write_text("{not json")
    s = SeenStore(path=p)
    assert not s.is_seen("x")
    assert s.count() == 0


def test_seen_store_default_path_uses_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CF_CONFIG_DIR", str(tmp_path / ".clipforge"))
    s = SeenStore()
    assert str(s.path).endswith(".clipforge/seen.json")
