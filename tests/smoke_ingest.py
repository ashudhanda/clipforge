"""Real-network smoke test for the Phase 1 ingest layer.

NOT part of the pytest suite (needs network + the faster-whisper model).
Run:  .venv/bin/python tests/smoke_ingest.py

Video: "Me at the zoo" (jNQXAC9IVRw) — 19 s, public, has auto captions.
"""

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ingest import download_ranges, fetch_captions, ingest, transcribe_audio

VIDEO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
# Local faster-whisper model (path or HF name). The sandbox reuses the
# ZeroPing-cached base model; on a fresh machine use e.g. "small".
WHISPER_MODEL = os.environ.get(
    "CF2_WHISPER_MODEL", "/home/hatch/workspace/zeroping/whisper_model"
)


def probe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def main():
    cache = tempfile.mkdtemp(prefix="cf2smoke_")
    print("cache:", cache)
    try:
        _run(cache)
    except Exception as e:  # noqa: BLE001 - smoke script reports, never tracebacks
        print("SMOKE TEST BLOCKED:", str(e).splitlines()[0])
        sys.exit(2)


def _run(cache):

    print("\n[1] captions fetch (no video download)...")
    cues = fetch_captions(VIDEO)
    assert cues, "expected captions for Me at the zoo"
    print(f"    OK: {len(cues)} cues, first: {cues[0]}")

    print("\n[2] whisper fallback (audio-only + local transcription)...")
    words = transcribe_audio(VIDEO, model=WHISPER_MODEL, cache_dir=cache)
    assert words, "expected words from whisper"
    print(f"    OK: {len(words)} words, first 5: {[w['text'] for w in words[:5]]}")

    print("\n[3] range-only download [(2,8),(10,16)]...")
    paths = download_ranges(VIDEO, [(2, 8), (10, 16)], cache_dir=cache)
    assert len(paths) == 2, paths
    for p in paths:
        assert os.path.getsize(p) > 0
        print(f"    OK: {os.path.basename(p)} duration={probe_duration(p):.1f}s")

    print("\n[4] full ingest() end-to-end...")
    out = ingest(VIDEO, segments=[(2, 8), (10, 16)], cache_dir=cache)
    assert out["source"] == "captions", out["source"]
    assert len(out["transcript"]) > 0
    assert len(out["media_paths"]) == 2
    print("    OK:", json.dumps({k: (len(v) if isinstance(v, list) else v)
                                 for k, v in out.items() if k != "transcript"}))

    print("\nSMOKE TEST PASSED")


if __name__ == "__main__":
    main()
