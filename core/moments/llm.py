"""Pluggable LLM provider abstraction for ClipForge.

Design notes (logic adapted from the Phase 0 audit of cutawan's highlights
pipeline — rewritten in Python, our own code):
- Providers are selected at runtime from environment keys; model names live in
  small config lists (overridable via env) and are NEVER hardcoded in logic.
- ``score_moments`` is the single high-level entry: give it transcript text, a
  rubric and a target clip count, get parsed JSON back.
- Failures are honest: LLM/network/parse errors raise LLMError. We never
  invent scores, timestamps or clips.
- HTTP is done with stdlib urllib only — no SDK dependency to go stale.
"""

from __future__ import annotations

import abc
import http.client
import json
import logging
import os
import time
import urllib.error
import urllib.request

log = logging.getLogger("clipforge.moments.llm")


class LLMError(RuntimeError):
    """Raised when an LLM call fails. Never silently substituted with fake data."""


# --- Model config lists (NOT hardcoded in logic) ---------------------------
# Order = preference order. Override with CF_GEMINI_MODEL / CF_OPENAI_MODEL
# to pin a single model. On a 404 "model not found" the provider falls through
# to the next entry, so renamed/retired models degrade gracefully instead of
# crashing (lesson from our own chrome-136 hardcoding bug).
DEFAULT_GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
]
DEFAULT_OPENAI_MODELS = [
    "gpt-4o-mini",
    "gpt-4.1-mini",
]

HTTP_TIMEOUT_S = 120

