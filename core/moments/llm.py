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
import json
import logging
import os
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


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token). Used for the cost guard log."""
    return max(1, len(text) // 4)


def _post_json(url: str, payload: dict, headers: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        raise LLMError(f"LLM HTTP {e.code}: {body}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LLMError(f"LLM network error: {e}") from e


class LLMProvider(abc.ABC):
    """Abstract chat-completion provider returning parsed JSON."""

    name: str = "base"

    @abc.abstractmethod
    def generate_json(self, system: str, user: str) -> tuple[object, dict]:
        """Return (parsed_json, usage_dict). Raise LLMError on any failure."""
        raise NotImplementedError


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
                text = resp["candidates"][0]["content"]["parts"][0]["text"]
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
                text = resp["choices"][0]["message"]["content"]
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
    """Pick a provider from available API keys. Raise LLMError if none."""
    prefer = (prefer or os.environ.get("CF_LLM_PROVIDER", "")).lower()
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if prefer == "openai":
        return OpenAIProvider(openai_key or None)
    if prefer == "gemini":
        return GeminiProvider(gemini_key or None)
    if gemini_key:
        return GeminiProvider(gemini_key)
    if openai_key:
        return OpenAIProvider(openai_key)
    raise LLMError(
        "No LLM API key found. Set GEMINI_API_KEY (preferred, free tier) or "
        "OPENAI_API_KEY, or use mode='offline' for zero-cost detection."
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
