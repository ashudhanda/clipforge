"""Robustness regression tests for core/moments.

Covers concrete failure modes found in the bug-hunt: transient LLM HTTP
failures (retry), truncated/non-JSON response bodies, safety-blocked Gemini
responses (empty candidates), malformed OpenAI responses, and non-finite
transcript timestamps. All network access is mocked — no real API calls.
"""

import http.client
import io
import urllib.error

import pytest

from core.moments import detect_moments, find_moments
from core.moments.llm import (
    GeminiProvider,
    LLMError,
    LLMProvider,
    OpenAIProvider,
    _parse_json_strict,
    _post_json,
    score_moments,
)
from core.moments.scorer import group_sentences

SENT_TRANSCRIPT = [
    {"start": 0.0, "end": 9.0, "text": "I quit my job on a Tuesday."},
    {"start": 9.5, "end": 20.0, "text": "Nobody believed the numbers at first!"},
    {"start": 20.5, "end": 33.0, "text": "Then everything changed overnight and we laughed."},
]


class FakeProvider(LLMProvider):
    name = "fake"

    def __init__(self, payload=None, exc: Exception | None = None):
        self.payload = payload if payload is not None else {"clips": []}
        self.exc = exc

    def generate_json(self, system: str, user: str):
        if self.exc:
            raise self.exc
        return self.payload, {"provider": "fake", "model": "fake",
                              "est_prompt_tokens": 1}


# ------------------------------------------------- _post_json test harness


class _FakeResp:
    """Minimal urlopen context manager returning canned bytes or raising."""

    def __init__(self, body: bytes = b"{}", read_exc: Exception | None = None):
        self._body = body
        self._read_exc = read_exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        if self._read_exc:
            raise self._read_exc
        return self._body


def _http_error(code: int, body: bytes = b"error page") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://example.invalid/", code, "Err", {}, io.BytesIO(body))


def _patch_urlopen(monkeypatch, side_effects: list):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        eff = side_effects[len(calls) - 1] if len(calls) <= len(side_effects) \
            else side_effects[-1]
        if isinstance(eff, Exception):
            raise eff
        return eff

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return calls


def _patch_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(
        "core.moments.llm._sleep_before_retry",
        lambda attempt: sleeps.append(attempt),
    )
    return sleeps


# ------------------------------------------------- retry on transient failures


def test_post_json_retries_429_then_succeeds(monkeypatch):
    sleeps = _patch_sleep(monkeypatch)
    ok = _FakeResp(b'{"ok": true}')
    calls = _patch_urlopen(monkeypatch, [_http_error(429), ok])
    assert _post_json("https://x/", {}, {}) == {"ok": True}
    assert len(calls) == 2
    assert sleeps == [0]  # backed off once before the successful retry


def test_post_json_retries_503_then_succeeds(monkeypatch):
    sleeps = _patch_sleep(monkeypatch)
    ok = _FakeResp(b'{"ok": true}')
    calls = _patch_urlopen(monkeypatch, [_http_error(503), _http_error(503), ok])
    assert _post_json("https://x/", {}, {}) == {"ok": True}
    assert len(calls) == 3
    assert sleeps == [0, 1]  # 1s then 2s backoff


def test_post_json_retries_network_error_then_succeeds(monkeypatch):
    sleeps = _patch_sleep(monkeypatch)
    ok = _FakeResp(b'{"ok": true}')
    calls = _patch_urlopen(
        monkeypatch,
        [urllib.error.URLError("connection reset"), ok],
    )
    assert _post_json("https://x/", {}, {}) == {"ok": True}
    assert len(calls) == 2 and sleeps == [0]


def test_post_json_gives_up_after_three_attempts(monkeypatch):
    sleeps = _patch_sleep(monkeypatch)
    calls = _patch_urlopen(monkeypatch, [_http_error(500)])
    with pytest.raises(LLMError, match="LLM HTTP 500"):
        _post_json("https://x/", {}, {})
    assert len(calls) == 3
    assert sleeps == [0, 1]


def test_post_json_no_retry_on_401_bad_key(monkeypatch):
    sleeps = _patch_sleep(monkeypatch)
    calls = _patch_urlopen(monkeypatch, [_http_error(401, b"bad key")])
    with pytest.raises(LLMError, match="LLM HTTP 401"):
        _post_json("https://x/", {}, {})
    assert len(calls) == 1 and sleeps == []


def test_post_json_no_retry_on_400(monkeypatch):
    sleeps = _patch_sleep(monkeypatch)
    calls = _patch_urlopen(monkeypatch, [_http_error(400, b"bad request")])
    with pytest.raises(LLMError, match="LLM HTTP 400"):
        _post_json("https://x/", {}, {})
    assert len(calls) == 1 and sleeps == []


def test_post_json_404_not_retried_but_model_fallback_still_works(monkeypatch):
    """404 must bypass the retry sleep — the provider loop, not the retry
    loop, handles dead models."""
    sleeps = _patch_sleep(monkeypatch)
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
    assert len(calls) == 2 and sleeps == []


# --------------------------------------- truncated / non-JSON response bodies


def test_post_json_truncated_body_raises_llmerror_not_incompleteread(monkeypatch):
    """A connection that dies mid-body used to leak raw
    http.client.IncompleteRead — killing the job instead of triggering the
    offline fallback. Now it must surface as LLMError."""
    _patch_sleep(monkeypatch)
    calls = _patch_urlopen(
        monkeypatch,
        [_FakeResp(read_exc=http.client.IncompleteRead(b"part", 100))],
    )
    with pytest.raises(LLMError, match="network error"):
        _post_json("https://x/", {}, {})
    assert len(calls) == 3  # retried as a transient failure, then honest error


