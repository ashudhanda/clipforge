"""End-to-end Shorts build pipeline for ClipForge.

``build_short(source_video, clip, out_path)``:
  1. plan silence + filler-word cuts on [clip.start, clip.end)
  2. compute the face-aware full-bleed 9:16 crop
  3. remap transcript word timings through the cut plan
  4. single ffmpeg pass: trim -> cut -> crop -> scale -> caption burn (video),
     trim -> cut -> loudnorm (audio) -> 1080x1920 H.264 MP4
  5. verify the output with ffprobe

Deterministic: same inputs -> same output. Every step is logged.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile

from . import captions as _captions
from . import crop as _crop
from . import loudness as _loudness
from . import silence as _silence
from core.paths import ffmpeg_path as _ffmpeg_path
from core.paths import ffprobe_path as _ffprobe_path

log = logging.getLogger("clipforge.edit")

OUT_W, OUT_H = 1080, 1920
FPS = 30


def _ffmpeg() -> str:
    path = _ffmpeg_path()
    if not path:
        raise RuntimeError(
            "ffmpeg not found — install it (see README) or set CLIPFORGE_FFMPEG"
        )
    return path


def _clip_get(clip, key: str, default=None):
    if isinstance(clip, dict):
        return clip.get(key, default)
    return getattr(clip, key, default)


def build_short(
    source_video: str,
    clip: dict,
    out_path: str,
    preset: str = "medium",
    crf: int = 20,
    workdir: str | None = None,
) -> str:
    """Build one vertical Short from *source_video*.

    *clip* needs ``start``, ``end``, ``transcript`` (word list, clip-relative
    or absolute seconds — see below) and ``caption_style``.
    Transcript word times may be absolute (source timeline) or relative to
    ``clip.start``; absolute is auto-detected (any word start >= clip.start).

    Returns *out_path*; raises RuntimeError on any failure.
    """
    start = float(_clip_get(clip, "start"))
    end = float(_clip_get(clip, "end"))
    style = _captions.resolve_style(_clip_get(clip, "caption_style", "karaoke") or "karaoke")
    words = list(_clip_get(clip, "transcript", []) or [])
    if end <= start:
        raise ValueError(f"clip end ({end}) must be > start ({start})")
    if not os.path.exists(source_video):
        raise FileNotFoundError(f"source video not found: {source_video}")

    tmp = tempfile.mkdtemp(prefix="cf2edit_", dir=workdir)
    try:
        # 1. Cut plan (silence + filler words). Words may carry absolute
        #    source times -> shift to clip-relative for the planner.
        rel_words = _to_relative(words, start)
        plan = _silence.plan_cuts(source_video, start, end, words=rel_words)
        log.info(
            "cut plan: %.1fs -> %.1fs kept (%d segments, %.0f%% speech)",
            plan.duration,
            _silence.kept_duration(plan.kept),
            len(plan.kept),
            plan.speech_ratio * 100,
        )
        if not plan.kept:
            raise RuntimeError("cut plan kept nothing — clip is all silence?")

        # 2. Crop (face-aware, full-bleed 9:16 — never letterboxed).
        crop_p = _crop.compute_crop(source_video, start, end)
        if not _crop.is_full_bleed(crop_p):
            raise RuntimeError(
                f"internal error: crop is not exactly 9:16 full-bleed: {crop_p!r}"
            )
        log.info(
            "crop: %dx%d @ (%d,%d) face_guided=%s",
            crop_p["w"], crop_p["h"], crop_p["x"], crop_p["y"],
            crop_p["face_guided"],
        )

        # 3. Remap word timings through the cuts -> ASS.
        final_words = _silence.remap_words(rel_words, plan.kept)
        ass_path = os.path.join(tmp, "captions.ass")
        _captions.write_ass(final_words, style, ass_path)
        log.info("captions: %d words -> %s style", len(final_words), style)

        # 4. Single ffmpeg pass.
        sel = _silence.build_select_expr(plan.kept)
        v_filters = ",".join([
            f"trim=start={start:.3f}:end={end:.3f}",
            "setpts=PTS-STARTPTS",
            f"fps={FPS}",
            f"select='{sel}'",
            f"setpts=N/{FPS}/TB",
            _crop.crop_filter(crop_p),
            f"scale={OUT_W}:{OUT_H}:flags=lanczos",
            _captions.ass_filter(ass_path),
            "format=yuv420p",
        ])
        a_filters = ",".join([
            f"atrim=start={start:.3f}:end={end:.3f}",
            "asetpts=PTS-STARTPTS",
            f"aselect='{sel}'",
            "asetpts=N/SR/TB",
            _loudness.loudnorm_filter(),
            "aresample=48000",
        ])
        cmd = [
            _ffmpeg(), "-hide_banner", "-loglevel", "warning",
            "-i", source_video,
            "-filter_complex",
            f"[0:v]{v_filters}[v];[0:a]{a_filters}[a]",
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            "-y", out_path,
        ]
        log.info("rendering %s", out_path)
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg render failed: {proc.stderr.decode()[-500:]}"
            )

        # 5. Verify output.
        _verify_output(out_path, _silence.kept_duration(plan.kept))
        log.info("done: %s", out_path)
        return out_path
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _to_relative(words: list, clip_start: float,
                absolute: bool | None = None) -> list[dict]:
    """Shift word timings to clip-relative seconds.

    ``absolute=None`` (default) auto-detects: words already near 0 are
    treated as clip-relative, words on the source timeline (>= clip_start)
    are shifted. The shift is validated — if shifting would push words
    clearly negative, the words were already relative and are returned
    untouched (this guards the old ``any(start >= clip_start)`` heuristic
    against misfiring on relative words that start after clip_start).
    Pass ``absolute=True/False`` explicitly when the caller knows.
    """
    norm: list[dict] = []
    for w in words:
        ws = w["start"] if isinstance(w, dict) else w.start
        we = w["end"] if isinstance(w, dict) else w.end
        text = w["text"] if isinstance(w, dict) else w.text
        norm.append({"start": ws, "end": we, "text": text})
    if not norm or clip_start <= 1e-6:
        return norm
    if absolute is None:
        shifted = [
            {"start": w["start"] - clip_start,
             "end": w["end"] - clip_start,
             "text": w["text"]}
            for w in norm
        ]
        # A genuine absolute→relative shift lands at ~0-based times.
        # Already-relative words would go (mostly) negative instead.
        if all(w["start"] >= -1.0 for w in shifted):
            return shifted
        return norm
    if absolute:
        return [
            {"start": w["start"] - clip_start,
             "end": w["end"] - clip_start,
             "text": w["text"]}
            for w in norm
        ]
    return norm


def _verify_output(out_path: str, expected_dur: float) -> None:
    ffprobe = _ffprobe_path()
    if not ffprobe:
        raise RuntimeError("ffprobe not found — cannot verify output")
    cmd = [
        ffprobe, "-hide_banner", "-loglevel", "error",
        "-show_entries", "stream=width,height,codec_type:format=duration",
        "-of", "json", out_path,
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed on output: {proc.stderr.decode()[:200]}")
    import json

    info = json.loads(proc.stdout.decode() or "{}")
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if v is None:
        raise RuntimeError("output has no video stream")
    if a is None:
        raise RuntimeError("output has no audio stream")
    w, h = int(v["width"]), int(v["height"])
    if (w, h) != (OUT_W, OUT_H):
        raise RuntimeError(f"output is {w}x{h}, expected {OUT_W}x{OUT_H}")
    dur = float(info.get("format", {}).get("duration", 0) or 0)
    if dur and abs(dur - expected_dur) > 1.0:
        raise RuntimeError(
            f"output duration {dur:.1f}s far from expected {expected_dur:.1f}s"
        )
