"""Loudness normalization for ClipForge.

Single-pass EBU R128-style normalization via ffmpeg's ``loudnorm``
(YouTube-Shorts-friendly -14 LUFS target) plus a true-peak safety
limiter. One pass keeps the pipeline fast; the slight accuracy trade-off
vs dual-pass loudnorm is acceptable for Shorts.
"""

from __future__ import annotations

TARGET_LUFS = -14.0
TRUE_PEAK_DBTP = -1.0
LRA = 11.0


def loudnorm_filter(
    target_lufs: float = TARGET_LUFS,
    true_peak_dbtp: float = TRUE_PEAK_DBTP,
    lra: float = LRA,
) -> str:
    """Return the ffmpeg audio-filter chain for normalization + limiting."""
    # alimiter limit is linear amplitude: -1 dBTP ~= 0.891.
    limiter = 10 ** (true_peak_dbtp / 20.0)
    return (
        f"loudnorm=I={target_lufs}:TP={true_peak_dbtp}:LRA={lra},"
        f"alimiter=limit={limiter:.3f}:attack=5:release=50:level_in=1:level_out=1"
    )
