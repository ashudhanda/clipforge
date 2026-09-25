"""Smoke test for Phase 2 moment detection (mock LLM — no real API calls).

Exercises the full chain on a realistic ~10-min synthetic podcast fixture:
  llm scoring -> sentence snapping -> duration validation -> dedupe,
plus the offline TextTiling path (boundary detection + determinism).

Run:  cd ~/workspace/clipforge2 && .venv/bin/python tests/smoke_moments.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.moments import detect_moments
from core.moments.llm import LLMProvider
from core.moments.scorer import group_sentences
from core.moments.segmenter import segment_offline

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(f"{name}: {detail}")


class FakeProvider(LLMProvider):
    name = "fake"

    def __init__(self, payload):
        self.payload = payload

    def generate_json(self, system, user):
        return self.payload, {"provider": "fake", "model": "fake",
                              "est_prompt_tokens": 1}


def main() -> int:
    fx_path = os.path.join(os.path.dirname(__file__), "fixtures", "podcast_10min.json")
    with open(fx_path) as f:
        fx = json.load(f)
    transcript = fx["transcript"]
    sentences = group_sentences(transcript)
    starts = {round(s.start, 2) for s in sentences}
    ends = {round(s.end, 2) for s in sentences}
    print(f"fixture: {len(sentences)} sentences, {sentences[-1].end:.1f}s")

    # ---- canned LLM response: exact, overlapping, mid-sentence, bad rows ---
    def cand(a, b, score, title, **kw):
        d = {"start": a, "end": b, "title": title, "hook_line": "hook",
             "reason": "reason", "score": score}
        d.update(kw)
        return d

    s = sentences
    canned = {"clips": [
        cand(s[5].start, s[14].end, 88, "Exact boundaries"),
        cand(s[10].start, s[20].end, 72, "Heavy overlap"),   # >40% ov w/ above -> dropped
        cand(s[30].start + 1.7, s[40].end - 0.9, 81, "Mid-sentence"),  # snapped
        cand(s[50].start, s[90].end, 64, "Too long"),         # >100s -> dropped
        {"start": s[60].start, "title": "Broken"},            # missing fields -> dropped
        cand(s[100].start, s[110].end, 150, "Clamped"),       # score -> 100
        cand(s[70].start, s[78].end, 55, "Late one"),
        cand(s[80].start, s[95].end, 77, "Later one"),
    ]}

    print("\n[1] llm mode: scoring -> snap -> validate -> dedupe")
    clips = detect_moments(transcript, mode="llm",
                           provider=FakeProvider(canned), min_clip_sec=8.0)
    titles = [c.title for c in clips]
    print("  kept:", [(c.title, c.score, f"{c.start:.1f}-{c.end:.1f}") for c in clips])
    check("expected 5 clips kept", len(clips) == 5, f"got {len(clips)}: {titles}")
    check("sorted by score desc", [c.score for c in clips] == sorted([c.score for c in clips], reverse=True))
    check("score clamped to 100", clips[0].score == 100 and clips[0].title == "Clamped")
    check("heavy-overlap clip deduped", "Heavy overlap" not in titles)
    check("too-long clip dropped", "Too long" not in titles)
    check("broken row dropped", "Broken" not in titles)
    check("no mid-sentence starts", all(c.start in starts for c in clips),
          str([c.start for c in clips if c.start not in starts]))
    check("no mid-sentence ends", all(c.end in ends for c in clips),
          str([c.end for c in clips if c.end not in ends]))
    mid = next(c for c in clips if c.title == "Mid-sentence")
    check("mid-sentence snapped to sentence edges",
          mid.start == round(s[30].start, 2) and mid.end == round(s[40].end, 2))

    # ---- offline mode ------------------------------------------------------
    print("\n[2] offline mode: TextTiling-lite topic segmentation")
    off = detect_moments(transcript, mode="offline", min_clip_sec=8.0)
    print("  kept:", [(f"{c.start:.0f}-{c.end:.0f}", c.score) for c in off])
    check("at least 2 topic clips", len(off) >= 2, f"got {len(off)}")
    check("scores in 0-100", all(0 <= c.score <= 100 for c in off))
    check("source marked offline", all(c.source == "offline" for c in off))
    bounds = sorted({c.start for c in off[1:]} | {c.end for c in off[:-1]})
    for expect in (150, 300, 450):
        check(f"boundary near {expect}s topic switch",
              any(abs(b - expect) < 25 for b in bounds), f"bounds={bounds}")

    print("\n[3] offline determinism")
    again = [ (c.start, c.end, c.score) for c in segment_offline(transcript) ]
    first = [ (c.start, c.end, c.score) for c in segment_offline(transcript) ]
    check("identical reruns", again == first)

    print()
    if FAILURES:
        print(f"SMOKE FAILED ({len(FAILURES)}):")
        for f_ in FAILURES:
            print("  -", f_)
        return 1
    print("SMOKE PASSED: llm + dedupe + snap + offline all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
