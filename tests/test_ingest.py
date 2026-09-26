"""Phase 1 ingest-layer tests. Network is mocked everywhere except the
explicitly-marked smoke test (run separately via ``test_smoke.py``).
"""

import os

import pytest

from core.ingest import captions, downloader, transcribe, ytdlp_helper
from core.ingest import ingest as ingest_fn


# ---------------------------------------------------------------- VTT parsing

SAMPLE_VTT = """WEBVTT

00:00.000 --> 00:01.200
Hello <c>world</c>

00:01.200 --> 00:02.400
Hello <c>world</c>

00:02.400 --> 00:04.000
this is a <00:03.000>test &amp; check

1
00:05.000 --> 00:06.500
second cue here
"""


def test_parse_vtt_basic():
    cues = captions.parse_vtt(SAMPLE_VTT)
    # first two cues have identical text -> merged
    assert cues[0] == {"start": 0.0, "end": 2.4, "text": "Hello world"}
    assert cues[1]["text"] == "this is a test & check"
    assert cues[1]["start"] == pytest.approx(2.4)
    assert cues[2] == {"start": 5.0, "end": 6.5, "text": "second cue here"}


def test_parse_vtt_hour_format_and_tags():
    vtt = "WEBVTT\n\n01:02:03.500 --> 01:02:05.000 align:start\n<b>bold</b> text\n"
    cues = captions.parse_vtt(vtt)
    assert len(cues) == 1
    assert cues[0]["start"] == pytest.approx(3723.5)
    assert cues[0]["end"] == pytest.approx(3725.0)
    assert cues[0]["text"] == "bold text"


def test_parse_vtt_empty_and_garbage():
    assert captions.parse_vtt("") == []
    assert captions.parse_vtt("WEBVTT\n\nnot a cue\n") == []
    # end before start is dropped
    assert captions.parse_vtt("WEBVTT\n\n00:05.000 --> 00:04.000\nx\n") == []


# ------------------------------------------------------- video id extraction


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/watch?v=jNQXAC9IVRw", "jNQXAC9IVRw"),
        ("https://youtu.be/jNQXAC9IVRw", "jNQXAC9IVRw"),
        ("https://www.youtube.com/shorts/jNQXAC9IVRw?x=1", "jNQXAC9IVRw"),
        ("https://www.youtube.com/embed/jNQXAC9IVRw", "jNQXAC9IVRw"),
        ("jNQXAC9IVRw", "jNQXAC9IVRw"),
        ("https://www.youtube.com/watch?v=short", None),
        ("", None),
        (None, None),
    ],
)
def test_extract_video_id(url, expected):
    assert ytdlp_helper.extract_video_id(url) == expected


# ------------------------------------------------- impersonation discovery


def test_discover_impersonation_degrades_gracefully(monkeypatch):
    ytdlp_helper.discover_impersonation.cache_clear()

    class FakeYDL:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def _get_available_impersonate_targets(self):
            raise RuntimeError("private api moved")

    monkeypatch.setattr(ytdlp_helper, "YoutubeDL", FakeYDL)
    try:
        assert ytdlp_helper.discover_impersonation() is None
    finally:
        ytdlp_helper.discover_impersonation.cache_clear()

    # empty list -> None
    class FakeYDL2(FakeYDL):
        def _get_available_impersonate_targets(self):
            return []

    monkeypatch.setattr(ytdlp_helper, "YoutubeDL", FakeYDL2)
    try:
        assert ytdlp_helper.discover_impersonation() is None
    finally:
        ytdlp_helper.discover_impersonation.cache_clear()


def test_discover_impersonation_picks_newest_chrome(monkeypatch):
    from yt_dlp.networking.impersonate import ImpersonateTarget

    ytdlp_helper.discover_impersonation.cache_clear()

    class FakeYDL:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def _get_available_impersonate_targets(self):
            return [
                (ImpersonateTarget("chrome", "120"), "curl_cffi"),
                (ImpersonateTarget("chrome", "150"), "curl_cffi"),
                (ImpersonateTarget("firefox", "140"), "curl_cffi"),
            ]

    monkeypatch.setattr(ytdlp_helper, "YoutubeDL", FakeYDL)
    try:
        assert ytdlp_helper.discover_impersonation() == "chrome-150"
    finally:
        ytdlp_helper.discover_impersonation.cache_clear()


