"""Tests for the 6 new caption styles (hormozi, beast, neon, wordbox,
stroke, typewriter) — pure ASS building, zero network."""

import pytest

from core.edit.captions import (
    AVAILABLE_STYLES,
    CAPTION_STYLES,
    build_ass,
    resolve_style,
)

NEW_STYLES = ("hormozi", "beast", "neon", "wordbox", "stroke", "typewriter")

WORDS = [
    {"start": 0.20 + i * 0.40, "end": 0.50 + i * 0.40, "text": t}
    for i, t in enumerate("this is a test clip ok".split())
]


def _style_defs(doc: str) -> str:
    """The [V4+ Styles] section of an ASS doc."""
    return doc.split("[Events]")[0]


@pytest.mark.parametrize("style", NEW_STYLES)
def test_new_style_has_style_definition(style):
    doc = build_ass(WORDS, style)
    defs = _style_defs(doc)
    if style == "wordbox":
        assert "Style: wordbox_a," in defs
        assert "Style: wordbox_b," in defs
    else:
        assert f"Style: {style}," in defs


@pytest.mark.parametrize("style", NEW_STYLES)
def test_new_style_emits_dialogue_events(style):
    doc = build_ass(WORDS, style)
    assert "Dialogue:" in doc


def test_unknown_style_still_raises():
    with pytest.raises(ValueError):
        build_ass(WORDS, "comic-sans-extreme")


def test_random_resolves_only_from_available_styles():
    seen = {resolve_style("random") for _ in range(200)}
    assert seen <= set(AVAILABLE_STYLES)
    assert seen == set(AVAILABLE_STYLES)  # all reachable (Ashu: all 9 user-facing)


def test_resolve_style_accepts_new_styles_directly():
    for s in NEW_STYLES:
        assert resolve_style(s) == s


@pytest.mark.parametrize("style", NEW_STYLES)
def test_word_display_floor_applies(style):
    tiny = [{"start": 1.0, "end": 1.01, "text": "blink"}]
    doc = build_ass(tiny, style)
    assert "Dialogue:" in doc  # didn't crash; clamp handled it


@pytest.mark.parametrize("style", NEW_STYLES)
def test_special_chars_escaped(style):
    tricky = [{"start": 0.5, "end": 1.0, "text": "wow{cool}\\nice"}]
    doc = build_ass(tricky, style)
    body = doc.split("[Events]")[1]
    # hormozi/beast/stroke uppercase their text — expect the uppercased form.
    expected = "WOW\\{COOL\\}\\\\NICE" if style in ("hormozi", "beast", "stroke") \
        else "wow\\{cool\\}\\\\nice"
    assert expected in body


def test_wordbox_alternates_box_styles():
    doc = build_ass(WORDS, "wordbox")
    body = doc.split("[Events]")[1]
    lines = [ln for ln in body.splitlines() if ln.startswith("Dialogue:")]
    styles_used = [ln.split(",")[3] for ln in lines]
    assert styles_used == ["wordbox_a", "wordbox_b"] * 3  # 6 words


def test_hormozi_is_uppercase_with_fade():
    doc = build_ass(WORDS, "hormozi")
    body = doc.split("[Events]")[1]
    assert "THIS IS" in body
    assert "\\fad(50,50)" in body


def test_beast_is_uppercase_yellow_pop():
    doc = build_ass(WORDS, "beast")
    defs = _style_defs(doc)
    assert "Style: beast," in defs and "&H0000FFFF" in defs.split("Style: neon")[0]
    assert "TEST" in doc.split("[Events]")[1]


def test_neon_has_blur_override():
    doc = build_ass(WORDS, "neon")
    assert "\\blur2" in doc.split("[Events]")[1]


def test_stroke_is_uppercase_outline_only():
    doc = build_ass(WORDS, "stroke")
    defs = _style_defs(doc)
    stroke_line = [ln for ln in defs.splitlines() if ln.startswith("Style: stroke,")][0]
    assert "&HFFFFFFFF" in stroke_line  # transparent fill
    assert "CLIP" in doc.split("[Events]")[1]


def test_typewriter_has_cursor_and_mono_box():
    doc = build_ass(WORDS, "typewriter")
    defs = _style_defs(doc)
    tw_line = [ln for ln in defs.splitlines() if ln.startswith("Style: typewriter,")][0]
    assert "DejaVu Sans Mono" in tw_line
    assert ",3," in tw_line  # OpaqueBox border style
    assert "▌" in doc.split("[Events]")[1]
