"""Phase 4 metadata tests. The LLM is mocked everywhere — no real API calls."""

import pytest

from core.metadata import (
    ensure_unique_titles,
    generate_for_clips,
    generate_metadata,
    transcript_to_text,
    validate_metadata,
)
from core.moments.llm import LLMError


class FakeProvider:
    """Duck-typed LLM provider replaying canned responses."""

    name = "fake"

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def generate_json(self, system, user):
        self.calls.append((system, user))
        if not self._responses:
            raise AssertionError("FakeProvider ran out of responses")
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp, {
            "provider": "fake",
            "model": "fake-model",
            "est_prompt_tokens": 42,
        }


TRANSCRIPT = (
    "Today I want to talk about the 50/30/20 budget rule. "
    "Fifty percent of your income goes to needs like rent and groceries. "
    "Thirty percent goes to wants, the fun stuff. "
    "And twenty percent goes straight to savings. "
    "It is the simplest budget that actually works for most people."
)

VALID_META = {
    "title": "The 50/30/20 Budget Rule Explained",
    "description": (
        "He breaks down the 50/30/20 rule for splitting your income.\n"
        "Needs, wants and savings finally make sense in plain words.\n"
        "Have you ever tried budgeting like this?\n"
        "#budgeting #personalfinance #moneytips"
    ),
    "hashtags": ["Budgeting", "#PersonalFinance", "moneytips", "moneytips"],
    "keywords_used": ["50/30/20", "budget"],
}


def make_provider(*responses):
    return FakeProvider(list(responses))


# --- transcript normalization ------------------------------------------------


def test_transcript_to_text_from_word_list():
    words = [
        {"start": 0.0, "end": 0.5, "text": "hello"},
        {"start": 0.5, "end": 1.0, "text": "world"},
    ]
    assert transcript_to_text(words) == "hello world"


def test_transcript_to_text_from_string():
    assert transcript_to_text("  hello world  ") == "hello world"


def test_transcript_to_text_empty_raises():
    with pytest.raises(ValueError):
        transcript_to_text([])
    with pytest.raises(ValueError):
        transcript_to_text("   ")
    with pytest.raises(ValueError):
        transcript_to_text(123)


# --- happy path --------------------------------------------------------------


def test_valid_metadata_passes_and_normalizes():
    meta, usage = generate_metadata(
        TRANSCRIPT, "personal finance", provider=make_provider(VALID_META)
    )
    assert meta["title"] == "The 50/30/20 Budget Rule Explained"
    assert len(meta["title"]) <= 60
    assert "#" not in meta["title"]
    # hashtags lowercased, deduped, banned-free
    assert meta["hashtags"] == ["budgeting", "personalfinance", "moneytips"]
    # description keeps body, question, and canonical hashtag line at the end
    assert "?" in meta["description"]
    assert meta["description"].endswith("#budgeting #personalfinance #moneytips")
    assert usage["est_prompt_tokens"] == 42


def test_accepts_word_list_transcript():
    words = [{"start": i, "end": i + 1, "text": w} for i, w in enumerate(TRANSCRIPT.split())]
    meta, _ = generate_metadata(words, "personal finance", provider=make_provider(VALID_META))
    assert meta["title"].startswith("The 50/30/20")


def test_style_hints_reach_prompt():
    provider = make_provider(VALID_META)
    generate_metadata(
        TRANSCRIPT, "finance",
        style_hints={"tone": "bold", "language": "Hindi"},
        provider=provider,
    )
    _system, user = provider.calls[0]
    assert "bold" in user and "Hindi" in user


# --- validation failures -----------------------------------------------------


def test_long_title_retried_then_raises():
    bad = dict(VALID_META, title="x" * 70)
    provider = make_provider(bad, bad)
    with pytest.raises(LLMError):
        generate_metadata(TRANSCRIPT, "finance", provider=provider)
    assert len(provider.calls) == 2  # one retry, then honest failure