# ---------------------------------------------------------- segment merging


def test_merge_segments():
    merged = downloader.merge_segments(
        [(10, 20), (5, 8), (19, 25), (40, 45)], gap=1.0, pad=0.0
    )
    assert merged == [(5.0, 8.0), (10.0, 25.0), (40.0, 45.0)]


def test_merge_segments_gap_and_pad():
    # 8->10 gap is exactly 1.0 with gap=1.0 -> merges; pad 0.5 applied
    merged = downloader.merge_segments([(0, 8), (9, 12)], gap=1.0, pad=0.5)
    assert merged == [(0.0, 12.5)]
    # invalid ranges dropped
    assert downloader.merge_segments([(5, 5), (9, 3)]) == []


# ------------------------------------------------------ captions (mocked)


def test_fetch_captions_nonfatal(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("HTTP 429")

    monkeypatch.setattr(captions, "_download_subtitle_file", boom)
    assert (
        captions.fetch_captions("https://www.youtube.com/watch?v=jNQXAC9IVRw")
        is None
    )


def test_fetch_captions_parses_downloaded_file(monkeypatch, tmp_path):
    vtt = tmp_path / "jNQXAC9IVRw.en.vtt"
    vtt.write_text("WEBVTT\n\n00:00.500 --> 00:02.000\nyay captions\n")

    def fake_dl(video_url, video_id, automatic, languages, tmpdir):
        assert automatic is False  # manual pass first
        return str(vtt)

    monkeypatch.setattr(captions, "_download_subtitle_file", fake_dl)
    cues = captions.fetch_captions("https://www.youtube.com/watch?v=jNQXAC9IVRw")
    assert cues == [{"start": 0.5, "end": 2.0, "text": "yay captions"}]


def test_fetch_captions_falls_back_to_auto(monkeypatch, tmp_path):
    vtt = tmp_path / "jNQXAC9IVRw.en.vtt"
    vtt.write_text("WEBVTT\n\n00:01.000 --> 00:02.000\nauto text\n")
    calls = []

    def fake_dl(video_url, video_id, automatic, languages, tmpdir):
        calls.append(automatic)
        return str(vtt) if automatic else None

    monkeypatch.setattr(captions, "_download_subtitle_file", fake_dl)
    cues = captions.fetch_captions("https://www.youtube.com/watch?v=jNQXAC9IVRw")
    assert calls == [False, True]
    assert cues[0]["text"] == "auto text"


def test_fetch_captions_bad_url():
    assert captions.fetch_captions("not a url") is None


# ----------------------------------------------------- transcribe (mocked)


class _FakeWord:
    def __init__(self, start, end, word):
        self.start, self.end, self.word = start, end, word


class _FakeSeg:
    def __init__(self, words):
        self.words = words


class _FakeModel:
    instances = 0

    def __init__(self, *a, **k):
        type(self).instances += 1

    def transcribe(self, path, **k):
        assert os.path.exists(path)
        segs = [
            _FakeSeg([_FakeWord(0.0, 0.4, " hello "), _FakeWord(0.4, 0.9, "world")]),
            _FakeSeg([_FakeWord(1.0, 1.5, "  ")]),  # whitespace-only -> dropped
        ]
        return segs, None


def test_transcribe_word_output_and_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(transcribe, "_whisper_model_cls", lambda: _FakeModel)

    def boom(*a, **k):
        raise AssertionError("yt-dlp must not run on cache hit")

    monkeypatch.setattr(transcribe, "YoutubeDL", boom)
    url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"

    # seed the cache so no download happens
    cache = tmp_path / "c"
    audio_dir = cache / "audio"
    audio_dir.mkdir(parents=True)
    (audio_dir / "audio_jNQXAC9IVRw.m4a").write_bytes(b"fake-audio-bytes")

    words = transcribe.transcribe_audio(url, cache_dir=str(cache))
    assert words == [
        {"start": 0.0, "end": 0.4, "text": "hello"},
        {"start": 0.4, "end": 0.9, "text": "world"},
    ]


class _VadBoomModel(_FakeModel):
    """Raises an onnx-style error when VAD is on (missing silero asset)."""

    calls = []

    def transcribe(self, path, **k):
        type(self).calls.append(k.get("vad_filter"))
        if k.get("vad_filter"):
            raise RuntimeError(
                "[ONNXRuntimeError] : 3 : NO_SUCHFILE : "
                "Load model from C:\\app\\_internal\\faster_whisper\\assets\\silero_vad_v6.onnx"
            )
        return super().transcribe(path, **k)


def test_transcribe_falls_back_when_vad_asset_missing(monkeypatch, tmp_path):
    """v0.1.6: missing silero_vad_v6.onnx must not fail the job — VAD is
    retried off instead of raising."""
    _VadBoomModel.calls = []
    monkeypatch.setattr(transcribe, "_whisper_model_cls", lambda: _VadBoomModel)
    url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    cache = tmp_path / "c3"
    audio_dir = cache / "audio"
    audio_dir.mkdir(parents=True)
    (audio_dir / "audio_jNQXAC9IVRw.m4a").write_bytes(b"fake-audio-bytes")

    words = transcribe.transcribe_audio(url, cache_dir=str(cache))
    assert [w["text"] for w in words] == ["hello", "world"]
    assert _VadBoomModel.calls == [True, False]


def test_transcribe_non_vad_errors_still_raise(monkeypatch, tmp_path):
    """v0.1.6: real transcription errors are not masked by the VAD fallback."""

    class _BoomModel(_FakeModel):
        def transcribe(self, path, **k):
            raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(transcribe, "_whisper_model_cls", lambda: _BoomModel)
    url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    cache = tmp_path / "c4"
    audio_dir = cache / "audio"
    audio_dir.mkdir(parents=True)
    (audio_dir / "audio_jNQXAC9IVRw.m4a").write_bytes(b"fake-audio-bytes")

    import pytest

    with pytest.raises(RuntimeError, match="CUDA out of memory"):
        transcribe.transcribe_audio(url, cache_dir=str(cache))


def test_download_audio_cache_hit_skips_ydl(monkeypatch, tmp_path):
    cache = tmp_path / "c2"
    audio_dir = cache / "audio"
    audio_dir.mkdir(parents=True)
    seeded = audio_dir / "audio_jNQXAC9IVRw.webm"
    seeded.write_bytes(b"x" * 100)

    def boom(*a, **k):
        raise AssertionError("yt-dlp must not run on cache hit")

    monkeypatch.setattr(transcribe, "YoutubeDL", boom)
    got = transcribe.download_audio(
        "https://www.youtube.com/watch?v=jNQXAC9IVRw", cache_dir=str(cache)
    )
    assert got == str(seeded)


# ----------------------------------------------------- downloader (mocked)


def test_download_ranges_merges_and_caches(monkeypatch, tmp_path):
    calls = []

    def fake_single(video_url, start, end, out_path, format):
        calls.append((round(start, 2), round(end, 2)))
        with open(out_path, "wb") as fh:
            fh.write(b"fake-video")
        return out_path

    monkeypatch.setattr(downloader, "_download_single_range", fake_single)
    cache = str(tmp_path / "r")
    url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"

    paths = downloader.download_ranges(url, [(2, 8), (7, 12)], cache_dir=cache)
    # (2,8)+(7,12) overlap -> one merged range with pad 0.5 -> (1.5, 12.5)
    assert calls == [(1.5, 12.5)]
    assert len(paths) == 1 and os.path.getsize(paths[0]) > 0

    # second call: cache hit, no new download
    paths2 = downloader.download_ranges(url, [(2, 8), (7, 12)], cache_dir=cache)
    assert calls == [(1.5, 12.5)]
    assert paths2 == paths


def test_download_ranges_rejects_bad_url():
    with pytest.raises(ValueError):
        downloader.download_ranges("not a url", [(1, 2)])


def _fake_ydl_factory(created_bytes=b"fake-video"):
    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download):
            outtmpl = self.opts["outtmpl"]
            path = outtmpl.replace(".%(ext)s", ".mp4")
            with open(path, "wb") as fh:
                fh.write(created_bytes)

    return FakeYDL