# --- Retry policy for transient LLM failures --------------------------------
# Scoring requests are idempotent (same prompt -> same ask), so a failed
# attempt can simply be repeated. Retried: 408/429/5xx and network-level
# errors (reset connections, truncated bodies, DNS blips). Never retried:
# 400 (bad request), 401/403 (bad key — retrying won't help), 404 (model
# gone — the per-provider model-fallback loop handles that itself).
_RETRYABLE_HTTP_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_S = (1.0, 2.0)  # sleeps before attempt 2 and 3


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token). Used for the cost guard log."""
    return max(1, len(text) // 4)


def _sleep_before_retry(attempt: int) -> None:
    time.sleep(_RETRY_BACKOFF_S[min(attempt, len(_RETRY_BACKOFF_S) - 1)])


def _post_json(url: str, payload: dict, headers: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    for attempt in range(_MAX_ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:500]
            err = LLMError(f"LLM HTTP {e.code}: {body}")
            if e.code in _RETRYABLE_HTTP_STATUS and attempt < _MAX_ATTEMPTS - 1:
                log.warning(
                    "LLM HTTP %s (attempt %d/%d) — retrying",
                    e.code, attempt + 1, _MAX_ATTEMPTS,
                )
                _sleep_before_retry(attempt)
                continue
            raise err from e
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,  # e.g. IncompleteRead on truncated body
        ) as e:
            if attempt < _MAX_ATTEMPTS - 1:
                log.warning(
                    "LLM network error (attempt %d/%d): %s — retrying",
                    attempt + 1, _MAX_ATTEMPTS, e,
                )
                _sleep_before_retry(attempt)
                continue
            raise LLMError(f"LLM network error: {e}") from e
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise LLMError(f"LLM returned a non-UTF8 response body: {e}") from e
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(
                f"LLM returned a non-JSON response body (first 120 chars: "
                f"{text[:120]!r}): {e}"
            ) from e
    # Unreachable: the loop always returns or raises.
    raise LLMError(f"LLM request failed after {_MAX_ATTEMPTS} attempts")


class LLMProvider(abc.ABC):
    """Abstract chat-completion provider returning parsed JSON."""

    name: str = "base"

    @abc.abstractmethod
    def generate_json(self, system: str, user: str) -> tuple[object, dict]:
        """Return (parsed_json, usage_dict). Raise LLMError on any failure."""
        raise NotImplementedError


def _gemini_text(resp: object) -> str:
    """Pull the text out of a Gemini generateContent response.

    Raises LLMError (never IndexError/KeyError/TypeError) so a
    safety-blocked prompt — which comes back HTTP 200 with an empty
    ``candidates`` list — degrades to the honest offline fallback in the
    caller instead of crashing the job.
    """
    try:
        text = resp["candidates"][0]["content"]["parts"][0]["text"]  # type: ignore[index]
    except (KeyError, IndexError, TypeError) as e:
        feedback = ""
        if isinstance(resp, dict):
            feedback = f" promptFeedback={json.dumps(resp.get('promptFeedback'))[:200]}"
        raise LLMError(
            f"Gemini returned no usable candidates (blocked or empty).{feedback}"
        ) from e
    if not isinstance(text, str) or not text.strip():
        raise LLMError("Gemini returned an empty text part")
    return text


def _openai_text(resp: object) -> str:
    """Pull the text out of an OpenAI chat-completion response.

    Raises LLMError (never IndexError/AttributeError) on empty choices or
    null content so callers keep their honest-failure contract.
    """
    try:
        text = resp["choices"][0]["message"]["content"]  # type: ignore[index]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError("OpenAI returned no usable choices") from e
    if not isinstance(text, str) or not text.strip():
        raise LLMError("OpenAI returned empty content (possibly filtered)")
    return text


class GeminiProvider(LLMProvider):
    """Google Gemini via the REST generateContent endpoint (v1beta)."""

    name = "gemini"

    def __init__(self, api_key: str | None = None, models: list[str] | None = None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY is not set")
        pinned = os.environ.get("CF_GEMINI_MODEL")
        self.models = [pinned] if pinned else list(models or DEFAULT_GEMINI_MODELS)

    def generate_json(self, system: str, user: str) -> tuple[object, dict]:
        payload = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"parts": [{"text": user}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        headers = {"Content-Type": "application/json"}
        last_err: Exception | None = None
        for model in self.models:
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:generateContent?key={self.api_key}"
            )
            try:
                resp = _post_json(url, payload, headers)
                text = _gemini_text(resp)
                return _parse_json_strict(text), {
                    "provider": self.name,
                    "model": model,
                    "est_prompt_tokens": estimate_tokens(system + user),
                }
            except LLMError as e:
                # Model renamed/retired -> fall through to the next candidate.
                if "404" in str(e) and len(self.models) > 1:
                    log.warning("gemini model %s unavailable, trying next", model)
                    last_err = e
                    continue
                raise
        raise LLMError(f"all gemini models failed; last error: {last_err}")

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"GeminiProvider(models={self.models})"


class OpenAIProvider(LLMProvider):
    """OpenAI chat completions with JSON response format."""

    name = "openai"

    def __init__(self, api_key: str | None = None, models: list[str] | None = None):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not self.api_key:
            raise LLMError("OPENAI_API_KEY is not set")
        pinned = os.environ.get("CF_OPENAI_MODEL")
        self.models = [pinned] if pinned else list(models or DEFAULT_OPENAI_MODELS)

    def generate_json(self, system: str, user: str) -> tuple[object, dict]:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        }
        last_err: Exception | None = None
        for model in self.models:
            try:
                resp = _post_json(
                    "https://api.openai.com/v1/chat/completions",
                    {**payload, "model": model},
                    headers,
                )
                text = _openai_text(resp)
                return _parse_json_strict(text), {
                    "provider": self.name,
                    "model": model,
                    "est_prompt_tokens": estimate_tokens(system + user),
                }
            except LLMError as e:
                if "404" in str(e) and len(self.models) > 1:
                    log.warning("openai model %s unavailable, trying next", model)
                    last_err = e
                    continue
                raise
        raise LLMError(f"all openai models failed; last error: {last_err}")

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"OpenAIProvider(models={self.models})"


def _parse_json_strict(text: str) -> object:
    """Parse model output as JSON; raise LLMError instead of guessing."""
    if not isinstance(text, str):
        raise LLMError(f"LLM did not return text (got {type(text).__name__})")
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Tolerate a single fenced block, but nothing more creative.
    if text.startswith("```"):
        inner = text.split("```")[1]
        inner = inner[inner.find("\n") + 1 :] if "\n" in inner else inner
        try:
            return json.loads(inner.strip())
        except json.JSONDecodeError:
            pass
    raise LLMError(f"LLM did not return valid JSON (first 200 chars: {text[:200]!r})")


def get_provider(prefer: str | None = None) -> LLMProvider:
    """Pick a provider from available API keys. Raise LLMError if none.

    Key lookup order: environment variables first (``GEMINI_API_KEY`` /
    ``OPENAI_API_KEY`` / ``CF_LLM_PROVIDER``), then the dashboard-saved keys
    in ``~/.clipforge/llm_keys.json``. Explicit env always wins.
    """
    from . import llm_keys

    stored = llm_keys.load_keys()
    prefer = (prefer or os.environ.get("CF_LLM_PROVIDER", "")).lower()
    if not prefer and stored["provider"] != "auto":
        prefer = stored["provider"]
    gemini_key = os.environ.get("GEMINI_API_KEY", "") or stored["gemini_key"]
    openai_key = os.environ.get("OPENAI_API_KEY", "") or stored["openai_key"]
    # Dashboard model pick: the chosen model goes first, provider defaults
    # stay as fallbacks. Explicit CF_*_MODEL env pins still win inside the
    # provider constructors.
    pin = (stored.get("model") or "").strip() or None

    def _with_pin(defaults: list[str]) -> list[str]:
        if pin and pin in defaults:
            return [pin] + [m for m in defaults if m != pin]
        return list(defaults)

    if prefer == "openai":
        return OpenAIProvider(openai_key or None,
                              models=_with_pin(DEFAULT_OPENAI_MODELS))
    if prefer == "gemini":
        return GeminiProvider(gemini_key or None,
                              models=_with_pin(DEFAULT_GEMINI_MODELS))
    if gemini_key:
        return GeminiProvider(gemini_key, models=_with_pin(DEFAULT_GEMINI_MODELS))
    if openai_key:
        return OpenAIProvider(openai_key, models=_with_pin(DEFAULT_OPENAI_MODELS))
    raise LLMError(
        "No LLM API key found. Paste a Gemini key (free) in the dashboard's "
        "AI-brain card, or set GEMINI_API_KEY / OPENAI_API_KEY, "
        "or use mode='offline' for zero-cost detection."
    )


def score_moments(
    transcript_text: str,
    rubric: str,
    n: int,
    provider: LLMProvider | None = None,
) -> tuple[list[dict], dict]:
    """Ask the LLM to pick the n best clip moments from transcript text.

    Returns (list_of_candidate_dicts, usage_dict). Each candidate dict is
    expected to carry start/end/title/hook_line/reason/score. Raises LLMError
    on any failure — callers must handle it, never fabricate replacements.
    """
    provider = provider or get_provider()
    system = (
        "You are an expert short-form video editor. You pick self-contained "
        "moments from a long video transcript that work as standalone vertical "
        "clips. Reply with JSON only."
    )
    user = (
        f"{rubric}\n\n"
        f"Pick the {n} best moments. Transcript lines are numbered sentences; "
        "each line shows [start-end] in seconds. Use ONLY timestamps that "
        "appear in the transcript — never invent times.\n\n"
        "Reply as a JSON object: "
        '{"clips": [{"start": <seconds>, "end": <seconds>, '
        '"title": "<=60 chars, no hashtags>", '
        '"hook_line": "<scroll-stopping first overlay line>", '
        '"reason": "<one line: why this moment works>", '
        '"score": <0-100 integer>}]}'
        "\n\nTranscript:\n" + transcript_text
    )
    log.info(
        "llm.score_moments via %s: ~%d prompt tokens, requesting %d clips",
        provider.name,
        estimate_tokens(system + user),
        n,
    )
    parsed, usage = provider.generate_json(system, user)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("clips"), list):
        raise LLMError(
            "LLM JSON did not contain a 'clips' list — refusing to guess"
        )
    return parsed["clips"], usage
