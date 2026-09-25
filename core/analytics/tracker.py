"""Clip performance records for ClipForge's feedback loop.

One JSON file per user (``~/.clipforge/analytics.json``, honoring
``CF_CONFIG_DIR``): every built clip gets a record with its niche, style,
score and cost inputs. Views/likes/comments are logged manually for now —
when Phase 6 adds YouTube OAuth, its auto-pull writes into the SAME
``stats`` fields, so nothing downstream changes.

Never invents numbers: a record with no stats simply has ``stats`` full of
``None`` and is excluded from performance math.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..config import config_dir

log = logging.getLogger("clipforge.analytics")

ANALYTICS_FILE = "analytics.json"

# Stats fields Phase 6's YouTube auto-pull will fill with the same names.
STAT_FIELDS = ("views", "likes", "comments")


@dataclass
class ClipRecord:
    """One built clip and everything we know about its performance."""

    clip_id: str
    niche: str
    caption_style: str
    score: float
    created_at: float
    source_url: str
    title: str
    # LLM cost inputs: per-clip usage dicts (each has est_prompt_tokens).
    usage: list[dict] = field(default_factory=list)
    # Usage shared across the whole source video (e.g. moment scoring),
    # divided by shared_among when totaling cost — never double counted.
    shared_usage: dict | None = None
    # Filled when the clip is uploaded (Phase 6 calls mark_uploaded).
    youtube_video_id: str | None = None
    uploaded_at: float | None = None
    # Manually logged now; Phase 6 auto-pull writes the same fields.
    stats: dict = field(
        default_factory=lambda: {"views": None, "likes": None,
                                 "comments": None, "logged_at": None})

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ClipRecord":
        stats = dict(d.get("stats") or {})
        for f in STAT_FIELDS:
            stats.setdefault(f, None)
        stats.setdefault("logged_at", None)
        return cls(
            clip_id=str(d.get("clip_id", "")),
            niche=str(d.get("niche", "")),
            caption_style=str(d.get("caption_style", "")),
            score=float(d.get("score") or 0.0),
            created_at=float(d.get("created_at") or 0.0),
            source_url=str(d.get("source_url", "")),
            title=str(d.get("title", "")),
            usage=list(d.get("usage") or []),
            shared_usage=d.get("shared_usage"),
            youtube_video_id=d.get("youtube_video_id"),
            uploaded_at=d.get("uploaded_at"),
            stats=stats,
        )


class AnalyticsStore:
    """Thread-safe JSON store for clip records."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else config_dir() / ANALYTICS_FILE
        self._lock = threading.Lock()
        self._records: dict[str, ClipRecord] = {}
        self._load()

    # -- persistence ----------------------------------------------------
    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as e:
            log.warning("analytics: can't read %s: %s", self.path, e)
            return
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as e:
            # Corrupt file: start empty rather than crash or lose the app.
            # Keep a backup so nothing is silently destroyed.
            log.warning("analytics: corrupt file %s (%s) — starting empty",
                        self.path, e)
            try:
                bak = self.path.with_suffix(".json.corrupt")
                bak.write_text(raw, encoding="utf-8")
            except OSError:
                pass
            return
        if not isinstance(data, list):
            log.warning("analytics: unexpected shape in %s — starting empty",
                        self.path)
            return
        for item in data:
            if isinstance(item, dict) and item.get("clip_id"):
                try:
                    rec = ClipRecord.from_dict(item)
                except (TypeError, ValueError):
                    continue
                self._records[rec.clip_id] = rec

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent),
                                       prefix=".analytics-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump([r.to_dict() for r in self._records.values()],
                          f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("analytics: can't write %s: %s", self.path, e)

    # -- writes ---------------------------------------------------------
    def record_clip(self, clip_id: str, niche: str, caption_style: str,
                    score: float, source_url: str, title: str,
                    usage: list[dict] | None = None,
                    shared_usage: dict | None = None) -> ClipRecord:
        """Store a newly built clip. Returns the record."""
        rec = ClipRecord(
            clip_id=clip_id, niche=niche, caption_style=caption_style,
            score=float(score), created_at=time.time(),
            source_url=source_url, title=title,
            usage=list(usage or []),
            shared_usage=(dict(shared_usage) | {"shared_among":
                          max(1, int(shared_usage.get("shared_among", 1)))})
            if shared_usage else None)
        with self._lock:
            self._records[clip_id] = rec
            self._save()
        return rec

    def update_clip(self, clip_id: str, **fields) -> bool:
        """Update mutable fields (caption_style, title, score)."""
        allowed = {"caption_style", "title", "score", "niche"}
        with self._lock:
            rec = self._records.get(clip_id)
            if not rec:
                return False
            for k, v in fields.items():
                if k in allowed:
                    setattr(rec, k, v)
            self._save()
            return True

    def mark_uploaded(self, clip_id: str, youtube_video_id: str) -> bool:
        with self._lock:
            rec = self._records.get(clip_id)
            if not rec:
                return False
            rec.youtube_video_id = youtube_video_id
            rec.uploaded_at = time.time()
            self._save()
            return True

    def log_stats(self, clip_id: str, views: int | None = None,
                  likes: int | None = None,
                  comments: int | None = None) -> bool:
        """Log performance numbers. Values must be non-negative ints."""
        for name, val in (("views", views), ("likes", likes),
                          ("comments", comments)):
            if val is not None and (not isinstance(val, int) or val < 0):
                raise ValueError(f"{name} must be a non-negative integer")
        with self._lock:
            rec = self._records.get(clip_id)
            if not rec:
                return False
            if views is not None:
                rec.stats["views"] = views
            if likes is not None:
                rec.stats["likes"] = likes
            if comments is not None:
                rec.stats["comments"] = comments
            rec.stats["logged_at"] = time.time()
            self._save()
            return True

    # -- reads ----------------------------------------------------------
    def get(self, clip_id: str) -> ClipRecord | None:
        with self._lock:
            return self._records.get(clip_id)

    def all_records(self) -> list[ClipRecord]:
        with self._lock:
            return list(self._records.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)
