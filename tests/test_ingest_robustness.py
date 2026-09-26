"""Robustness / failure-mode regression tests for the ingest layer.

Network is stubbed everywhere: yt-dlp, ffprobe and faster-whisper never run
for real. Each test pins the behavior of one concrete failure mode found in
the 2026-09-26 ingest bug-hunt — either the fixed behavior or the
deliberately-loud failure that must not become silent.
"""

import os

import pytest
from yt_dlp.utils import DownloadError

from core.ingest import captions, downloader
from core.ingest import ingest as ingest_fn

VID = "jNQXAC9IVRw"
URL = f"https://www.youtube.com/watch?v={VID}"


def _fake_ydl_factory(*, write=None, exc=None):
    """Build a stub YoutubeDL.

    ``write``: ``{ext: bytes}`` files materialized from the ``outtmpl``
    option (``{"mp4.part": b"..." }`` simulates a crashed download that left
    only its resume file). ``exc``: raised from ``extract_info`` instead.
    """

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download):
            if exc is not None:
                raise exc
            outtmpl = self.opts["outtmpl"]
            for ext, data in (write or {}).items():
                path = outtmpl.replace(".%(ext)s", f".{ext}")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as fh:
                    fh.write(data)

    return FakeYDL


# --------------------------------- partial-download cache poisoning (P0)


def test_truncated_range_failure_removes_partial_file(monkeypatch, tmp_path):
    """A truncated range must not leave bytes behind: the next run would
    otherwise treat them as a valid cache hit and serve the broken clip
    forever."""
    monkeypatch.setattr(
        downloader, "YoutubeDL", _fake_ydl_factory(write={"mp4": b"fake-video"})
    )
    monkeypatch.setattr(downloader, "_probe_duration", lambda p: 1.0)
    out = str(tmp_path / "clip.mp4")
    with pytest.raises(RuntimeError, match="truncated"):
        downloader._download_single_range(URL, 0.0, 10.0, out, "b")
    assert not os.path.exists(out)


def test_failed_range_download_cleans_up_leftovers(monkeypatch, tmp_path):
    """A mid-download exception must clean up stale bytes (previous crash
    leftovers), so a retry starts from a clean slate."""
    monkeypatch.setattr(
        downloader,
        "YoutubeDL",
        _fake_ydl_factory(exc=DownloadError("HTTP Error 403: Forbidden")),
    )
    out = str(tmp_path / "clip.mp4")
    part = out + ".part"
    for stale in (out, part):
        with open(stale, "wb") as fh:
            fh.write(b"stale-bytes")
    with pytest.raises(DownloadError, match="403"):
        downloader._download_single_range(URL, 0.0, 10.0, out, "b")
    assert not os.path.exists(out)
    assert not os.path.exists(part)


def test_part_file_never_accepted_as_finished_clip(monkeypatch, tmp_path):
    """A lone ``.part`` resume file is not a finished clip: it must raise
    (loudly) and be cleaned up, not returned as a successful download."""
    monkeypatch.setattr(
        downloader, "YoutubeDL", _fake_ydl_factory(write={"mp4.part": b"partial"})
    )
    out = str(tmp_path / "clip.mp4")
    with pytest.raises(RuntimeError, match="produced no file"):
        downloader._download_single_range(URL, 0.0, 10.0, out, "b")
    assert not os.path.exists(out + ".part")


