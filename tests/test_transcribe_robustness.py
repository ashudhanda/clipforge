"""Robustness regression tests for core/ingest/transcribe.py.

faster-whisper is ALWAYS mocked here — no model downloads, no network.
Covers: stale yt-dlp temp files, CUDA init fallback, corrupt/partial
model cache recovery, silent audio, malformed word objects, and
iteration-time error propagation.
"""

import os
import sys
import types

import pytest

from core.ingest import transcribe

URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
VID = "jNQXAC9IVRw"


# ------------------------------------------------------------- fakes


class _FakeWord:
    def __init__(self, start, end, word):
        self.start = start
        self.end = end
        self.word = word


class _FakeSeg:
    def __init__(self, words):
        self.words = words


class _OkayModel:
    """Succeeds; records init kwargs; returns two clean words."""

    last_kwargs = None

    def __init__(self, *a, **k):
        type(self).last_kwargs = k

    def transcribe(self, path, **k):
        assert os.path.exists(path)
        return (
            [_FakeSeg([_FakeWord(0.0, 0.5, "hello"), _FakeWord(0.5, 1.0, "world")])],
            None,
        )


def _seed_cache(tmp_path, subdir, names):
    """Create cache/<subdir>/audio/ with the given file names (nonzero)."""
    audio_dir = tmp_path / subdir / "audio"
    audio_dir.mkdir(parents=True)
    for name in names:
        (audio_dir / name).write_bytes(b"fake-audio-bytes")
    return str(tmp_path / subdir), audio_dir


def _seed_one(tmp_path, subdir="c"):
    cache, _ = _seed_cache(tmp_path, subdir, [f"audio_{VID}.m4a"])
    return cache


# ------------------------------------------------------------- temp files


def test_is_temp_download():
    assert transcribe._is_temp_download("/c/audio_x.m4a.part")
    assert transcribe._is_temp_download("/c/audio_x.webm.ytdl")
    assert transcribe._is_temp_download("/c/audio_x.mp4.part-Frag7")
    assert transcribe._is_temp_download("/c/audio_x.m4a.temp")
    assert not transcribe._is_temp_download("/c/audio_x.m4a")
    assert not transcribe._is_temp_download("/c/audio_x.partial.m4a")


@pytest.mark.parametrize(
    "temp_name",
    [
        f"audio_{VID}.m4a.part",
        f"audio_{VID}.webm.ytdl",
        f"audio_{VID}.mp4.part-Frag7",
    ],
)
def test_stale_temp_file_not_treated_as_cache_hit(monkeypatch, tmp_path, temp_name):
    """An interrupted download's temp file must not be returned as cached
    audio — download_audio must download fresh instead."""
    cache, audio_dir = _seed_cache(tmp_path, "c", [temp_name])

    calls = []

    class _FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download):
            calls.append(url)
            (audio_dir / f"audio_{VID}.m4a").write_bytes(b"real-audio-bytes")

    monkeypatch.setattr(transcribe, "YoutubeDL", _FakeYDL)
    got = transcribe.download_audio(URL, cache_dir=cache)
    assert calls == [URL]  # a real download happened
    assert got.endswith(f"audio_{VID}.m4a")
    assert not transcribe._is_temp_download(got)


def test_real_file_wins_over_stale_temp(monkeypatch, tmp_path):
    """A completed download next to a stale temp file is used as-is."""
    cache, _ = _seed_cache(tmp_path, "c2", [f"audio_{VID}.m4a", f"audio_{VID}.m4a.part"])

    def boom(*a, **k):
        raise AssertionError("yt-dlp must not run when a real file is cached")

    monkeypatch.setattr(transcribe, "YoutubeDL", boom)
    got = transcribe.download_audio(URL, cache_dir=cache)
    assert got.endswith(f"audio_{VID}.m4a")


# ------------------------------------------------------------- CUDA fallback