def test_invalid_metadata_retried_then_succeeds():
    bad = dict(VALID_META, hashtags=["viral", "#fyp"])  # both banned -> 0 valid
    provider = make_provider(bad, VALID_META)
    meta, _ = generate_metadata(TRANSCRIPT, "finance", provider=provider)
    assert meta["hashtags"] == ["budgeting", "personalfinance", "moneytips"]
    assert len(provider.calls) == 2


def test_hashtags_in_title_are_stripped():
    meta = dict(VALID_META, title="Great Budget Tips #money #viral")
    result, _ = generate_metadata(TRANSCRIPT, "finance", provider=make_provider(meta))
    assert result["title"] == "Great Budget Tips"
    assert "#" not in result["title"]


def test_invented_keyword_rejected():
    bad = dict(VALID_META, keywords_used=["unicorn startup IPO"])
    provider = make_provider(bad, bad)
    with pytest.raises(LLMError, match="not found verbatim"):
        generate_metadata(TRANSCRIPT, "finance", provider=provider)


def test_description_without_question_rejected():
    bad = dict(
        VALID_META,
        description="Line one here.\nLine two here.\nLine three here.\n#budgeting #personalfinance #moneytips",
    )
    provider = make_provider(bad, bad)
    with pytest.raises(LLMError, match="question"):
        generate_metadata(TRANSCRIPT, "finance", provider=provider)


def test_description_line_count_enforced():
    one_line = dict(
        VALID_META,
        description="Only one line?\n#budgeting #personalfinance #moneytips",
    )
    provider = make_provider(one_line, one_line)
    with pytest.raises(LLMError, match="content lines"):
        generate_metadata(TRANSCRIPT, "finance", provider=provider)


def test_llm_transport_failure_propagates_never_fabricated():
    provider = make_provider(LLMError("network down"))
    with pytest.raises(LLMError, match="network down"):
        generate_metadata(TRANSCRIPT, "finance", provider=provider)
    # no fabricated metadata, no retry on transport failure
    assert len(provider.calls) == 1


def test_bad_inputs_raise_valueerror():
    provider = make_provider(VALID_META)
    with pytest.raises(ValueError):
        generate_metadata("", "finance", provider=provider)
    with pytest.raises(ValueError):
        generate_metadata(TRANSCRIPT, "", provider=provider)
    with pytest.raises(ValueError):
        generate_metadata(TRANSCRIPT, "   ", provider=provider)


def test_validate_metadata_rejects_non_dict():
    with pytest.raises(LLMError):
        validate_metadata("not a dict", TRANSCRIPT)


# --- uniqueness ----------------------------------------------------------------


def _item(title, transcript=TRANSCRIPT):
    return {
        "metadata": {
            "title": title,
            "description": "A line.\nAnother line?\n#aaa #bbb #ccc",
            "hashtags": ["aaa", "bbb", "ccc"],
        },
        "transcript": transcript,
    }


def test_unique_titles_untouched():
    items = [_item("Alpha Title"), _item("Beta Title")]
    out = ensure_unique_titles(items, "finance", provider=make_provider())
    assert [i["metadata"]["title"] for i in out] == ["Alpha Title", "Beta Title"]


def test_duplicate_title_reworded_via_llm():
    reword = {
        "title": "Budget Rule: A Fresh Take",
        "description": "x",
        "hashtags": ["a", "b", "c"],
        "keywords_used": [],
    }
    items = [_item("Same Title"), _item("Same Title")]
    provider = make_provider(reword)
    out = ensure_unique_titles(items, "finance", provider=provider)
    titles = [i["metadata"]["title"] for i in out]
    assert titles[0] == "Same Title"
    assert titles[1] == "Budget Rule: A Fresh Take"
    assert len(set(t.lower() for t in titles)) == 2


def test_duplicate_case_insensitive():
    items = [_item("Same Title"), _item("same title")]
    provider = make_provider(LLMError("nope"), LLMError("nope"))
    out = ensure_unique_titles(items, "finance", provider=provider)
    titles = [i["metadata"]["title"] for i in out]
    assert len(set(t.lower() for t in titles)) == 2


