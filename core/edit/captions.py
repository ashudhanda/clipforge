"""Caption burn-in for ClipForge: words -> ASS -> ffmpeg/libass.

Pipeline (adapted from the auto-caption audit):
word-level timestamps -> grouped caption events -> ASS file ->
ffmpeg ``ass`` filter burn-in.

Nine style presets are implemented (karaoke, pop, minimal + 6 new:
hormozi, beast, neon, wordbox, stroke, typewriter).
Ashu's locked user-facing set is karaoke + pop (picked 2026-09-25);
he picks which of the new ones become user-facing after seeing previews
(his taste call — NOT changed here). minimal stays implemented but hidden.
"""

from __future__ import annotations

import os
import random

# Play-resolution for all Shorts output.
PLAY_RES_X, PLAY_RES_Y = 1080, 1920

CAPTION_STYLES = (
    "karaoke", "pop", "minimal",
    "hormozi", "beast", "neon", "wordbox", "stroke", "typewriter",
)

# Ashu-locked user-facing styles (2026-09-25): all 9 — the setup wizard and
# dashboard offer these; "random" picks one per clip.
AVAILABLE_STYLES = CAPTION_STYLES


def resolve_style(style: str) -> str:
    """Resolve a user-chosen style name to a concrete style.

    Accepts any name in CAPTION_STYLES plus the special value "random",
    which picks uniformly from AVAILABLE_STYLES.
    """
    if style == "random":
        return random.choice(AVAILABLE_STYLES)
    if style not in CAPTION_STYLES:
        raise ValueError(f"unknown caption style {style!r}; pick from {CAPTION_STYLES} or 'random'")
    return style

# Preferred font chain — libass resolves via fontconfig; DejaVu/Noto are
# present on this VM. First available wins at render time (fontconfig),
# so we just name the family.
_FONT = "Noto Sans"


def _ass_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .strip()
    )


def _ass_time(s: float) -> str:
    s = max(0.0, s)
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s % 60
    return f"{h}:{m:02d}:{sec:05.2f}"


def group_words(
    words: list[dict],
    max_words: int = 4,
    max_span_s: float = 1.8,
) -> list[list[dict]]:
    """Greedily group words into caption events.

    Breaks early on sentence-ending punctuation so a caption never
    straddles two sentences awkwardly.
    """
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if cur and (
            len(cur) >= max_words
            or w["end"] - cur[0]["start"] > max_span_s
            or cur[-1]["text"].rstrip().endswith((".", "!", "?"))
        ):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


# NOTE (libass quirk, verified 2026-09-25): BorderStyle=3 (OpaqueBox) only
# renders when Outline >= 1 — with Outline=0 the box silently disappears.
# Keep Outline=2 on wordbox_a/b and typewriter. BackColour alpha is also
# ignored by this libass build (box renders fully opaque) — by design here.
_STYLES_ASS = """[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: karaoke,{font},110,&H00FFFFFF,&H0000FFFF,&H99000000,&H00000000,-1,0,0,0,100,100,0,0,1,5,2,5,60,60,80,1
Style: pop,{font},132,&H0000FFFF,&H0000FFFF,&H99000000,&H00000000,-1,0,0,0,100,100,0,0,1,6,3,5,60,60,80,1
Style: minimal,{font},62,&H00FFFFFF,&H00FFFFFF,&H99000000,&H64000000,0,0,0,0,100,100,0,0,1,2,1,2,60,60,240,1
Style: hormozi,{font},118,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,7,3,5,60,60,80,1
Style: beast,{font},142,&H0000FFFF,&H0000FFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,8,4,5,60,60,80,1
Style: neon,{font},112,&H00FFFF00,&H00FFFF00,&H00FF0000,&H00000000,-1,0,0,0,100,100,0,0,1,4,4,5,60,60,80,1
Style: wordbox_a,{font},104,&H00FFFFFF,&H00FFFFFF,&H00000000,&HCC000000,-1,0,0,0,100,100,0,0,3,2,0,5,60,60,80,1
Style: wordbox_b,{font},104,&H00FFFFFF,&H00FFFFFF,&H00000000,&HCC660000,-1,0,0,0,100,100,0,0,3,2,0,5,60,60,80,1
Style: stroke,{font},148,&HFFFFFFFF,&HFFFFFFFF,&H00FFFFFF,&H00000000,-1,0,0,0,100,100,0,0,1,6,0,5,60,60,80,1
Style: typewriter,DejaVu Sans Mono,58,&H00FFFFFF,&H00FFFFFF,&H00000000,&HAA000000,0,0,0,0,100,100,0,0,3,2,0,1,70,70,220,1
""".format(font=_FONT)


