"""Tests for core.edit — all synthetic media, zero network.

Fixture: 12 s 1280x720 testsrc2 + 440 Hz tone with two silent gaps
(3-5 s and 8-10 s), so speech regions are ~[0,3], [5,8], [10,12].
"""

import os
import shutil
import subprocess

import pytest

from core.edit import (
    CAPTION_STYLES,
    ass_filter,
    build_ass,
    build_select_expr,
    build_short,
    compute_crop,
    detect_speech_regions,
    displayed_dims,
    decode_mono_pcm,
    group_words,
    is_full_bleed,
    kept_duration,
    loudnorm_filter,
    plan_cuts,
    remap_time,
    remap_words,
)

TMP = "/tmp/cf2_edit_tests"
SRC = os.path.join(TMP, "src.mp4")


def _ffmpeg():
    return shutil.which("ffmpeg")


@pytest.fixture(scope="module")
def src_video():
    os.makedirs(TMP, exist_ok=True)
    if not os.path.exists(SRC):
        cmd = [
            _ffmpeg(), "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=duration=12:size=1280x720:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
            "-af", "volume='if(between(t,3,5)+between(t,8,10),0,1)':eval=frame",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
            "-y", SRC,
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert proc.returncode == 0, proc.stderr.decode()[:300]
    return SRC


def _words():
    """Synthetic word transcript over the speech regions (clip-relative)."""
    words, i = [], 0
    for rs, re in [(0.2, 2.8), (5.2, 7.8), (10.2, 11.8)]:
        t = rs
        while t + 0.3 <= re:
            text = "um" if (rs == 5.2 and i == 3) else f"word{i}"
            words.append({"start": round(t, 2), "end": round(t + 0.3, 2),
                          "text": text})
            i += 1
            t += 0.35
    return words


# --- silence.py ---

def test_displayed_dims(src_video):
    assert displayed_dims(src_video) == (1280, 720)


def test_detect_speech_regions(src_video):
    pcm = decode_mono_pcm(src_video, 0.0, 12.0)
    regions = detect_speech_regions(pcm)
    assert len(regions) == 3
    for (s, e), (es, ee) in zip(regions, [(0, 3), (5, 8), (10, 12)]):
        assert abs(s - es) < 0.3 and abs(e - ee) < 0.3


def test_plan_cuts(src_video):
    plan = plan_cuts(src_video, 0.0, 12.0)
    assert len(plan.kept) == 3
    total_kept = kept_duration(plan.kept)
    # 12 s minus ~2.8 s of cut silence (rolls keep ~0.48 s around each gap)
    assert 8.5 < total_kept < 10.0
    assert 0.7 < plan.speech_ratio < 0.85
    # removed gaps sit where the silence was
    assert any(3.0 < s < 3.6 and 4.4 < e < 5.0 for s, e in plan.removed)
    assert any(8.0 < s < 8.6 and 9.4 < e < 10.0 for s, e in plan.removed)


def test_plan_cuts_filler_words(src_video):
    words = [{"start": 1.0, "end": 1.3, "text": "um"},
             {"start": 1.5, "end": 1.8, "text": "hello"}]
    plan = plan_cuts(src_video, 0.0, 12.0, words=words)
    assert any(s <= 1.0 and e >= 1.3 for s, e in plan.removed)


def test_plan_cuts_all_silence(src_video):
    plan = plan_cuts(src_video, 3.2, 4.8)  # inside a silent gap
    assert plan.kept == []  # graceful, no crash


def test_build_select_expr():
    expr = build_select_expr([(0.0, 3.3), (4.7, 8.3)])
    assert expr == "between(t,0.000,3.300)+between(t,4.700,8.300)"


def test_remap_time_and_words():
    kept = [(0.0, 3.0), (5.0, 8.0)]
    assert remap_time(1.0, kept) == pytest.approx(1.0)
    assert remap_time(6.0, kept) == pytest.approx(4.0)
    assert remap_time(4.0, kept) == pytest.approx(3.0)  # inside cut -> snap
    words = [{"start": 1.0, "end": 1.5, "text": "hi"},
             {"start": 3.5, "end": 4.5, "text": "gone"}]  # fully inside cut
    out = remap_words(words, kept)
    assert len(out) == 1 and out[0]["text"] == "hi"
    assert out[0]["start"] == pytest.approx(1.0)


# --- crop.py ---

def test_compute_crop_centered(src_video):
    c = compute_crop(src_video, 0.0, 12.0)
    # 1280x720 -> largest exact 9:16 window on the 18px grid: 396x704
    assert (c["w"], c["h"]) == (396, 704)
    assert c["x"] == 442 and c["y"] == 8
    assert is_full_bleed(c)  # exactly 9:16, never letterboxed


def test_compute_crop_never_letterboxes():
    # Portrait source narrower than 9:16 -> cut top/bottom, still full-bleed.
    fake = {"w": 0, "h": 0, "x": 0, "y": 0, "target": [1080, 1920]}
    fake.update({"w": 720, "h": 1280})
    assert is_full_bleed(fake)


# --- captions.py ---

def test_caption_styles_known():
    assert set(CAPTION_STYLES) == {
        "karaoke", "pop", "minimal",
        "hormozi", "beast", "neon", "wordbox", "stroke", "typewriter",
    }


def test_group_words():
    words = [{"start": i * 0.4, "end": i * 0.4 + 0.3, "text": f"w{i}"}
             for i in range(10)]
    groups = group_words(words, max_words=4)
    assert [len(g) for g in groups] == [4, 4, 2]


def test_group_words_sentence_break():
    words = [{"start": 0.0, "end": 0.3, "text": "hello."},
             {"start": 0.4, "end": 0.7, "text": "world"}]
    groups = group_words(words, max_words=4)
    assert len(groups) == 2


def test_build_ass_all_styles():
    words = _words()[:8]
    for style in CAPTION_STYLES:
        doc = build_ass(words, style)
        assert "[Events]" in doc
        assert "PlayResX: 1080" in doc and "PlayResY: 1920" in doc
        if style == "wordbox":
            # wordbox renders via two alternating box styles
            assert "Style: wordbox_a," in doc and "Style: wordbox_b," in doc
            assert ",wordbox_a," in doc or ",wordbox_b," in doc
        else:
            assert f"Style: {style}," in doc  # style defined in header
            assert f",{style}," in doc  # ...and referenced by events
    karaoke = build_ass(words, "karaoke")
    assert "{\\k" in karaoke  # per-word sweep tags
    pop = build_ass(words, "pop")
    assert "{\\fad(60,60)}" in pop
    minimal = build_ass(words, "minimal")
    assert "word0" in minimal


def test_build_ass_escapes():
    words = [{"start": 0.0, "end": 0.5, "text": "a{b}\\c"}]
    doc = build_ass(words, "minimal")
    assert "a\\{b\\}\\\\c" in doc


def test_build_ass_clamps_tiny_words():
    words = [{"start": 1.0, "end": 1.01, "text": "x"}]
    doc = build_ass(words, "karaoke")
    # word clamped to >= 0.05 s, group gets +0.10 s tail padding
    assert "0:00:01.15" in doc


def test_build_ass_bad_style():
    with pytest.raises(ValueError):
        build_ass([], "definitely-not-a-style")


def test_ass_filter_escaping():
    f = ass_filter("/tmp/my dir/cap's.ass")
    assert f.startswith("ass='") and "\\'" in f


# --- loudness.py ---

def test_loudnorm_filter():
    f = loudnorm_filter()
    assert "loudnorm=I=-14" in f and "alimiter" in f


# --- pipeline.py ---

def _probe(path):
    import json
    ffprobe = shutil.which("ffprobe")
    cmd = [ffprobe, "-hide_banner", "-loglevel", "error",
           "-show_entries", "stream=width,height,codec_type:format=duration",
           "-of", "json", path]
    info = json.loads(subprocess.run(cmd, stdout=subprocess.PIPE).stdout.decode() or "{}")
    out = {"video": None, "audio": False, "duration": 0.0}
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            out["video"] = (int(s["width"]), int(s["height"]))
        elif s.get("codec_type") == "audio":
            out["audio"] = True
    out["duration"] = float(info.get("format", {}).get("duration", 0) or 0)
    return out


def test_build_short_end_to_end(src_video):
    out = os.path.join(TMP, "short_e2e.mp4")
    clip = {"start": 0.0, "end": 12.0, "transcript": _words(),
            "caption_style": "karaoke"}
    build_short(src_video, clip, out, preset="veryfast")
    info = _probe(out)
    assert info["video"] == (1080, 1920)  # full-bleed 9:16
    assert info["audio"] is True
    plan = plan_cuts(src_video, 0.0, 12.0, words=_words())
    assert abs(info["duration"] - kept_duration(plan.kept)) < 1.0


def test_build_short_rejects_bad_style(src_video):
    with pytest.raises(ValueError):
        build_short(src_video,
                    {"start": 0, "end": 2, "transcript": [], "caption_style": "definitely-not-a-style"},
                    os.path.join(TMP, "x.mp4"))


def test_build_short_missing_source():
    with pytest.raises(FileNotFoundError):
        build_short("/tmp/does-not-exist.mp4",
                    {"start": 0, "end": 2, "transcript": []},
                    os.path.join(TMP, "x.mp4"))
