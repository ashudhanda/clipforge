"""Silence / filler-word removal planning for ClipForge2.

Energy-based voice activity detection (adapted conceptually from the
auto-editor/cutawan audit: ~-28 dBFS threshold, pre/post-roll padding,
short-gap bridging) plus transcript-driven filler-word cuts.

Everything here only *plans* cuts — the actual cutting happens in
``pipeline.py`` via ffmpeg ``select``/``aselect`` filters, which keeps
cuts sample/frame-accurate in a single pass.
"""

from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass

from core.paths import ffmpeg_path as _ffmpeg_path

import numpy as np

# Filler-word set adapted from the cutawan audit (tighten.ts).
FILLER_WORDS = frozenset(
    {"um", "uh", "uhm", "umm", "erm", "er", "ah", "mmm", "hmm", "mhm"}
)

DEFAULT_THRESHOLD_DB = -28.0  # ~= 0.04 linear amplitude (auto-editor's 0.04)
DEFAULT_SR = 16000
FRAME_MS = 20  # analysis frame


@dataclass
class CutPlan:
    """Result of planning cuts for one clip.

    All times are relative to the clip's own timeline (0 == clip start).
    """

    duration: float
    kept: list[tuple[float, float]]  # segments to keep, sorted, non-overlapping
    removed: list[tuple[float, float]]  # segments cut out, sorted
    speech_ratio: float  # fraction of clip that is speech


def _ffmpeg() -> str:
    path = _ffmpeg_path()
    if not path:
        raise RuntimeError(
            "ffmpeg not found — install it (see README) or set CLIPFORGE2_FFMPEG"
        )
    return path