def test_post_json_non_json_200_body_raises_llmerror(monkeypatch):
    """Proxies sometimes answer 200 with an HTML error page — that used to
    leak raw json.JSONDecodeError."""
    _patch_sleep(monkeypatch)
    calls = _patch_urlopen(monkeypatch, [_FakeResp(b"<html>proxy error</html>")])
    with pytest.raises(LLMError, match="non-JSON"):
        _post_json("https://x/", {}, {})
    assert len(calls) == 1  # not retryable — the body is what it is


# ------------------------------------------------- malformed provider payloads


def test_gemini_empty_candidates_raises_llmerror(monkeypatch):
    """Safety-blocked prompts come back HTTP 200 with candidates: [] — that
    used to leak raw IndexError."""
    p = GeminiProvider(api_key="k", models=["m"])
    monkeypatch.setattr(
        "core.moments.llm._post_json",
        lambda url, payload, headers: {
            "candidates": [],
            "promptFeedback": {"blockReason": "SAFETY"},
        },
    )
    with pytest.raises(LLMError, match="no usable candidates"):
        p.generate_json("sys", "usr")


def test_gemini_malformed_response_shape_raises_llmerror(monkeypatch):
    p = GeminiProvider(api_key="k", models=["m"])
    monkeypatch.setattr(
        "core.moments.llm._post_json",
        lambda url, payload, headers: ["not", "a", "dict"],
    )
    with pytest.raises(LLMError):
        p.generate_json("sys", "usr")


def test_openai_empty_choices_raises_llmerror(monkeypatch):
    p = OpenAIProvider(api_key="k", models=["m"])
    monkeypatch.setattr(
        "core.moments.llm._post_json",
        lambda url, payload, headers: {"choices": []},
    )
    with pytest.raises(LLMError, match="no usable choices"):
        p.generate_json("sys", "usr")


def test_openai_null_content_raises_llmerror(monkeypatch):
    """content: null (filtered) used to leak AttributeError from
    _parse_json_strict(None)."""
    p = OpenAIProvider(api_key="k", models=["m"])
    monkeypatch.setattr(
        "core.moments.llm._post_json",
        lambda url, payload, headers: {
            "choices": [{"message": {"content": None}}]},
    )
    with pytest.raises(LLMError):
        p.generate_json("sys", "usr")


def test_parse_json_strict_rejects_non_string():
    with pytest.raises(LLMError):
        _parse_json_strict(None)


# ------------------------------------------------- non-finite transcript times


def test_group_sentences_drops_nan_and_inf_timestamps():
    tr = [
        {"start": float("nan"), "end": 5.0, "text": "Broken time."},
        {"start": 0.0, "end": float("inf"), "text": "Endless time."},
        {"start": 0.0, "end": 9.0, "text": "I quit my job on a Tuesday."},
        {"start": float("-inf"), "end": 3.0, "text": "Negative forever."},
    ]
    sents = group_sentences(tr)
    assert [s.text for s in sents] == ["I quit my job on a Tuesday."]


def test_find_moments_nan_only_transcript_returns_empty_no_crash():
    """All-NaN transcript used to crash in round(nan) -> ValueError."""
    tr = [{"start": float("nan"), "end": float("nan"), "text": "Broken."}]
    clips, usage = find_moments(tr, provider=FakeProvider())
    assert clips == []


def test_find_moments_mixed_nan_and_valid():
    tr = [
        {"start": float("nan"), "end": 5.0, "text": "Broken."},
        {"start": 0.0, "end": 20.0, "text": "A perfectly good sentence here."},
    ]
    payload = {"clips": [{"start": 0.0, "end": 20.0, "title": "T",
                          "hook_line": "H", "reason": "R", "score": 70}]}
    clips, _ = find_moments(tr, provider=FakeProvider(payload), min_clip_sec=8.0)
    assert len(clips) == 1 and clips[0].title == "T"


# ------------------------------------------------- end-to-end failure contract


def test_detect_moments_safety_block_surfaces_as_llmerror(monkeypatch):
    """The app catches only LLMError to fall back to offline mode — a
    safety-blocked Gemini response must arrive as LLMError, never IndexError."""
    monkeypatch.setattr(
        "core.moments.llm._post_json",
        lambda url, payload, headers: {"candidates": []},
    )
    provider = GeminiProvider(api_key="k", models=["m"])
    with pytest.raises(LLMError):
        detect_moments(SENT_TRANSCRIPT, mode="llm", provider=provider)


def test_score_moments_empty_clips_list_is_honest_empty():
    """LLM returning zero clips is a valid 'no good moments' answer, not an
    error and not fabricated clips."""
    clips, usage = score_moments("text", "rubric", 1,
                                 provider=FakeProvider({"clips": []}))
    assert clips == []
    assert usage["provider"] == "fake"


def test_group_sentences_hindi_danda_splits():
    """Non-English transcripts work: Devanagari danda ends sentences."""
    tr = [{"start": 0.0, "end": 5.0, "text": "पहला वाक्य यहाँ है।"},
          {"start": 5.0, "end": 10.0, "text": "दूसरा वाक्य अब आता है।"}]
    sents = group_sentences(tr)
    assert len(sents) == 2
    assert sents[0].text == "पहला वाक्य यहाँ है।"