def test_truncated_range_is_retried_on_next_run(monkeypatch, tmp_path):
    """End-to-end through ``download_ranges``: after a truncation failure,
    the follow-up run re-downloads instead of hitting a poisoned cache."""
    attempts = []

    def flaky_single(video_url, start, end, out_path, format):
        attempts.append((round(start, 2), round(end, 2)))
        if len(attempts) == 1:
            # first attempt: write a truncated file, then raise like the
            # real truncation guard does (after cleaning up)
            with open(out_path, "wb") as fh:
                fh.write(b"truncated")
            downloader._remove_partial(out_path)
            raise RuntimeError("range download truncated for 1.5-12.5")
        with open(out_path, "wb") as fh:
            fh.write(b"healthy-video")
        return out_path

    monkeypatch.setattr(downloader, "_download_single_range", flaky_single)
    cache = str(tmp_path / "r")
    with pytest.raises(RuntimeError, match="truncated"):
        downloader.download_ranges(URL, [(2, 8), (7, 12)], cache_dir=cache)
    paths = downloader.download_ranges(URL, [(2, 8), (7, 12)], cache_dir=cache)
    assert len(attempts) == 2  # retried, not served from a poisoned cache
    assert len(paths) == 1 and os.path.getsize(paths[0]) > 0


# --------------------------------------- outtmpl / glob path handling (P2)


def test_media_stem_only_strips_trailing_mp4():
    assert downloader._media_stem("/tmp/a.mp4/x/clip.mp4") == "/tmp/a.mp4/x/clip"
    assert downloader._media_stem("/tmp/clip.mp4") == "/tmp/clip"
    assert downloader._media_stem("/tmp/clip.webm") == "/tmp/clip.webm"


def test_outtmpl_ignores_mp4_in_parent_dir(monkeypatch, tmp_path):
    """A cache dir containing ``".mp4"`` must not corrupt the yt-dlp output
    template (the old ``str.replace`` rewrote the directory too)."""
    weird = tmp_path / "my.mp4.cache"
    weird.mkdir()
    seen = {}

    class FakeYDL:
        def __init__(self, opts):
            seen["outtmpl"] = opts["outtmpl"]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download):
            path = seen["outtmpl"].replace(".%(ext)s", ".mp4")
            with open(path, "wb") as fh:
                fh.write(b"v")

    monkeypatch.setattr(downloader, "YoutubeDL", FakeYDL)
    out = str(weird / "clip.mp4")
    # 2s range skips the ffprobe duration check; no ffprobe needed
    got = downloader._download_single_range(URL, 0.0, 2.0, out, "b")
    assert seen["outtmpl"] == str(weird / "clip.%(ext)s")
    assert got == out and os.path.getsize(out) > 0


# --------------------------------- extractor failures stay loud / non-fatal


@pytest.mark.parametrize(
    "msg",
    [
        "Private video",
        "This video is unavailable",
        "Video unavailable. This video is no longer available",
        "Premieres in 2 days",  # upcoming livestream / premiere
        "Sign in to confirm you're not a bot",  # YouTube bot-check
        "HTTP Error 429: Too Many Requests",  # IP rate-limit
    ],
)
def test_download_ranges_propagates_extractor_errors(monkeypatch, tmp_path, msg):
    """Media-download failures (private/deleted/region-blocked/upcoming/
    bot-checked videos, retry exhaustion) must raise loudly — never return
    an empty or fake path list."""
    monkeypatch.setattr(
        downloader, "YoutubeDL", _fake_ydl_factory(exc=DownloadError(msg))
    )
    with pytest.raises(DownloadError):
        downloader.download_ranges(URL, [(1, 2)], cache_dir=str(tmp_path))


@pytest.mark.parametrize(
    "msg",
    [
        "Private video",
        "This video is unavailable",
        "Premieres in 2 days",
        "Sign in to confirm you're not a bot",
        "HTTP Error 429: Too Many Requests",
    ],
)
def test_fetch_captions_none_on_extractor_errors(monkeypatch, msg):
    """Caption fetch stays non-fatal by design: any extractor/network
    failure returns None so ingest can fall back to Whisper."""
    monkeypatch.setattr(
        captions, "YoutubeDL", _fake_ydl_factory(exc=DownloadError(msg))
    )
    assert captions.fetch_captions(URL) is None


