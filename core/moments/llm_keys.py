"""LLM API key storage for ClipForge — ``~/.clipforge/llm_keys.json``.

Kept OUT of ``config.json`` on purpose: config.json is documented as never
holding secrets. This file is created with mode 0o600 (owner-only), the same
convention as the YouTube OAuth token files.

The dashboard's "AI brain" card writes here via ``POST /api/llm``;
``llm.get_provider()`` reads environment variables first and falls back to
this file, so a key pasted in the UI just works with no terminal needed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from core.config import config_dir

KEYS_FILE = "llm_keys.json"
VALID_PROVIDERS = ("auto", "gemini", "openai")


def keys_path() -> Path:
    return config_dir() / KEYS_FILE


def load_keys() -> dict:
    """Return ``{"gemini_key","openai_key","provider"}``; missing file -> blanks."""
    out = {"gemini_key": "", "openai_key": "", "provider": "auto"}
    try:
        raw = keys_path().read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return out
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return out
    if isinstance(data, dict):
        for k in ("gemini_key", "openai_key"):
            v = data.get(k)
            if isinstance(v, str):
                out[k] = v.strip()
        if data.get("provider") in VALID_PROVIDERS:
            out["provider"] = data["provider"]
    return out


def save_keys(
    gemini_key: str = "",
    openai_key: str = "",
    provider: str = "auto",
) -> Path:
    """Write keys with owner-only (0o600) permissions.

    Writes exactly what it is given (use ``forget_key`` to clear one key).
    The dashboard API (``POST /api/llm``) treats blank fields as "keep the
    stored key", so blanks never reach this function as clears.
    """
    if provider not in VALID_PROVIDERS:
        raise ValueError(f"provider must be one of {VALID_PROVIDERS}")
    p = keys_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "gemini_key": (gemini_key or "").strip(),
            "openai_key": (openai_key or "").strip(),
            "provider": provider,
        }
    )
    # os.open with 0o600 from the start: no window where the file is wider.
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(payload)
    # Belt & braces: enforce 0o600 even if the file pre-existed with wider perms.
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def clear_keys() -> None:
    """Remove the key file entirely (used by tests / "forget my keys")."""
    try:
        keys_path().unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def forget_key(which: str) -> None:
    """Forget one key (``"gemini"`` or ``"openai"``), keeping the other.

    This is the ONLY supported way to clear a single key: saving with a
    blank field preserves the stored key (see ``POST /api/llm``).
    """
    which = (which or "").strip().lower()
    if which not in ("gemini", "openai"):
        raise ValueError('which must be "gemini" or "openai"')
    stored = load_keys()
    stored[f"{which}_key"] = ""
    save_keys(
        gemini_key=stored["gemini_key"],
        openai_key=stored["openai_key"],
        provider=stored["provider"],
    )