class _CudaBoomModel(_OkayModel):
    """Fails model construction on CUDA (broken driver / CPU-only build)."""

    attempts = []

    def __init__(self, *a, **k):
        type(self).attempts.append(k.get("device"))
        if k.get("device") == "cuda":
            raise RuntimeError("CUDA failed with error: no CUDA-capable device")
        super().__init__(*a, **k)


def test_cuda_init_failure_falls_back_to_cpu(monkeypatch, tmp_path):
    """device='auto' picked CUDA but init failed -> retry on CPU, not die."""
    _CudaBoomModel.attempts = []
    _CudaBoomModel.last_kwargs = None
    monkeypatch.setattr(transcribe, "_whisper_model_cls", lambda: _CudaBoomModel)
    monkeypatch.setattr(transcribe, "_pick_device", lambda requested: "cuda")
    purges = []
    monkeypatch.setattr(transcribe, "_purge_model_cache", lambda m: purges.append(m))
    cache = _seed_one(tmp_path, "c3")

    words = transcribe.transcribe_audio(URL, device="auto", cache_dir=cache)

    assert [w["text"] for w in words] == ["hello", "world"]
    assert _CudaBoomModel.attempts == ["cuda", "cpu"]
    assert _CudaBoomModel.last_kwargs["device"] == "cpu"
    assert _CudaBoomModel.last_kwargs["compute_type"] == "int8"
    assert purges == []  # driver issue: no pointless cache purge


def test_explicit_cuda_init_failure_still_raises(monkeypatch, tmp_path):
    """An explicit device='cuda' that can't init must fail loudly, not
    silently fall back (the user asked for CUDA)."""
    _CudaBoomModel.attempts = []
    monkeypatch.setattr(transcribe, "_whisper_model_cls", lambda: _CudaBoomModel)
    cache = _seed_one(tmp_path, "c4")

    with pytest.raises(RuntimeError, match="no CUDA-capable device"):
        transcribe.transcribe_audio(URL, device="cuda", cache_dir=cache)
    assert _CudaBoomModel.attempts == ["cuda"]


# ------------------------------------------------------------- model cache recovery


class _FlakyInitModel(_OkayModel):
    """Fails the first construction, then succeeds (corrupt cache purged)."""

    attempts = 0
    first_error = RuntimeError("Unsupported model binary version")

    def __init__(self, *a, **k):
        type(self).attempts += 1
        if type(self).attempts == 1:
            raise type(self).first_error
        super().__init__(*a, **k)


def test_corrupt_model_cache_purged_and_retried(monkeypatch, tmp_path):
    _FlakyInitModel.attempts = 0
    _FlakyInitModel.first_error = RuntimeError(
        "Unsupported model binary version. This usually means the model "
        "was generated by a later version of CTranslate2."
    )
    monkeypatch.setattr(transcribe, "_whisper_model_cls", lambda: _FlakyInitModel)
    purges = []
    monkeypatch.setattr(
        transcribe, "_purge_model_cache", lambda m: purges.append(m) or True
    )
    cache = _seed_one(tmp_path, "c5")

    words = transcribe.transcribe_audio(URL, cache_dir=cache)

    assert [w["text"] for w in words] == ["hello", "world"]
    assert _FlakyInitModel.attempts == 2
    assert purges == ["small"]  # default model, purged exactly once


def test_transient_download_error_retried_after_purge(monkeypatch, tmp_path):
    """Interrupted first-run download -> purge + one retry, then success."""
    _FlakyInitModel.attempts = 0
    _FlakyInitModel.first_error = ConnectionError(
        "HTTPSConnectionPool: Failed to establish a new connection"
    )
    monkeypatch.setattr(transcribe, "_whisper_model_cls", lambda: _FlakyInitModel)
    purges = []
    monkeypatch.setattr(
        transcribe, "_purge_model_cache", lambda m: purges.append(m) or True
    )
    cache = _seed_one(tmp_path, "c6")

    words = transcribe.transcribe_audio(URL, cache_dir=cache)

    assert [w["text"] for w in words] == ["hello", "world"]
    assert _FlakyInitModel.attempts == 2
    assert purges == ["small"]