def test_fallback_when_llm_reword_fails():
    # LLM keeps failing -> deterministic fallback from transcript keywords.
    items = [_item("Same Title"), _item("Same Title"), _item("Same Title")]
    provider = make_provider(*[LLMError("down")] * 10)
    out = ensure_unique_titles(items, "finance", provider=provider)
    titles = [i["metadata"]["title"] for i in out]
    assert titles[0] == "Same Title"
    assert len(set(t.lower() for t in titles)) == 3
    for t in titles[1:]:
        assert len(t) <= 60
        assert "(2)" not in t and "#" not in t
        # fallback keyword really comes from the transcript
        assert t.split()[0].strip(":").lower() in TRANSCRIPT.lower()


def test_fallback_is_deterministic():
    items = [_item("Same Title"), _item("Same Title")]
    p1 = make_provider(LLMError("x"), LLMError("x"))
    p2 = make_provider(LLMError("x"), LLMError("x"))
    out1 = ensure_unique_titles(items, "finance", provider=p1)
    out2 = ensure_unique_titles(items, "finance", provider=p2)
    assert out1[1]["metadata"]["title"] == out2[1]["metadata"]["title"]


def test_fallback_impossible_raises_not_duplicates():
    # Transcript with no usable keywords -> honest raise, never duplicates.
    items = [_item("Same Title", transcript="the and of to"),
             _item("Same Title", transcript="the and of to")]
    provider = make_provider(LLMError("x"), LLMError("x"))
    with pytest.raises(LLMError):
        ensure_unique_titles(items, "finance", provider=provider)


def test_uniqueness_does_not_mutate_inputs():
    items = [_item("Same Title"), _item("Same Title")]
    provider = make_provider(LLMError("x"), LLMError("x"))
    ensure_unique_titles(items, "finance", provider=provider)
    assert items[0]["metadata"]["title"] == "Same Title"
    assert items[1]["metadata"]["title"] == "Same Title"


def test_uniqueness_bad_input():
    with pytest.raises(ValueError):
        ensure_unique_titles([], "finance", provider=make_provider())


# --- generate_for_clips --------------------------------------------------------


def test_generate_for_clips_end_to_end():
    clips = [
        {"transcript": TRANSCRIPT, "start": 0, "end": 30},
        {"transcript": TRANSCRIPT, "start": 40, "end": 70},
    ]
    # both clips get the same title from the mock -> uniqueness must fix it
    provider = make_provider(VALID_META, VALID_META,
                             {"title": "Another Angle on Budgeting"})
    results = generate_for_clips(clips, "personal finance", provider=provider)
    assert len(results) == 2
    assert results[0]["clip"] is clips[0]
    assert results[1]["clip"] is clips[1]
    titles = [r["metadata"]["title"] for r in results]
    assert len(set(t.lower() for t in titles)) == 2
    for r in results:
        assert r["metadata"]["hashtags"]
        assert r["usage"]["provider"] == "fake"


def test_generate_for_clips_object_with_transcript_attr():
    class C:
        def __init__(self, t):
            self.transcript = t

    provider = make_provider(VALID_META)
    results = generate_for_clips([C(TRANSCRIPT)], "finance", provider=provider)
    assert results[0]["metadata"]["title"] == "The 50/30/20 Budget Rule Explained"


def test_generate_for_clips_missing_transcript_raises():
    with pytest.raises(ValueError, match="no transcript"):
        generate_for_clips([{"start": 0}], "finance", provider=make_provider())


def test_generate_for_clips_empty_raises():
    with pytest.raises(ValueError):
        generate_for_clips([], "finance", provider=make_provider())


def test_generate_for_clips_does_not_mutate_clips():
    clips = [{"transcript": TRANSCRIPT}]
    provider = make_provider(VALID_META)
    generate_for_clips(clips, "finance", provider=provider)
    assert clips[0] == {"transcript": TRANSCRIPT}
