"""Whisper fallback: audio-only download + local faster-whisper transcription.

Own implementation. Used only when :mod:`captions` finds no usable YouTube
subtitles. Audio is cached by video ID so repeat runs never re-download.
"""

from __future__ import annotations

import glob
import logging
import os
from typing import Optional

from yt_dlp import YoutubeDL

from .ytdlp_helper import base_opts, extract_video_id

log = logging.getLogger(__name__)

DEFAULT_CACHE_SUBDIR = "audio"

# yt-dlp temp extensions: an interrupted download leaves these behind.
# They must never count as a completed download.
_TEMP_SUFFIXES = (".part", ".ytdl", ".temp")


def _is_temp_download(path: str) -> bool:
    """True for yt-dlp's in-progress/leftover temp files."""
    name = os.path.basename(path)
    return name.endswith(_TEMP_SUFFIXES) or ".part-" in name


def _cache_dir(cache_dir: Optional[str]) -> str:
    base = cache_dir or os.path.join(os.path.expanduser("~"), ".cache", "clipforge")
    path = os.path.join(base, DEFAULT_CACHE_SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def _find_cached_audio(cache: str, video_id: str) -> Optional[str]:
    matches = sorted(glob.glob(os.path.join(cache, f"audio_{video_id}.*")))
    for path in matches:
        if _is_temp_download(path):
            # Interrupted download: a partial/corrupt file. Ignore it so a
            # fresh download happens instead of transcribing garbage.
            log.debug("ignoring incomplete download %s", path)
            continue
        if os.path.getsize(path) > 0:
            return path
    return None


def download_audio(video_url: str, cache_dir: Optional[str] = None) -> str:
    """Download best-audio only; return the file path (cached by video ID)."""
    video_id = extract_video_id(video_url)
    if not video_id:
        raise ValueError(f"could not extract video id from {video_url!r}")
    cache = _cache_dir(cache_dir)
    hit = _find_cached_audio(cache, video_id)
    if hit:
        log.info("audio cache hit for %s", video_id)
        return hit

    opts = base_opts(
        {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(cache, f"audio_{video_id}.%(ext)s"),
        }
    )
    with YoutubeDL(opts) as ydl:
        ydl.extract_info(video_url, download=True)

    hit = _find_cached_audio(cache, video_id)
    if not hit:
        raise RuntimeError(f"audio download produced no file for {video_id}")
    return hit


def _looks_like_vad_failure(exc: Exception) -> bool:
    """True when the exception is the Silero VAD / onnxruntime model failing
    to load (e.g. the ``silero_vad_v6.onnx`` asset missing from a packaged
    build). VAD only improves word timestamps; it must never kill a job."""
    msg = f"{type(exc).__name__}: {exc}".lower()
    return (
        "onnx" in msg
        or "no_suchfile" in msg
        or "silero" in msg
        or "vad" in msg
    )


def _whisper_model_cls():
    """Indirection for the faster-whisper class (lazy import, test-seam)."""
    from faster_whisper import WhisperModel

    return WhisperModel


def _pick_device(requested: str) -> str:
    if requested != "auto":
        return requested
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def _looks_like_cuda_failure(exc: Exception) -> bool:
    """True when model construction failed because CUDA is unusable
    (broken driver, CPU-only ctranslate2 build, OOM at load)."""
    msg = f"{type(exc).__name__}: {exc}".lower()
    return any(
        token in msg
        for token in (
            "cuda",
            "cublas",
            "cudnn",
            "nvml",
            "nvidia",
            "gpu",
            "driver",
            "out of memory",
            "outofmemory",
        )
    )


def _model_repo_id(model: str) -> Optional[str]:
    """Resolve a faster-whisper model name to its Hugging Face repo id,
    using the same mapping faster-whisper itself uses."""
    if "/" in model:
        return model
    try:
        from faster_whisper.utils import _MODELS

        return _MODELS.get(model)
    except Exception:
        return None


def _purge_model_cache(model: str, cache_dir: Optional[str] = None) -> bool:
    """Best-effort delete of the cached HF snapshot for this model so a
    corrupt or interrupted download is fetched fresh on retry. Only ever
    touches that one model's cache entry. Never raises."""
    try:
        from huggingface_hub.utils import scan_cache_dir

        repo_id = _model_repo_id(model)
        if not repo_id:
            return False
        info = scan_cache_dir(cache_dir)
        purged = False
        for repo in info.repos:
            if repo.repo_id == repo_id:
                # delete_revisions only *plans* the deletion; execute() does it.
                info.delete_revisions(
                    *[r.commit_hash for r in repo.revisions]
                ).execute()
                purged = True
        if purged:
            log.info("purged model cache for %r", repo_id)
        return purged
    except Exception as exc:
        log.debug("model cache purge failed: %s", exc)
        return False


def _is_permanent_model_error(exc: Exception) -> bool:
    """True for init errors a fresh download can never fix: unknown model
    name, bad device/compute_type, repo gone. Retrying those would just
    burn a full model re-download before failing identically."""
    if isinstance(exc, ValueError):
        return True
    return type(exc).__name__ in (
        "RepositoryNotFoundError",
        "GatedRepoError",
        "HFValidationError",
    )


def _init_with_cache_retry(cls, model: str, device: str, compute_type: str):
    """Construct the model; on a non-permanent init failure (interrupted
    download, corrupt cache) purge that model's HF cache and try exactly
    once more. The last error always propagates — nothing is swallowed."""
    try:
        return cls(model, device=device, compute_type=compute_type)
    except Exception as exc:
        if _is_permanent_model_error(exc):
            raise
        _purge_model_cache(model)
        log.warning("model init failed (%s); purged model cache, retrying once", exc)
        return cls(model, device=device, compute_type=compute_type)


def _init_whisper_model(model: str, device: str, compute_type: str, auto_device: bool):
    """Construct the faster-whisper model with graceful degradation.

    - CUDA auto-picked but unusable -> retry once on CPU ("auto" means
      best available; an explicit ``device="cuda"`` still fails loudly).
    - Interrupted download / corrupt model cache -> purge + one retry.
    """
    cls = _whisper_model_cls()
    try:
        return cls(model, device=device, compute_type=compute_type)
    except Exception as exc:
        if _looks_like_cuda_failure(exc) and device == "cuda":
            # CUDA is unusable on this box — a re-download can never fix
            # that, so never purge here. With "auto" the faithful move is
            # CPU; with an explicit device="cuda" this is a config error.
            if auto_device:
                log.warning("CUDA init failed (%s); falling back to CPU", exc)
                return _init_with_cache_retry(cls, model, "cpu", "int8")
            raise
        if _is_permanent_model_error(exc):
            raise
        _purge_model_cache(model)
        log.warning("model init failed (%s); purged model cache, retrying once", exc)
        return cls(model, device=device, compute_type=compute_type)


def transcribe_audio(
    video_url: str,
    model: str = "small",
    device: str = "auto",
    language: str = "en",
    cache_dir: Optional[str] = None,
    vad_filter: bool = True,
) -> list[dict]:
    """Transcribe via local faster-whisper; return word-level timestamps.

    ``model`` is a faster-whisper model name (default ``"small"``) and is a
    plain parameter — pick per machine (tiny/base/small/medium/large-v3).
    Returns ``[{"start", "end", "text"}]`` per word, times in seconds.
    """
    audio_path = download_audio(video_url, cache_dir=cache_dir)
    auto_device = device == "auto"
    dev = _pick_device(device)
    compute_type = "float16" if dev == "cuda" else "int8"
    log.info("transcribing %s with faster-whisper %s (%s)", audio_path, model, dev)

    wm = _init_whisper_model(model, dev, compute_type, auto_device)
    tx_kwargs = dict(language=language, word_timestamps=True, vad_filter=vad_filter)
    try:
        segments, _info = wm.transcribe(audio_path, **tx_kwargs)
    except Exception as exc:
        # The Silero VAD onnx asset can be missing from a packaged build
        # (PyInstaller doesn't bundle faster_whisper's data files unless told
        # to). VAD only refines timestamps — retry without it instead of
        # failing the whole job.
        if vad_filter and _looks_like_vad_failure(exc):
            log.warning(
                "VAD model unavailable (%s); retrying transcription without VAD",
                exc,
            )
            segments, _info = wm.transcribe(
                audio_path, language=language, word_timestamps=True, vad_filter=False
            )
        else:
            raise
    words: list[dict] = []
    for seg in segments or []:
        for w in seg.words or []:
            text = (w.word or "").strip()
            if text and w.end > w.start:
                words.append(
                    {
                        "start": round(float(w.start), 3),
                        "end": round(float(w.end), 3),
                        "text": text,
                    }
                )
    words.sort(key=lambda w: w["start"])
    log.info("transcribed %d words from %s", len(words), audio_path)
    return words