def test_permanent_model_error_not_retried(monkeypatch, tmp_path):
    """Unknown model name (ValueError) must raise immediately — purging and
    re-downloading can never fix it."""

    class _BadNameModel(_OkayModel):
        attempts = 0

        def __init__(self, *a, **k):
            type(self).attempts += 1
            raise ValueError("Invalid model size 'smal', expected one of: tiny, base")

    _BadNameModel.attempts = 0
    monkeypatch.setattr(transcribe, "_whisper_model_cls", lambda: _BadNameModel)

    def boom_purge(m):
        raise AssertionError("purge must not run for permanent errors")

    monkeypatch.setattr(transcribe, "_purge_model_cache", boom_purge)
    cache = _seed_one(tmp_path, "c7")

    with pytest.raises(ValueError, match="Invalid model size"):
        transcribe.transcribe_audio(URL, model="smal", cache_dir=cache)
    assert _BadNameModel.attempts == 1


def test_missing_repo_error_not_retried(monkeypatch, tmp_path):
    """A 404 from the Hub (repo gone/renamed) must raise immediately."""

    class RepositoryNotFoundError(Exception):
        pass

    class _GoneModel(_OkayModel):
        attempts = 0

        def __init__(self, *a, **k):
            type(self).attempts += 1
            raise RepositoryNotFoundError("404 Client Error: Repository Not Found")

    _GoneModel.attempts = 0
    monkeypatch.setattr(transcribe, "_whisper_model_cls", lambda: _GoneModel)

    def boom_purge(m):
        raise AssertionError("purge must not run for 404s")

    monkeypatch.setattr(transcribe, "_purge_model_cache", boom_purge)
    cache = _seed_one(tmp_path, "c8")

    with pytest.raises(RepositoryNotFoundError):
        transcribe.transcribe_audio(URL, cache_dir=cache)
    assert _GoneModel.attempts == 1


def test_model_repo_id_resolution():
    assert transcribe._model_repo_id("small") == "Systran/faster-whisper-small"
    assert transcribe._model_repo_id("tiny") == "Systran/faster-whisper-tiny"
    assert (
        transcribe._model_repo_id("Systran/faster-whisper-large-v3")
        == "Systran/faster-whisper-large-v3"
    )
    assert transcribe._model_repo_id("not-a-real-model") is None


def test_purge_model_cache_removes_snapshot(tmp_path):
    """Real huggingface_hub cache scan on a fake cache dir: the model's
    snapshot is deleted, other models untouched, unknown model -> False.

    NOTE: the two fake repos must use DIFFERENT commit hashes — hub's
    delete_revisions() treats a hash as unique across repos (it removes the
    hash from its to-delete set after the first match), so sharing "abc123"
    made this test flaky depending on frozenset iteration order.
    """
    hub = tmp_path / "hub"
    repo = hub / "models--Systran--faster-whisper-small"
    other = hub / "models--Systran--faster-whisper-tiny"
    for r, commit in ((repo, "abc123"), (other, "def456")):
        blobs = r / "blobs"
        snaps = r / "snapshots" / commit
        refs = r / "refs"
        blobs.mkdir(parents=True)
        snaps.mkdir(parents=True)
        refs.mkdir(parents=True)
        blob = blobs / "deadbeef"
        blob.write_bytes(b"fake-model-bytes")
        (snaps / "model.bin").symlink_to(blob)
        (refs / "main").write_text(commit)

    assert transcribe._purge_model_cache("small", cache_dir=str(hub)) is True
    assert not repo.exists()  # the model's snapshot is gone
    assert (other / "blobs" / "deadbeef").exists()  # other model untouched
    # second purge: nothing left to delete
    assert transcribe._purge_model_cache("small", cache_dir=str(hub)) is False
    # unknown model name: nothing to purge
    assert transcribe._purge_model_cache("not-a-real-model", cache_dir=str(hub)) is False


