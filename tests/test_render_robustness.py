"""Robustness regression tests for core.edit (bug-hunt 2026-09-26).

Covers concrete failure modes found by auditing the render subsystem:
ffmpeg failures, caption edge cases (empty/None text, inverted times,
very long words, unicode/emoji, overlapping timestamps), crop math on
odd/tiny resolutions, corrupt inputs, and output verification.

All tests are fast: real ffmpeg is only exercised through mocks, except
for tiny fixture files created on disk (never encoded).
"""

import json
import os
import subprocess
import sys

import pytest

from core.edit import captions as _captions
from core.edit import crop as _crop
from core.edit import pipeline as _pipeline
from core.edit import silence as _silence
from core.edit.captions import CAPTION_STYLES, build_ass
from core.edit.crop import compute_crop, crop_filter, is_full_bleed
from core.edit.pipeline import _verify_output, build_short
from core.edit.silence import (
    build_select_expr,
    detect_speech_regions,
    remap_words,
)


# --- helpers ---

def _ffprobe_payload(duration="5.040000", width=1080, height=1920,
                     audio=True, video=True):
    streams = []
    if video:
        streams.append({"codec_type": "video", "width": width, "height": height})
    if audio:
        streams.append({"codec_type": "audio"})
    payload = {"streams": streams}
    if duration is not None:
        payload["format"] = {"duration": duration}
    return payload


def _fake_run(payload=None, rc=0, stderr=b""):
    def _run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, rc,
            json.dumps(payload or {}).encode() if rc == 0 else b"",
            stderr,
        )
    return _run


@pytest.fixture
def fake_ffprobe(monkeypatch):
    """Route _verify_output's ffprobe call through a canned JSON payload."""
    def _set(payload, rc=0, stderr=b""):
        monkeypatch.setattr(_pipeline, "_ffprobe_path", lambda: "/fake/ffprobe")
        monkeypatch.setattr(subprocess, "run", _fake_run(payload, rc, stderr))
    return _set


@pytest.fixture
def src_file(tmp_path):
    p = tmp_path / "src.mp4"
    p.write_bytes(b"\x00" * 64)  # only existence is checked pre-render
    return str(p)


# --- P0: build_short must never render onto its own source ---

def test_build_short_refuses_self_overwrite(src_file, tmp_path):
    clip = {"start": 0.0, "end": 2.0, "transcript": []}
    with pytest.raises(ValueError, match="must differ from source_video"):
        build_short(src_file, clip, src_file)


def test_build_short_refuses_self_overwrite_via_relative_path(src_file, tmp_path):
    # Same file reached through a different spelling (./, sub/../) must
    # still be caught — realpath comparison, not string comparison.
    rel = os.path.join(str(tmp_path), "sub", "..", "src.mp4")
    clip = {"start": 0.0, "end": 2.0, "transcript": []}
    with pytest.raises(ValueError, match="must differ from source_video"):
        build_short(src_file, clip, rel)


def test_build_short_none_text_word_raises_clear_error(src_file, tmp_path):
    # A null word text (corrupt transcript) must fail fast with a clear
    # error — not an AttributeError deep inside the pipeline.
    clip = {"start": 0.0, "end": 2.0,
            "transcript": [{"start": 0.0, "end": 0.5, "text": None}]}
    with pytest.raises(ValueError, match="must be a string"):
        build_short(src_file, clip, str(tmp_path / "out.mp4"))


# --- P1: tiny sources used to produce an ffmpeg-impossible crop ---

@pytest.mark.parametrize("dims", [(20, 20), (10, 100), (100, 10), (17, 200)])
def test_compute_crop_tiny_source_raises_clear_error(monkeypatch, dims):
    # Previously: 18x32 window on a 20x20 frame passed is_full_bleed and
    # died inside ffmpeg with "Invalid too big or non positive size".
    monkeypatch.setattr(_crop, "displayed_dims", lambda p: dims)
    monkeypatch.setattr(_crop, "detect_face_centers", lambda *a: [])
    with pytest.raises(RuntimeError, match="too small for a 9:16 crop"):
        compute_crop("/fake/tiny.mp4", 0.0, 2.0)


