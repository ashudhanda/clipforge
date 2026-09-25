"""Persistent seen-store for ClipForge discovery.

``~/.clipforge/seen.json`` records every video the tool has already
processed, so discovery never suggests it again. Also records how many
clips each source produced (autopilot can prefer fertile sources later).

Never crashes the caller: a missing or corrupt file just behaves as empty.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from ..config import config_dir

log = logging.getLogger(__name__)

SEEN_FILE = "seen.json"


class SeenStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else config_dir() / SEEN_FILE
        self._data: dict[str, dict] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning("seen-store unreadable (%s): %s", self.path, exc)
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("seen-store corrupt, starting empty: %s", self.path)
            return
        if isinstance(data, dict):
            self._data = {str(k): v for k, v in data.items()
                          if isinstance(v, dict)}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, self.path)  # atomic-ish
        except OSError as exc:
            log.warning("seen-store save failed: %s", exc)

    def is_seen(self, video_id: str) -> bool:
        self._load()
        return str(video_id) in self._data

    def mark_used(self, video_id: str, niche: str = "",
                  clips_made: int = 0) -> None:
        """Record a processed video. Safe to call repeatedly."""
        self._load()
        vid = str(video_id)
        entry = self._data.get(vid, {})
        entry.update({
            "niche": niche,
            "used_at": time.time(),
            "clips_made": int(clips_made),
        })
        self._data[vid] = entry
        self.save()

    def update_clips(self, video_id: str, clips_made: int) -> None:
        """Update the clip count after a job finishes building."""
        self._load()
        vid = str(video_id)
        if vid in self._data:
            self._data[vid]["clips_made"] = int(clips_made)
            self.save()

    def count(self) -> int:
        self._load()
        return len(self._data)
