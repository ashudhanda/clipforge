"""Speaker-aware 9:16 full-bleed cover crop for ClipForge.

HARD RULE (from a past project that lost views over this): the crop is
ALWAYS full-bleed — the 9:16 window is cut out of the source frame and
fills the whole 1080x1920 output. There is no letterbox / blurred-fill
``fit_layout`` path anywhere in this module.

Face detection steers the crop window horizontally so a speaker/facecam
stays in frame. The window position is a single FIXED x per clip (median
of smoothed detections) — deliberately, because per-frame tracking
introduces the jittery crops we want to avoid.
"""

from __future__ import annotations

import statistics
import subprocess
import tempfile
import os

from core.paths import ffmpeg_path as _ffmpeg_path
from core.paths import ffprobe_path as _ffprobe_path

import numpy as np

TARGET_W, TARGET_H = 1080, 1920
CROP_RATIO = 9 / 16


def _ffmpeg() -> str:
    path = _ffmpeg_path()
    if not path:
        raise RuntimeError("ffmpeg not found — install it (see README) or set CLIPFORGE_FFMPEG")
    return path


def displayed_dims(video_path: str) -> tuple[int, int]:
    """Return the ACTUAL displayed (w, h) of the video.

    Lesson from a past bug: ffprobe's stream width/height are the CODED
    dimensions — phone videos often carry rotation metadata. Extracting
    one real frame (ffmpeg auto-applies rotation on decode) and probing
    the PNG gives the true displayed size.
    """
    with tempfile.TemporaryDirectory() as tmp:
        png = os.path.join(tmp, "frame.png")
        cmd = [
            _ffmpeg(), "-hide_banner", "-loglevel", "error",
            "-i", video_path,
            "-vframes", "1",
            "-y", png,
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0 or not os.path.exists(png):
            raise RuntimeError(
                f"frame extraction failed: {proc.stderr.decode()[:200]}"
            )
        ffprobe = _ffprobe_path()
        if not ffprobe:
            raise RuntimeError(
                "ffprobe not found — cannot measure the video's displayed size"
            )
        probe = [
            ffprobe, "-hide_banner", "-loglevel", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0", png,
        ]
        out = subprocess.run(probe, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if out.returncode != 0:
            raise RuntimeError(
                f"ffprobe failed on extracted frame: {out.stderr.decode()[:200]}"
            )
        parts = out.stdout.decode().strip().split(",")
        if len(parts) != 2:
            raise RuntimeError(
                f"ffprobe returned unexpected dimensions: {out.stdout.decode()[:80]!r}"
            )
        try:
            w, h = int(parts[0]), int(parts[1])
        except ValueError:
            raise RuntimeError(
                f"ffprobe returned non-numeric dimensions: {out.stdout.decode()[:80]!r}"
            )
        if w <= 0 or h <= 0:
            raise RuntimeError(
                f"ffprobe returned invalid dimensions: {w}x{h}"
            )
        return w, h


def _sample_frame_png(video_path: str, t: float, width: int = 320) -> "np.ndarray | None":
    import cv2

    cmd = [
        _ffmpeg(), "-hide_banner", "-loglevel", "error",
        "-i", video_path,
        "-ss", f"{t:.3f}",
        "-vframes", "1",
        "-vf", f"scale={width}:-2",
        "-f", "image2pipe", "-vcodec", "png", "-",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0 or not proc.stdout:
        return None
    buf = np.frombuffer(proc.stdout, dtype=np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def detect_face_centers(
    video_path: str,
    start: float,
    end: float,
    max_samples: int = 12,
) -> list[tuple[float, float]]:
    """Sample frames across [start, end); return [(t, center_x_frac), ...].

    Returns an empty list when OpenCV is unavailable or no faces found —
    callers must fall back to center crop (never crash).
    """
    try:
        import cv2
    except ImportError:
        return []
    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    detector = cv2.CascadeClassifier(cascade_path)
    if detector.empty():
        return []

    duration = max(0.0, end - start)
    if duration <= 0:
        return []
    n = max(2, min(max_samples, int(duration / 0.5)))
    hits: list[tuple[float, float]] = []
    for i in range(n):
        t = start + duration * (i + 0.5) / n
        frame = _sample_frame_png(video_path, t)
        if frame is None:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = detector.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30)
        )
        if len(faces) == 0:
            continue
        # Largest face wins (closest speaker / main facecam).
        x, y, w, h = max(faces, key=lambda r: r[2] * r[3])
        hits.append((t, (x + w / 2) / frame.shape[1]))
    return hits


def compute_crop(
    video_path: str,
    start: float = 0.0,
    end: float | None = None,
    target: tuple[int, int] = (TARGET_W, TARGET_H),
) -> dict:
    """Compute a full-bleed 9:16 crop window: {x, y, w, h} in source pixels.

    Face-aware: the window is steered so the median detected face center
    stays in frame; otherwise centered. Always fills the whole output
    frame after scaling — never letterboxed.
    """
    dw, dh = displayed_dims(video_path)
    tw, th = target
    want = tw / th  # 9/16

    # Exact-ratio crop: width must be a multiple of 9 (and even), height a
    # multiple of 16 (and even) for 9:16. Rounding to "even" naively breaks
    # the ratio (e.g. 404x718 != 9:16), so step in whole ratio units.
    from math import gcd
    _g = gcd(tw, th)
    uw, uh = tw // _g, th // _g  # 9, 16
    step_w = uw * (2 if uw % 2 else 1)  # 18
    step_h = uh * (2 if uh % 2 else 1)  # 32 (kept for clarity)

    if dw / dh >= want:
        # Landscape or square: full height, cut sides.
        cw = (int(dh * want) // step_w) * step_w
        ch = cw * uh // uw
    else:
        # Narrow portrait: full width, cut top/bottom.
        cw = (dw // step_w) * step_w
        ch = cw * uh // uw
    cw = max(step_w, min(cw, dw - dw % 2))
    ch = max(step_h, min(ch, dh - dh % 2))
    # Re-derive to guarantee exactness after clamping.
    cw = (cw // step_w) * step_w
    ch = cw * uh // uw

    # On tiny sources (smaller than one 18x32 ratio step) the clamped
    # window can exceed the frame — e.g. 18x32 on a 20x20 video — which
    # ffmpeg then rejects with a cryptic error. Fail here, clearly.
    if cw > dw or ch > dh:
        raise RuntimeError(
            f"source video too small for a 9:16 crop: {dw}x{dh} "
            f"(smallest exact-ratio window is {cw}x{ch})"
        )

    if end is None:
        end = start + 30.0  # sampling window only; crop is geometric
    faces = detect_face_centers(video_path, start, end)
    if len(faces) >= 3:
        # Median of detections = robust to flicker; single fixed x = no jitter.
        cx_frac = statistics.median(c for _, c in faces)
        x = int(cx_frac * dw - cw / 2)
    else:
        x = (dw - cw) // 2
    x = max(0, min(dw - cw, x))
    x -= x % 2
    y = max(0, (dh - ch) // 2)
    y -= y % 2

    return {"x": x, "y": y, "w": cw, "h": ch,
            "src_w": dw, "src_h": dh,
            "face_guided": len(faces) >= 3,
            "target": [tw, th]}


def crop_filter(params: dict) -> str:
    """Render the crop as an ffmpeg filter string."""
    return f"crop={params['w']}:{params['h']}:{params['x']}:{params['y']}"


def is_full_bleed(params: dict) -> bool:
    """Sanity check: the crop window is exactly the target aspect ratio."""
    tw, th = params["target"]
    return params["w"] * th == params["h"] * tw