def test_compute_crop_small_but_valid_source_ok(monkeypatch):
    # The guard must not false-positive on small-but-valid sources.
    monkeypatch.setattr(_crop, "displayed_dims", lambda p: (64, 64))
    monkeypatch.setattr(_crop, "detect_face_centers", lambda *a: [])
    c = compute_crop("/fake/small.mp4", 0.0, 2.0)
    assert is_full_bleed(c)
    assert c["x"] + c["w"] <= 64 and c["y"] + c["h"] <= 64
    assert c["w"] > 0 and c["h"] > 0


@pytest.mark.parametrize("dims", [
    (1920, 1080), (1080, 1920), (1919, 1080), (1280, 719),
    (720, 1280), (640, 480), (3840, 2160), (100, 100),
])
def test_compute_crop_window_always_inside_frame(monkeypatch, dims):
    # Property: the crop window is exact 9:16, even-aligned, and never
    # exceeds the frame — for odd and even dimensions alike.
    dw, dh = dims
    monkeypatch.setattr(_crop, "displayed_dims", lambda p: (dw, dh))
    monkeypatch.setattr(_crop, "detect_face_centers", lambda *a: [])
    c = compute_crop("/fake/v.mp4", 0.0, 2.0)
    assert is_full_bleed(c)
    assert 0 <= c["x"] and c["x"] + c["w"] <= dw
    assert 0 <= c["y"] and c["y"] + c["h"] <= dh
    assert c["x"] % 2 == 0 and c["y"] % 2 == 0
    assert c["w"] % 2 == 0 and c["h"] % 2 == 0
    crop_filter(c)  # renders without error


# --- P1: _verify_output must not die parsing ffprobe output ---

def test_verify_output_ok(fake_ffprobe):
    fake_ffprobe(_ffprobe_payload(duration="5.040000"))
    _verify_output("/fake/out.mp4", 5.0)  # no raise


@pytest.mark.parametrize("duration", ["N/A", "", None])
def test_verify_output_unparseable_duration_skips_check(fake_ffprobe, duration):
    # ffprobe can report duration as "N/A" or omit it; that used to raise
    # ValueError (wrong exception type). The dims/stream gates still apply.
    fake_ffprobe(_ffprobe_payload(duration=duration))
    _verify_output("/fake/out.mp4", 5.0)  # no raise


def test_verify_output_duration_far_off_raises(fake_ffprobe):
    fake_ffprobe(_ffprobe_payload(duration="60.0"))
    with pytest.raises(RuntimeError, match="far from expected"):
        _verify_output("/fake/out.mp4", 5.0)


def test_verify_output_wrong_dimensions_raises(fake_ffprobe):
    fake_ffprobe(_ffprobe_payload(width=1280, height=720))
    with pytest.raises(RuntimeError, match="expected 1080x1920"):
        _verify_output("/fake/out.mp4", 5.0)


def test_verify_output_missing_audio_raises(fake_ffprobe):
    fake_ffprobe(_ffprobe_payload(audio=False))
    with pytest.raises(RuntimeError, match="no audio stream"):
        _verify_output("/fake/out.mp4", 5.0)


def test_verify_output_missing_video_raises(fake_ffprobe):
    fake_ffprobe(_ffprobe_payload(video=False))
    with pytest.raises(RuntimeError, match="no video stream"):
        _verify_output("/fake/out.mp4", 5.0)


def test_verify_output_ffprobe_failure_raises(fake_ffprobe):
    fake_ffprobe({}, rc=1, stderr=b"moov atom not found")
    with pytest.raises(RuntimeError, match="ffprobe failed"):
        _verify_output("/fake/out.mp4", 5.0)


# --- P1: caption word text must be a string (None used to AttributeError) ---

@pytest.mark.parametrize("style", CAPTION_STYLES)
def test_build_ass_none_text_raises_valueerror(style):
    words = [{"start": 0.0, "end": 0.5, "text": None}]
    with pytest.raises(ValueError, match="must be a string"):
        build_ass(words, style)


def test_build_ass_non_string_text_raises_valueerror():
    words = [{"start": 0.0, "end": 0.5, "text": 42}]
    with pytest.raises(ValueError, match="must be a string"):
        build_ass(words, "karaoke")


# --- P1: ffmpeg failure in build_short must stay loud (never swallowed) ---