def decode_mono_pcm(
    video_path: str,
    start: float,
    end: float,
    sample_rate: int = DEFAULT_SR,
) -> np.ndarray:
    """Decode [start, end) of *video_path* to mono float32 PCM.

    ``-ss``/``-to`` are placed AFTER ``-i`` (accurate seek — the fast
    keyframe seek would silently misalign sub-second cuts).
    """
    cmd = [
        _ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        video_path,
        "-ss",
        f"{start:.3f}",
        "-to",
        f"{end:.3f}",
        "-ar",
        str(sample_rate),
        "-ac",
        "1",
        "-f",
        "f32le",
        "-",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg audio decode failed: {proc.stderr.decode()[:300]}"
        )
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def _frame_dbfs(pcm: np.ndarray, sample_rate: int) -> np.ndarray:
    frame_len = max(1, int(sample_rate * FRAME_MS / 1000))
    n_frames = max(1, len(pcm) // frame_len)
    trimmed = pcm[: n_frames * frame_len].reshape(n_frames, frame_len)
    rms = np.sqrt(np.mean(trimmed.astype(np.float64) ** 2, axis=1))
    with np.errstate(divide="ignore"):
        db = 20.0 * np.log10(np.maximum(rms, 1e-12))
    return db


def _runs(labels: np.ndarray) -> list[tuple[bool, int, int]]:
    """Run-length encode a boolean array -> [(value, start, end), ...]."""
    out: list[tuple[bool, int, int]] = []
    if len(labels) == 0:
        return out
    cur = bool(labels[0])
    s = 0
    for i in range(1, len(labels)):
        if bool(labels[i]) != cur:
            out.append((cur, s, i))
            cur = bool(labels[i])
            s = i
    out.append((cur, s, len(labels)))
    return out


def detect_speech_regions(
    pcm: np.ndarray,
    sample_rate: int = DEFAULT_SR,
    threshold_db: float = DEFAULT_THRESHOLD_DB,
    min_speech_s: float = 0.25,
    min_silence_s: float = 0.10,
) -> list[tuple[float, float]]:
    """Return raw (unpadded) speech intervals in seconds."""
    if len(pcm) == 0:
        return []
    db = _frame_dbfs(pcm, sample_rate)
    speech = db > threshold_db

    frame_s = FRAME_MS / 1000.0
    min_speech_f = max(1, int(min_speech_s / frame_s))
    min_silence_f = max(1, int(min_silence_s / frame_s))

    # Bridge micro-gaps inside speech, then drop micro-blips of "speech".
    labels = speech.copy()
    for value, s, e in _runs(labels):
        if not value and (e - s) < min_silence_f:
            labels[s:e] = True
    for value, s, e in _runs(labels):
        if value and (e - s) < min_speech_f:
            labels[s:e] = False

    regions: list[tuple[float, float]] = []
    for value, s, e in _runs(labels):
        if value:
            regions.append((s * frame_s, e * frame_s))
    return regions


def plan_cuts(
    video_path: str,
    start: float,
    end: float,
    words: list | None = None,
    threshold_db: float = DEFAULT_THRESHOLD_DB,
    pre_roll_s: float = 0.18,
    post_roll_s: float = 0.30,
    min_cut_s: float = 0.35,
    sample_rate: int = DEFAULT_SR,
) -> CutPlan:
    """Plan silence + filler cuts for the clip [start, end).

    *words* — optional word-level transcript (dicts or objects with
    ``start``/``end``/``text``) in clip-relative seconds; filler words
    (um/uh/...) are cut out with a small padding.

    Returns a :class:`CutPlan` with kept/removed segments.
    """
    duration = max(0.0, end - start)
    if duration <= 0:
        return CutPlan(duration=0.0, kept=[], removed=[], speech_ratio=0.0)

    pcm = decode_mono_pcm(video_path, start, end, sample_rate)
    speech = detect_speech_regions(pcm, sample_rate, threshold_db)

    # Pad speech with pre/post roll, clamp, merge.
    padded: list[tuple[float, float]] = []
    for s, e in speech:
        padded.append((max(0.0, s - pre_roll_s), min(duration, e + post_roll_s)))
    padded.sort()
    merged: list[list[float]] = []
    for s, e in padded:
        if merged and s <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    # Merge across gaps too short to bother cutting.
    kept: list[list[float]] = []
    for s, e in merged:
        if kept and s - kept[-1][1] < min_cut_s:
            kept[-1][1] = e
        else:
            kept.append([s, e])

    removals: list[tuple[float, float]] = []
    prev = 0.0
    for s, e in kept:
        if s - prev >= min_cut_s:
            removals.append((prev, s))
        prev = e
    if duration - prev >= min_cut_s:
        removals.append((prev, duration))

    # Filler-word cuts from the transcript.
    if words:
        for w in words:
            text = (w["text"] if isinstance(w, dict) else w.text).strip().lower()
            text = text.strip(".,!?;:'\"()[]")
            if text in FILLER_WORDS:
                ws = w["start"] if isinstance(w, dict) else w.start
                we = w["end"] if isinstance(w, dict) else w.end
                removals.append(
                    (max(0.0, ws - 0.05), min(duration, we + 0.05))
                )

    # Merge all removals, then derive kept = complement.
    removals.sort()
    merged_rem: list[list[float]] = []
    for s, e in removals:
        if e <= s:
            continue
        if merged_rem and s <= merged_rem[-1][1] + 1e-6:
            merged_rem[-1][1] = max(merged_rem[-1][1], e)
        else:
            merged_rem.append([s, e])

    final_kept: list[tuple[float, float]] = []
    prev = 0.0
    for s, e in merged_rem:
        if s > prev:
            final_kept.append((prev, s))
        prev = max(prev, e)
    if prev < duration:
        final_kept.append((prev, duration))

    speech_dur = sum(e - s for s, e in final_kept)
    return CutPlan(
        duration=duration,
        kept=[(round(s, 3), round(e, 3)) for s, e in final_kept],
        removed=[(round(s, 3), round(e, 3)) for s, e in merged_rem],
        speech_ratio=round(speech_dur / duration, 4) if duration else 0.0,
    )


def build_select_expr(kept: list[tuple[float, float]]) -> str:
    """Build a ``between(t,a,b)+...`` expression for select/aselect."""
    if not kept:
        return "between(t,0,0.001)"
    return "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in kept)


def remap_time(t: float, kept: list[tuple[float, float]]) -> float:
    """Map a clip-relative timestamp onto the post-cut timeline."""
    out = 0.0
    for s, e in kept:
        if t < s:
            return out  # t falls inside a cut -> snap to cut start
        if t <= e:
            return out + (t - s)
        out += e - s
    return out  # past the end -> total kept duration


def kept_duration(kept: list[tuple[float, float]]) -> float:
    return sum(e - s for s, e in kept)


def remap_words(
    words: list,
    kept: list[tuple[float, float]],
    min_display_s: float = 0.05,
) -> list[dict]:
    """Remap word timings through the cut plan; drop words lost to cuts."""
    out: list[dict] = []
    for w in words:
        ws = w["start"] if isinstance(w, dict) else w.start
        we = w["end"] if isinstance(w, dict) else w.end
        text = w["text"] if isinstance(w, dict) else w.text
        ns, ne = remap_time(ws, kept), remap_time(we, kept)
        if ne - ns >= min_display_s:
            out.append({"start": round(ns, 3), "end": round(ne, 3), "text": text})
    return out
