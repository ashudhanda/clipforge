"""ClipForge user config — ~/.clipforge/config.json.

Never stores secrets (no API keys, no OAuth tokens). YouTube auth lands
in Phase 6 and will use the OS keyring / separate token file, not this.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .niches import niche_ids
APP_DIR_ENV = "CF_CONFIG_DIR"
DEFAULT_DIR = Path.home() / ".clipforge"
CONFIG_FILE = "config.json"

MODES = ("manual", "semi-auto", "autopilot")
MODE_LABELS = {
    "manual": "Manual — I make the clips here, then upload them myself",
    "semi-auto": "Semi-auto — I approve each clip, then the tool uploads it",
    "autopilot": "Full autopilot — finds videos, makes clips and uploads on its own",
}

DEFAULTS = {
    "setup_done": False,
    # No forced wizard anymore: first run auto-selects every niche so the
    # user can start instantly and trim the list anytime from Settings.
    "niches": [n for n in niche_ids() if n != "custom"],
    "custom_niche": "",      # free text when "custom" is selected
    "caption_style": "karaoke",  # one of AVAILABLE_STYLES or "random"
    "mode": "manual",        # manual | semi-auto | autopilot
    "daily_count": 2,        # clips per day
    "times": ["09:00", "18:00"],  # HH:MM upload/build times
    "autopilot": False,
    "quality_gate": 50,      # autopilot skips clips scoring below this
    # Default YouTube privacy for API uploads (public|unlisted|private).
    # "unlisted" is the safe default: nothing goes public by accident.
    "upload_privacy": "unlisted",
}

UPLOAD_PRIVACY = ("public", "unlisted", "private")

# Friendly quality-gate presets shown in the wizard. Stored as numbers.
QUALITY_GATE_PRESETS = {
    "low": 30,      # only skip the worst clips
    "medium": 50,   # balanced (recommended)
    "high": 70,     # only the best clips get through
}


def normalize_quality_gate(value):
    """'low'/'medium'/'high' (or a 0-100 number) -> int. None if invalid."""
    if isinstance(value, str):
        v = value.strip().lower()
        if v in QUALITY_GATE_PRESETS:
            return QUALITY_GATE_PRESETS[v]
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def config_dir() -> Path:
    # An empty CF_CONFIG_DIR must not mean "the current working directory".
    return Path(os.environ.get(APP_DIR_ENV) or str(DEFAULT_DIR))


def config_path() -> Path:
    return config_dir() / CONFIG_FILE


def atomic_write_text(path: Path, text: str) -> None:
    """Write *text* to *path* atomically (temp file + rename).

    A crash or kill mid-write can never leave a torn (half-written) file
    behind: readers either see the old content or the new content, never a
    mix. The temp file lives in the same directory so the rename stays on
    one filesystem (required for atomicity).
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _coerce_value(key: str, value):
    """Fall back to the default when a hand-edited value has the wrong type.

    load_config() promises bad files never crash the app; a wrong-typed
    value (e.g. "quality_gate": "abc") is just as broken as a missing key,
    so it gets the default instead of poisoning downstream int()/len()
    calls. Files written by save_config() always carry native types, so
    this only ever triggers on hand-edited files.
    """
    default = DEFAULTS[key]
    if key == "quality_gate":
        v = normalize_quality_gate(value)
        return v if v is not None else default
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if isinstance(default, int):
        if isinstance(value, bool):
            return default
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return default
    if isinstance(default, str):
        return value if isinstance(value, str) else default
    if isinstance(default, list):
        return value if isinstance(value, list) else default
    return value


def default_config() -> dict:
    return json.loads(json.dumps(DEFAULTS))


def load_config() -> dict:
    """Load config, merged over defaults so bad files never crash.

    Missing file, invalid JSON, undecodable bytes, or wrong-typed values
    all fall back to defaults (per-key), never raise.
    """
    cfg = default_config()
    try:
        # utf-8-sig tolerates a BOM left by Windows editors; plain UTF-8
        # files read identically. UnicodeDecodeError is a ValueError.
        raw = config_path().read_text(encoding="utf-8-sig")
    except (OSError, ValueError):
        return cfg
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return cfg
    if isinstance(data, dict):
        for k, v in data.items():
            if k in DEFAULTS:
                cfg[k] = _coerce_value(k, v)
    return cfg


def validate_config(cfg: dict) -> list[str]:
    """Return a list of plain-language problems; empty means valid."""
    problems: list[str] = []
    ids = niche_ids()

    niches = cfg.get("niches") or []
    if not isinstance(niches, list) or not niches:
        problems.append("Pick at least one niche.")
    else:
        bad = [n for n in niches if n not in ids]
        if bad:
            problems.append(f"Unknown niche(s): {', '.join(bad)}.")
        if "custom" in niches and not str(cfg.get("custom_niche") or "").strip():
            problems.append("You picked Custom — please type your niche name.")

    # caption style is validated against core.edit at app level (lazy import
    # to keep this module dependency-free); here just check it's a non-empty str.
    if not str(cfg.get("caption_style") or "").strip():
        problems.append("Pick a caption style.")

    if cfg.get("mode") not in MODES:
        problems.append("Pick an upload mode: manual, semi-auto or autopilot.")

    try:
        count = int(cfg.get("daily_count", 0))
    except (TypeError, ValueError):
        count = 0
    if not 1 <= count <= 10:
        problems.append("Daily clips must be between 1 and 10.")

    times = cfg.get("times") or []
    if not isinstance(times, list) or len(times) != count:
        problems.append(f"Give exactly {count} time(s), one per daily clip (HH:MM).")
    else:
        import re

        for t in times:
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(t)):
                problems.append(f"Bad time {t!r} — use HH:MM, e.g. 09:00.")
                break

    try:
        qg = int(cfg.get("quality_gate", 50))
    except (TypeError, ValueError):
        qg = -1
    if not 0 <= qg <= 100:
        problems.append("Quality gate must be low, medium, high, or a number 0–100.")

    if cfg.get("upload_privacy", "unlisted") not in UPLOAD_PRIVACY:
        problems.append("Upload privacy must be public, unlisted or private.")

    return problems


def save_config(cfg: dict) -> Path:
    """Validate, then write. Raises ValueError with plain-language problems."""
    cfg = dict(cfg)
    qg = normalize_quality_gate(cfg.get("quality_gate", 50))
    if qg is not None:
        cfg["quality_gate"] = qg
    problems = validate_config(cfg)
    if problems:
        raise ValueError(" | ".join(problems))
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    clean = default_config()
    for k in DEFAULTS:
        if k in cfg:
            clean[k] = cfg[k]
    clean["setup_done"] = True
    path = config_path()
    # Atomic write: a kill/crash mid-save must not leave a torn config.json
    # (which load_config would then silently reset to defaults).
    atomic_write_text(path, json.dumps(clean, indent=2, ensure_ascii=False))
    return path