# Styles rendered as one event per word (snappy single-word pops).
_PER_WORD_STYLES = ("pop", "beast", "neon", "wordbox")


def _per_word_events(norm: list[dict], style: str) -> list[str]:
    events: list[str] = []
    box_toggle = 0
    for w in norm:
        ev_style = style
        if style == "pop":
            txt = "{\\fad(60,60)}" + _ass_escape(w["text"])
            end = w["end"] + 0.08
        elif style == "beast":
            # MrBeast-style: huge yellow caps, one word at a time.
            txt = "{\\fad(60,60)}" + _ass_escape(w["text"].upper())
            end = w["end"] + 0.08
        elif style == "neon":
            # Cyan glow: \blur softens edges into the blue outline/shadow.
            txt = "{\\blur2\\fad(60,60)}" + _ass_escape(w["text"])
            end = w["end"] + 0.08
        elif style == "wordbox":
            # TikTok-style: single word on a solid box, alternating colours.
            ev_style = "wordbox_a" if box_toggle % 2 == 0 else "wordbox_b"
            box_toggle += 1
            txt = _ass_escape(w["text"])
            end = w["end"] + 0.05
        else:  # pragma: no cover - guarded by _PER_WORD_STYLES
            raise ValueError(style)
        events.append(
            f"Dialogue: 0,{_ass_time(w['start'])},{_ass_time(end)},"
            f"{ev_style},,0,0,0,,{txt}"
        )
    return events


def _karaoke_text(group: list[dict]) -> str:
    """One dialogue line with per-word {\\k} sweep timings (centiseconds)."""
    parts: list[str] = []
    for w in group:
        dur_cs = max(5, int(round((w["end"] - w["start"]) * 100)))
        parts.append(f"{{\\k{dur_cs}}}" + _ass_escape(w["text"]))
    return " ".join(parts)


def _grouped_events(norm: list[dict], style: str) -> list[str]:
    if style == "hormozi":
        groups = group_words(norm, max_words=2, max_span_s=1.2)
    elif style == "stroke":
        groups = group_words(norm, max_words=3, max_span_s=1.5)
    else:
        groups = group_words(norm)
    events: list[str] = []
    for group in groups:
        s = max(0.0, group[0]["start"] - 0.05)
        e = group[-1]["end"] + 0.10
        if style == "karaoke":
            txt = _karaoke_text(group)
        elif style == "hormozi":
            # Hormozi-style: bold white ALL-CAPS, 2 words, quick fade.
            txt = "{\\fad(50,50)}" + " ".join(
                _ass_escape(w["text"].upper()) for w in group
            )
        elif style == "stroke":
            # Outline-only caps: transparent fill, thick white stroke.
            txt = " ".join(_ass_escape(w["text"].upper()) for w in group)
        elif style == "typewriter":
            txt = " ".join(_ass_escape(w["text"]) for w in group) + "▌"
        else:  # minimal — clean full-phrase line
            txt = " ".join(_ass_escape(w["text"]) for w in group)
        events.append(
            f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},"
            f"{style},,0,0,0,,{txt}"
        )
    return events


def build_ass(words: list[dict], style: str = "karaoke") -> str:
    """Build a complete ASS subtitle document for *words* (post-cut timeline).

    Every word is guaranteed >= 0.05 s of display (audit lesson from
    cutawan's normalizeWordTimings). Accepts "random" (resolved via
    AVAILABLE_STYLES).
    """
    style = resolve_style(style)

    # Clamp tiny durations so no word flashes by invisibly.
    norm: list[dict] = []
    for w in words:
        dur = w["end"] - w["start"]
        if dur < 0.05:
            w = {**w, "end": w["start"] + 0.05}
        norm.append(w)

    if style in _PER_WORD_STYLES:
        events = _per_word_events(norm, style)
    else:
        events = _grouped_events(norm, style)

    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {PLAY_RES_X}\n"
        f"PlayResY: {PLAY_RES_Y}\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n"
        "\n" + _STYLES_ASS + "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        + "\n".join(events)
        + "\n"
    )


def write_ass(words: list[dict], style: str, out_path: str) -> str:
    """Write the ASS file; returns *out_path*."""
    doc = build_ass(words, style)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    return out_path


def ass_filter(ass_path: str) -> str:
    """Render the libass burn-in as an ffmpeg video-filter string."""
    # Escape for ffmpeg filter parsing (: ' [ ] need backslash-escaping).
    esc = (
        ass_path.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )
    return f"ass='{esc}'"
