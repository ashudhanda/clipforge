"""ClipForge2 user config — ~/.clipforge2/config.json.

Never stores secrets (no API keys, no OAuth tokens). YouTube auth lands
in Phase 6 and will use the OS keyring / separate token file, not this.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .niches import niche_ids

APP_DIR_ENV = "CF2_CONFIG_DIR"
DEFAULT_DIR = Path.home() / ".clipforge2"
CONFIG_FILE = "config.json"

MODES = ("manual", "semi-auto", "autopilot")
MODE_LABELS = {
    "manual": "Manual — I make the clips here, then upload them myself",
    "semi-auto": "Semi-auto — I approve each clip, then the tool uploads it",
    "autopilot": "Full autopilot — finds videos, makes clips and uploads on its own",
}

DEFAULTS = {
    "setup_done": False,
    "niches": [],            # list of niche ids
    "custom_niche": "",      # free text when "custom" is selected
    "caption_style": "karaoke",  # one of AVAILABLE_STYLES or "random"
    "mode": "manual",        # manual | semi-auto | autopilot
    "daily_count": 2,        # clips per day
    "times": ["09:00", "18:00"],  # HH:MM upload/build times
    "autopilot": False,
    "quality_gate": 50,      # autopilot skips clips scoring below this
}

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
    return Path(os.environ.get(APP_DIR_ENV, str(DEFAULT_DIR)))


def config_path() -> Path:
    return config_dir() / CONFIG_FILE


def default_config() -> dict:
    return json.loads(json.dumps(DEFAULTS))


def load_config() -> dict:
    """Load config, merged over defaults so missing keys never crash."""
    cfg = default_config()
    try:
        raw = config_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return cfg
    except OSError:
        return cfg
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return cfg
    if isinstance(data, dict):
        for k, v in data.items():
            if k in DEFAULTS:
                cfg[k] = v
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
    path.write_text(json.dumps(clean, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