def test_purge_model_cache_never_raises(tmp_path):
    """A missing/corrupt cache dir must not break the retry path."""
    assert transcribe._purge_model_cache("small", cache_dir=str(tmp_path / "nope")) is False


# ------------------------------------------------------------- transcript edge cases


class _SilentModel(_OkayModel):
    def transcribe(self, path, **k):
        assert os.path.exists(path)
        return [], None


def test_silent_audio_returns_empty_list(monkeypatch, tmp_path):
    """No-speech audio -> [] (callers treat it as a loud 'no transcript',
    never as a silent wrong result)."""
    monkeypatch.setattr(transcribe, "_whisper_model_cls", lambda: _SilentModel)
    cache = _seed_one(tmp_path, "c9")
    assert transcribe.transcribe_audio(URL, cache_dir=cache) == []


class _MessyModel(_OkayModel):
    def transcribe(self, path, **k):
        return (
            [
                _FakeSeg(None),  # words=None
                _FakeSeg(
                    [
                        _FakeWord(0.0, 0.5, None),  # word=None
                        _FakeWord(0.5, 0.4, "backwards"),  # end <= start
                        _FakeWord(1.0, 1.5, "  ok  "),  # padding stripped
                        _FakeWord(2.0, 2.5, "   "),  # whitespace-only
                    ]
                ),
            ],
            None,
        )


def test_malformed_words_tolerated(monkeypatch, tmp_path):
    monkeypatch.setattr(transcribe, "_whisper_model_cls", lambda: _MessyModel)
    cache = _seed_one(tmp_path, "c10")
    words = transcribe.transcribe_audio(URL, cache_dir=cache)
    assert words == [{"start": 1.0, "end": 1.5, "text": "ok"}]


class _IterBoomModel(_OkayModel):
    """Raises while the segment generator is being iterated (decode error
    mid-transcription) — must propagate, never be masked as a VAD issue."""

    def transcribe(self, path, **k):
        def _gen():
            yield _FakeSeg([_FakeWord(0.0, 0.5, "hello")])
            raise RuntimeError("boom during decode")

        return _gen(), None


def test_iteration_errors_propagate(monkeypatch, tmp_path):
    monkeypatch.setattr(transcribe, "_whisper_model_cls", lambda: _IterBoomModel)
    cache = _seed_one(tmp_path, "c11")
    with pytest.raises(RuntimeError, match="boom during decode"):
        transcribe.transcribe_audio(URL, cache_dir=cache)


# ------------------------------------------------------------- download_audio edge cases


def test_download_audio_bad_url_raises():
    with pytest.raises(ValueError, match="could not extract video id"):
        transcribe.download_audio("not a url")


def test_download_audio_raises_when_ydl_produces_no_file(monkeypatch, tmp_path):
    cache = str(tmp_path / "c12")
    os.makedirs(os.path.join(cache, "audio"), exist_ok=True)

    class _NoFileYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download):
            return {}  # downloaded nothing

    monkeypatch.setattr(transcribe, "YoutubeDL", _NoFileYDL)
    with pytest.raises(RuntimeError, match="produced no file"):
        transcribe.download_audio(URL, cache_dir=cache)


# ------------------------------------------------------------- device selection


def test_pick_device(monkeypatch):
    assert transcribe._pick_device("cpu") == "cpu"
    assert transcribe._pick_device("cuda") == "cuda"
    # torch present but no CUDA support object -> cpu
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace())
    assert transcribe._pick_device("auto") == "cpu"
    # torch reports CUDA available -> cuda
    fake_torch = types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: True)
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    assert transcribe._pick_device("auto") == "cuda"
