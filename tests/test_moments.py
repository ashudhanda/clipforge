"""Phase 2 moment-detection tests. The LLM is mocked everywhere — no real API
calls. Fixture transcripts are synthetic."""

import pytest

from core.moments import (
    Clip,
    LLMError,
    dedupe_clips,
    detect_moments,
    find_moments,
    get_provider,
    segment_offline,
)
from core.moments.llm import (
    GeminiProvider,
    OpenAIProvider,
    _parse_json_strict,
    estimate_tokens,
    score_moments,
)
from core.moments.llm import LLMProvider
from core.moments.scorer import (
    build_rubric,
    format_transcript,
    group_sentences,
    snap_to_sentences,
)


# ------------------------------------------------------------- fake provider


class FakeProvider(LLMProvider):
    name = "fake"

    def __init__(self, payload=None, exc: Exception | None = None):
        self.payload = payload if payload is not None else {"clips": []}
        self.exc = exc
        self.calls: list[tuple[str, str]] = []

    def generate_json(self, system: str, user: str):
        self.calls.append((system, user))
        if self.exc:
            raise self.exc
        return self.payload, {"provider": "fake", "model": "fake",
                              "est_prompt_tokens": 1}


WORD_TRANSCRIPT = [
    # word-level items that form 3 sentences: 0-9s, 9.5-20s, 20.5-33s
    {"start": 0.0, "end": 1.2, "text": "I"},
    {"start": 1.2, "end": 2.4, "text": "quit"},
    {"start": 2.4, "end": 3.6, "text": "my"},
    {"start": 3.6, "end": 5.0, "text": "job"},
    {"start": 5.0, "end": 6.5, "text": "on"},
    {"start": 6.5, "end": 8.0, "text": "a"},
    {"start": 8.0, "end": 9.0, "text": "Tuesday."},
    {"start": 9.5, "end": 11.0, "text": "Nobody"},
    {"start": 11.0, "end": 12.5, "text": "believed"},
    {"start": 12.5, "end": 14.0, "text": "the"},
    {"start": 14.0, "end": 16.0, "text": "numbers"},
    {"start": 16.0, "end": 18.0, "text": "at"},
    {"start": 18.0, "end": 20.0, "text": "first!"},
    {"start": 20.5, "end": 22.0, "text": "Then"},
    {"start": 22.0, "end": 24.0, "text": "everything"},
    {"start": 24.0, "end": 26.0, "text": "changed"},
    {"start": 26.0, "end": 28.0, "text": "overnight"},
    {"start": 28.0, "end": 30.0, "text": "and"},
    {"start": 30.0, "end": 31.5, "text": "we"},
    {"start": 31.5, "end": 33.0, "text": "laughed."},
]

SENT_TRANSCRIPT = [
    {"start": 0.0, "end": 9.0, "text": "I quit my job on a Tuesday."},
    {"start": 9.5, "end": 20.0, "text": "Nobody believed the numbers at first!"},
    {"start": 20.5, "end": 33.0, "text": "Then everything changed overnight and we laughed."},
]


def _two_topic_transcript(n_each: int = 12) -> list[dict]:
    """Two clearly separated topics -> TextTiling should find the boundary."""
    cats = [
        "The cat slept on the warm windowsill all afternoon.",
        "Kittens love chasing feathers across the living room rug.",
        "My tabby knocked the vase off the kitchen table again.",
        "The veterinarian said the kitten needs more wet food daily.",
    ]
    quant = [
        "Quantum entanglement links particles across vast distances instantly.",
        "The superposition principle defies classical intuition completely.",
        "Decoherence collapses the wavefunction into definite states.",
        "Physicists measure qubits with superconducting circuits today.",
    ]
    out, t = [], 0.0
    for i in range(n_each):
        out.append({"start": t, "end": t + 4.0, "text": cats[i % len(cats)]})
        t += 5.0
    for i in range(n_each):
        out.append({"start": t, "end": t + 4.0, "text": quant[i % len(quant)]})
        t += 5.0
    return out


# ------------------------------------------------------------------ llm.py