def test_single_range_rejects_truncated_download(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader, "YoutubeDL", _fake_ydl_factory())
    monkeypatch.setattr(downloader, "_probe_duration", lambda p: 1.0)
    out = str(tmp_path / "clip.mp4")
    with pytest.raises(RuntimeError, match="truncated"):
        downloader._download_single_range("http://x/y.mp4", 0.0, 10.0, out, "b")


def test_single_range_accepts_healthy_download(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader, "YoutubeDL", _fake_ydl_factory())
    monkeypatch.setattr(downloader, "_probe_duration", lambda p: 9.5)
    out = str(tmp_path / "clip.mp4")
    got = downloader._download_single_range("http://x/y.mp4", 0.0, 10.0, out, "b")
    assert got == out


def test_single_range_skips_check_for_short_ranges(monkeypatch, tmp_path):
    # ranges < 4s skip the duration check (keyframe quantization noise)
    monkeypatch.setattr(downloader, "YoutubeDL", _fake_ydl_factory())
    probed = []
    monkeypatch.setattr(downloader, "_probe_duration", lambda p: probed.append(p) or 0.1)
    out = str(tmp_path / "clip.mp4")
    downloader._download_single_range("http://x/y.mp4", 0.0, 2.0, out, "b")
    assert probed == []