def test_build_short_ffmpeg_failure_is_loud(src_file, tmp_path, monkeypatch):
    # Simulates e.g. an audio-less source ("matches no streams"): the
    # render must raise RuntimeError carrying ffmpeg's message.
    plan = _silence.CutPlan(duration=3.0, kept=[(0.0, 3.0)],
                            removed=[], speech_ratio=1.0)
    monkeypatch.setattr(_silence, "plan_cuts", lambda *a, **k: plan)
    monkeypatch.setattr(
        _crop, "compute_crop",
        lambda *a, **k: {"x": 0, "y": 0, "w": 1080, "h": 1920,
                         "src_w": 1080, "src_h": 1920,
                         "face_guided": False, "target": [1080, 1920]})
    monkeypatch.setattr(_captions, "write_ass",
                        lambda words, style, path: path)
    monkeypatch.setattr(
        subprocess, "run",
        _fake_run(rc=1, stderr=b"Stream specifier ':a' in filtergraph "
                               b"description matches no streams."))
    clip = {"start": 0.0, "end": 3.0, "transcript": []}
    with pytest.raises(RuntimeError, match="ffmpeg render failed"):
        build_short(src_file, clip, str(tmp_path / "out.mp4"))


# --- ffmpeg/subprocess failures elsewhere in edit/ stay loud ---

def test_decode_mono_pcm_failure_raises(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(rc=1, stderr=b"boom"))
    with pytest.raises(RuntimeError, match="ffmpeg audio decode failed"):
        _silence.decode_mono_pcm("/fake/v.mp4", 0.0, 1.0)


def test_displayed_dims_ffmpeg_failure_raises(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(rc=1, stderr=b"boom"))
    with pytest.raises(RuntimeError, match="frame extraction failed"):
        _crop.displayed_dims("/fake/v.mp4")


def test_detect_face_centers_without_cv2_falls_back(monkeypatch):
    # No OpenCV (or no faces) must fall back to a center crop, never crash.
    monkeypatch.setitem(sys.modules, "cv2", None)
    assert _crop.detect_face_centers("/fake/v.mp4", 0.0, 5.0) == []


# --- caption edge cases: must never crash, must stay well-formed ---

def test_build_ass_empty_words_valid():
    doc = build_ass([], "karaoke")
    assert "[Events]" in doc and "PlayResX: 1080" in doc


def test_build_ass_inverted_word_times_no_crash():
    # end < start: clamped, no crash, event still emitted.
    words = [{"start": 1.0, "end": 0.2, "text": "backwards"}]
    doc = build_ass(words, "karaoke")
    assert "Dialogue:" in doc


def test_build_ass_very_long_word_no_crash():
    # A 500-char "word" (transcript glitch): must not crash. (It may
    # overflow the frame visually — cosmetic, tracked separately.)
    words = [{"start": 0.0, "end": 2.0, "text": "x" * 500}]
    doc = build_ass(words, "pop")
    assert "Dialogue:" in doc and "x" * 500 in doc


def test_build_ass_unicode_emoji_rtl_no_crash():
    words = [
        {"start": 0.0, "end": 0.5, "text": "hello 🎮🔥"},
        {"start": 0.6, "end": 1.1, "text": "مرحبا بالعالم"},
        {"start": 1.2, "end": 1.7, "text": "日本語テスト"},
    ]
    doc = build_ass(words, "karaoke")
    assert "🎮" in doc and "مرحبا" in doc and "日本語" in doc


def test_build_ass_overlapping_timestamps_no_crash():
    words = [
        {"start": 0.0, "end": 1.0, "text": "first"},
        {"start": 0.5, "end": 1.5, "text": "second"},
    ]
    doc = build_ass(words, "minimal")
    assert doc.count("Dialogue:") == 1  # grouped, no crash


# --- silence/cut-planning edge cases ---

def test_detect_speech_regions_empty_pcm():
    import numpy as np
    assert detect_speech_regions(np.array([], dtype=np.float32)) == []


def test_build_select_expr_empty_kept():
    # Degenerate but must not crash (pipeline rejects empty kept earlier).
    assert build_select_expr([]) == "between(t,0,0.001)"


def test_remap_words_zero_duration_kept_segment():
    # A kept segment collapsed to zero by 3-decimal rounding must not
    # crash remapping; words inside it are dropped.
    words = [{"start": 1.0, "end": 1.05, "text": "x"}]
    out = remap_words(words, [(1.0, 1.0)])
    assert out == []
    assert build_select_expr([(1.0, 1.0)]) == "between(t,1.000,1.000)"