def test_fetch_captions_empty_track_returns_none(monkeypatch, tmp_path):
    """A downloaded-but-empty subtitle track (zero cues) is not a usable
    transcript: fetch must return None so the Whisper fallback runs."""
    vtt = tmp_path / f"{VID}.en.vtt"
    vtt.write_text("WEBVTT\n\n")
    monkeypatch.setattr(
        captions, "_download_subtitle_file", lambda *a, **k: str(vtt)
    )
    assert captions.fetch_captions(URL) is None


def test_download_ranges_segment_beyond_duration_raises(monkeypatch, tmp_path):
    """A segment far past the video's end yields nothing usable: loud
    RuntimeError, not a silent empty/garbage clip."""
    monkeypatch.setattr(
        downloader, "YoutubeDL", _fake_ydl_factory(write={"mp4": b"tiny"})
    )
    monkeypatch.setattr(downloader, "_probe_duration", lambda p: 0.2)
    with pytest.raises(RuntimeError, match="truncated"):
        downloader.download_ranges(URL, [(10000, 10010)], cache_dir=str(tmp_path))


def test_download_ranges_unwritable_cache_dir_raises(tmp_path):
    """An unwritable/misconfigured cache dir raises OSError loudly instead
    of silently dropping downloads."""
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"x")  # a file where the cache dir should be
    with pytest.raises(OSError):
        downloader.download_ranges(URL, [(1, 2)], cache_dir=str(blocker))


# ------------------------------------------------------- ingest glue


def test_ingest_forwards_language_to_whisper(monkeypatch):
    """The ``languages`` parameter must reach the Whisper fallback — before
    this fix it was silently ignored and transcription always ran in
    English."""
    monkeypatch.setattr("core.ingest.fetch_captions", lambda *a, **k: None)
    seen = {}

    def fake_tx(video_url, **kwargs):
        seen.update(kwargs)
        return [{"start": 0.0, "end": 1.0, "text": "hola"}]

    monkeypatch.setattr("core.ingest.transcribe_audio", fake_tx)
    out = ingest_fn(URL, languages=("es",))
    assert seen["language"] == "es"
    assert out["source"] == "whisper"


def test_ingest_whisper_language_defaults_to_english(monkeypatch):
    monkeypatch.setattr("core.ingest.fetch_captions", lambda *a, **k: None)
    seen = {}

    def fake_tx(video_url, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr("core.ingest.transcribe_audio", fake_tx)
    ingest_fn(URL)
    assert seen["language"] == "en"


def test_ingest_bad_url_raises_loudly():
    """A malformed URL raises ValueError (from the audio-download path)
    instead of returning a fake ``video_id: unknown`` result."""
    with pytest.raises(ValueError, match="could not extract video id"):
        ingest_fn("not a url")


def test_ingest_total_transcript_failure_raises(monkeypatch):
    """No captions AND transcription failure: ingest raises loudly rather
    than returning an empty transcript masquerading as success."""
    monkeypatch.setattr("core.ingest.fetch_captions", lambda *a, **k: None)

    def boom(*a, **k):
        raise RuntimeError("audio download failed: HTTP Error 403")

    monkeypatch.setattr("core.ingest.transcribe_audio", boom)
    with pytest.raises(RuntimeError, match="audio download failed"):
        ingest_fn(URL)


def test_ingest_empty_segments_downloads_nothing(monkeypatch):
    monkeypatch.setattr(
        "core.ingest.fetch_captions",
        lambda *a, **k: [{"start": 0, "end": 1, "text": "hi"}],
    )

    def boom(*a, **k):
        raise AssertionError("no download expected for segments=[]")

    monkeypatch.setattr("core.ingest.download_ranges", boom)
    out = ingest_fn(URL, segments=[])
    assert out["media_paths"] == []


# ------------------------------------------------------- segment merging


def test_merge_segments_drops_nan_and_zero_length():
    nan = float("nan")
    assert downloader.merge_segments(
        [(nan, 5), (3, 3), (1, 2)], gap=0.0, pad=0.0
    ) == [(1.0, 2.0)]