# ------------------------------------------------------------ ingest glue


def test_ingest_captions_first_no_media(monkeypatch):
    monkeypatch.setattr(
        "core.ingest.fetch_captions",
        lambda url, languages=("en",): [{"start": 0, "end": 1, "text": "hi"}],
    )

    def boom(*a, **k):
        raise AssertionError("must not download when segments=None")

    monkeypatch.setattr("core.ingest.download_ranges", boom)
    monkeypatch.setattr("core.ingest.transcribe_audio", boom)
    out = ingest_fn("https://www.youtube.com/watch?v=jNQXAC9IVRw")
    assert out["transcript"][0]["text"] == "hi"
    assert out["media_paths"] == []
    assert out["source"] == "captions"
    assert out["video_id"] == "jNQXAC9IVRw"


def test_ingest_falls_back_to_whisper_and_downloads(monkeypatch):
    monkeypatch.setattr("core.ingest.fetch_captions", lambda *a, **k: None)
    monkeypatch.setattr(
        "core.ingest.transcribe_audio",
        lambda *a, **k: [{"start": 0, "end": 1, "text": "w"}],
    )
    seen = {}

    def fake_dl(url, segments, cache_dir=None, **k):
        seen["segments"] = segments
        return ["p1.mp4", "p2.mp4"]

    monkeypatch.setattr("core.ingest.download_ranges", fake_dl)
    out = ingest_fn(
        "https://www.youtube.com/watch?v=jNQXAC9IVRw", segments=[(1, 2), (5, 9)]
    )
    assert out["source"] == "whisper"
    assert out["media_paths"] == ["p1.mp4", "p2.mp4"]
    assert seen["segments"] == [(1, 2), (5, 9)]


def test_base_opts_points_ytdlp_at_bundled_ffmpeg(monkeypatch):
    from core.ingest import ytdlp_helper
    monkeypatch.setattr("core.paths.ffmpeg_dir", lambda: "/fake/bindir")
    opts = ytdlp_helper.base_opts()
    assert opts["ffmpeg_location"] == "/fake/bindir"


def test_base_opts_no_ffmpeg_location_when_none_found(monkeypatch):
    from core.ingest import ytdlp_helper
    monkeypatch.setattr("core.paths.ffmpeg_dir", lambda: None)
    opts = ytdlp_helper.base_opts()
    assert "ffmpeg_location" not in opts