def test_get_provider_no_keys_raises(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CF2_LLM_PROVIDER", raising=False)
    with pytest.raises(LLMError):
        get_provider()


def test_get_provider_prefers_gemini(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k1")
    monkeypatch.setenv("OPENAI_API_KEY", "k2")
    monkeypatch.delenv("CF2_LLM_PROVIDER", raising=False)
    assert isinstance(get_provider(), GeminiProvider)


def test_get_provider_openai_only(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "k2")
    assert isinstance(get_provider(), OpenAIProvider)


def test_gemini_model_fallback_on_404(monkeypatch):
    p = GeminiProvider(api_key="k", models=["dead-model", "live-model"])
    calls = []

    def fake_post(url, payload, headers):
        calls.append(url)
        if "dead-model" in url:
            raise LLMError("LLM HTTP 404: model not found")
        return {"candidates": [{"content": {"parts": [{"text": '{"clips": []}'}]}}]}

    monkeypatch.setattr("core.moments.llm._post_json", fake_post)
    parsed, usage = p.generate_json("sys", "usr")
    assert parsed == {"clips": []}
    assert usage["model"] == "live-model"
    assert len(calls) == 2


def test_parse_json_strict_rejects_garbage():
    with pytest.raises(LLMError):
        _parse_json_strict("Sure! Here are some clips: ...")


def test_score_moments_returns_clips_list():
    payload = {"clips": [{"start": 1, "end": 9, "title": "T",
                         "hook_line": "H", "reason": "R", "score": 80}]}
    clips, usage = score_moments("text", "rubric", 1, provider=FakeProvider(payload))
    assert clips[0]["score"] == 80
    assert usage["provider"] == "fake"


def test_score_moments_rejects_missing_clips_key():
    with pytest.raises(LLMError):
        score_moments("text", "rubric", 1, provider=FakeProvider({"nope": 1}))


def test_estimate_tokens_positive():
    assert estimate_tokens("abcd" * 100) > 50


# ---------------------------------------------------------------- scorer.py


def test_rubric_covers_five_signals():
    r = build_rubric().lower()
    for kw in ("hook", "emotion", "useful", "arc", "discussion"):
        assert kw in r


def test_group_sentences_word_level():
    sents = group_sentences(WORD_TRANSCRIPT)
    assert len(sents) == 3
    assert sents[0].start == pytest.approx(0.0)
    assert sents[0].end == pytest.approx(9.0)
    assert sents[0].text == "I quit my job on a Tuesday."
    assert sents[2].text.endswith("laughed.")


def test_group_sentences_passthrough():
    sents = group_sentences(SENT_TRANSCRIPT)
    assert len(sents) == 3
    assert [s.text for s in sents] == [d["text"] for d in SENT_TRANSCRIPT]


def test_group_sentences_splits_multi_sentence_item():
    tr = [{"start": 0.0, "end": 10.0, "text": "First thought here. Second thought now!"}]
    sents = group_sentences(tr)
    assert len(sents) == 2
    assert sents[0].end <= sents[1].start
    assert sents[1].start >= 0.0


def test_snap_to_sentences_mid_sentence():
    sents = group_sentences(SENT_TRANSCRIPT)
    snapped = snap_to_sentences(2.5, 25.0, sents)  # inside sent 1 .. inside sent 3
    assert snapped == (0.0, 33.0)


def test_snap_to_sentences_invalid():
    sents = group_sentences(SENT_TRANSCRIPT)
    assert snap_to_sentences(40.0, 50.0, sents) is None
    assert snap_to_sentences(5.0, 2.0, sents) is None


def _cand(**kw):
    base = {"start": 0.0, "end": 20.0, "title": "T", "hook_line": "H",
            "reason": "R", "score": 70}
    base.update(kw)
    return base


def test_find_moments_snaps_and_sorts():
    payload = {"clips": [
        _cand(start=2.5, end=25.0, score=60, title="Mid"),   # snaps to 0-33
        _cand(start=9.5, end=20.0, score=90, title="Exact"),
        _cand(start=0.0, end=9.0, score=150, title="Clamped"),  # -> 100
        _cand(start=9.1, end=9.4, score=80, title="TooShort"),   # gap -> invalid snap
        {"start": 0.0, "title": "Broken"},                       # dropped
    ]}
    clips, _ = find_moments(SENT_TRANSCRIPT, provider=FakeProvider(payload),
                            min_clip_sec=8.0, max_clip_sec=100.0)
    assert [c.title for c in clips] == ["Clamped", "Exact", "Mid"]
    assert clips[0].score == 100 and clips[1].score == 90  # clamped, then sorted
    assert clips[2].start == pytest.approx(0.0)
    assert clips[2].end == pytest.approx(33.0)
    assert all(c.source == "llm" for c in clips)


def test_find_moments_drops_too_short():
    tr = [{"start": 0.0, "end": 3.0, "text": "Hi there."},
          {"start": 5.0, "end": 40.0,
           "text": "A much longer second sentence with real content here."}]
    payload = {"clips": [_cand(start=0.0, end=3.0, score=90, title="Tiny"),
                         _cand(start=5.0, end=40.0, score=70, title="Big")]}
    clips, _ = find_moments(tr, provider=FakeProvider(payload), min_clip_sec=8.0)
    assert [c.title for c in clips] == ["Big"]


LONG_TRANSCRIPT = [
    {"start": i * 12.0, "end": (i + 1) * 12.0 - 0.5,
     "text": f"Sentence number {i} with enough words to look real."}
    for i in range(12)
]  # 12 x ~11.5s = ~138s


def test_find_moments_drops_overlong():
    payload = {"clips": [
        _cand(start=0.0, end=130.0, score=95, title="Overlong"),  # snapped ~138s > 100
        _cand(start=0.0, end=36.0, score=70, title="Fine"),
    ]}
    clips, _ = find_moments(LONG_TRANSCRIPT, provider=FakeProvider(payload),
                            min_clip_sec=8.0, max_clip_sec=100.0)
    assert [c.title for c in clips] == ["Fine"]


def test_find_moments_llm_failure_raises_not_fakes():
    with pytest.raises(LLMError):
        find_moments(SENT_TRANSCRIPT,
                     provider=FakeProvider(exc=LLMError("boom")))


def test_find_moments_empty_transcript():
    clips, usage = find_moments([], provider=FakeProvider())
    assert clips == []


def test_format_transcript_numbered():
    sents = group_sentences(SENT_TRANSCRIPT)
    txt = format_transcript(sents)
    assert "[0] [0.0-9.0]" in txt and "[2] [20.5-33.0]" in txt


# ---------------------------------------------------------------- dedupe.py


def _clip(s, e, score):
    return Clip(start=s, end=e, score=score, title="t",
                hook_line="h", reason="r")


def test_dedupe_drops_high_overlap():
    # 13s overlap on the 30s shorter clip = 43% > 40% -> lower-scored one goes
    clips = [_clip(0, 30, 90), _clip(17, 47, 70)]
    kept = dedupe_clips(clips)
    assert [c.score for c in kept] == [90]


def test_dedupe_keeps_low_overlap():
    # 10s overlap on 30s shorter = 33% -> both kept
    clips = [_clip(0, 30, 90), _clip(20, 50, 70)]
    kept = dedupe_clips([clips[0], _clip(27, 57, 70)])
    assert len(kept) == 2


def test_dedupe_sorts_desc():
    kept = dedupe_clips([_clip(0, 10, 50), _clip(100, 110, 80)])
    assert [c.score for c in kept] == [80, 50]


# --------------------------------------------------------------- segmenter.py


def test_segment_offline_finds_topic_boundary():
    clips = segment_offline(_two_topic_transcript(), min_clip_sec=10.0)
    assert len(clips) >= 2
    # A boundary should sit near the topic switch (~60s mark)
    bounds = sorted({c.start for c in clips[1:]} | {c.end for c in clips[:-1]})
    assert any(abs(b - 60.0) < 12.0 for b in bounds), bounds
    assert all(0 <= c.score <= 100 for c in clips)
    assert all(c.source == "offline" for c in clips)


def test_segment_offline_deterministic():
    tr = _two_topic_transcript()
    a = [(c.start, c.end, c.score) for c in segment_offline(tr)]
    b = [(c.start, c.end, c.score) for c in segment_offline(tr)]
    assert a == b


def test_segment_offline_too_short():
    tr = [{"start": i * 5.0, "end": i * 5.0 + 4.0, "text": "Short bit here."}
          for i in range(3)]
    assert segment_offline(tr) == []


def test_segment_offline_no_sklearn(monkeypatch):
    import core.moments.segmenter as seg
    monkeypatch.setattr(seg, "_SKLEARN_OK", False)
    with pytest.raises(RuntimeError):
        seg.segment_offline(_two_topic_transcript())


# ------------------------------------------------------------------ __init__


def test_detect_moments_llm_mode():
    payload = {"clips": [_cand(start=0.0, end=20.0, score=75)]}
    clips = detect_moments(SENT_TRANSCRIPT, mode="llm",
                           provider=FakeProvider(payload))
    assert len(clips) == 1 and clips[0].score == 75


def test_detect_moments_offline_mode():
    clips = detect_moments(_two_topic_transcript(), mode="offline",
                           min_clip_sec=8.0)
    assert len(clips) >= 2


def test_detect_moments_bad_mode():
    with pytest.raises(ValueError):
        detect_moments(SENT_TRANSCRIPT, mode="nope")


def test_detect_moments_empty_raises():
    with pytest.raises(ValueError):
        detect_moments([])
