# ClipForge2 — Phase 0 Deep Code Audit

**Date:** 2026-09-25 · **Phase:** 0 (study only — no implementation code written, nothing copied into ClipForge2)
**Auditor:** Brio · **Status:** 5 repos audited at source level

This report records what was learned from reading the source of five existing
GitHub implementations, to inform ClipForge2's architecture (Python-based
Shorts pipeline: captions-first ingest, LLM moment selection, silence/filler
removal, full-bleed 9:16 smart crop, animated captions, loudness normalization,
metadata generation, upload/scheduling with YouTube Data API OAuth, analytics
feedback). All facts below come from the cloned source read on 2026-09-25;
star/issue counts are live GitHub API data as of 2026-09-25.

**Repos audited:**

| # | Repo | License | Stars | Open issues | Branch | Last commit |
|---|------|---------|-------|-------------|--------|-------------|
| 1 | `JeremySNR/cutawan` | MIT | 24 | 4 | `main` | 2026-09-23 (v0.12.1) |
| 2 | `WyattBlue/auto-editor` | Unlicense | 5,354 | 0 | `master` | 2026-09-19 (ffmpeg 9.0.2) |
| 3 | `ClipsAI/clipsai` | MIT | 543 | 15 | `main` | 2024-01-17 (stale ~2.7 yrs) |
| 4 | `sebetancurch/auto-caption` | MIT | 7 | 1 | `main` | 2026-07-17 |
| 5 | `fyrek1d/yt-automation` | MIT | 1 | 0 | `main` | 2026-08-18 |

---

## Repo 1 — JeremySNR/cutawan (MIT)

Desktop Electron app (TypeScript) that turns podcasts/long videos into captioned
9:16 Shorts with AI clip finding and speaker-aware reframing. The closest
single-repo analog to ClipForge2's goals. HEAD `15c0694` (2026-09-23).

### Module / file map

- `src/main/pipeline/` — the processing pipeline (all Electron main process):
  - `transcribe.ts` — chunked Whisper transcription, seam stitching, hallucination filtering
  - `highlights.ts` — LLM viral-moment detection + scoring + dedupe
  - `asd.ts` — LR-ASD audio-visual active-speaker detection (ONNX)
  - `facetracks.ts` — per-person face tracking (IOU + interpolation)
  - `faces.ts` — auto-reframe orchestration + focus-track building
  - `detect.ts` — UltraFace face detection + scene-cut detection
  - `mfcc.ts` — MFCC audio features for the ASD model
  - `energy.ts` — per-segment vocal energy (arousal signal for virality)
  - `vad.ts` — Silero VAD v5 (ONNX, 16 kHz)
  - `reframe.ts` — per-clip framing analysis orchestration/persistence
  - `captions.ts` — ASS karaoke subtitle generation (`buildAss()`)
  - `zoom.ts` (in `src/shared/`) — deterministic zoom-event planner
  - `tighten.ts` (in `src/shared/`) — silence/filler removal planner
  - `render.ts` — FFmpeg render: crop, sendcmd focus, burn-in captions
  - `ytdlp.ts` — yt-dlp binary management + URL downloads
  - `broll.ts`, `imagesearch.ts`, `socialCaption.ts` — LLM B-roll tagging, image search, post captions
  - `encoders.ts` — NVENC detection, GPU ffmpeg download
- `src/main/inference/` — ONNX inference worker + protocol
- `src/shared/` — shared types, `captionStyles.ts` (12 presets), `tighten.ts`, `zoom.ts`, reframing helpers
- `src/renderer/` — React 19 editor/dashboard (Zustand, Tailwind)
- `resources/models/` — bundled ONNX models (`lr-asd-*.onnx`, exported from MIT-licensed LR-ASD weights via `scripts/export-asd-onnx.py`)
- `scripts/` — `eval-clips.ts` (clip-boundary quality measurement), integration test scripts (`test-pipeline`, `test-e2e`, `test-asd`, …)
- `tests/` — 85 `*.test.ts` Vitest files

Dependencies: Electron, React 19, TypeScript, `ffmpeg-static`, `@ffmpeg-installer/ffprobe`,
`onnxruntime-node`, Zustand, Vitest. ONNX models run locally; LLM/vision passes go
through an API connection (ChatGPT/Codex sign-in) — transcript + sampled frames only,
full video never uploaded.

### Data flow

1. Import (local file or URL via yt-dlp → **full-video download**) or "caption whole video" mode.
2. `transcribeChunks()` — overlapping Whisper chunks decoded sequentially, next chunk
   primed with trusted previous text; `stitchChunkResults()` assigns disjoint
   responsibility windows; `repairTranscriptSeam()` re-decodes suspicious boundaries;
   `isHallucinatedSegment()` drops chunks with compression ratio > 2.4 or
   no-speech-prob > 0.6 + avg-logprob < −1.0; `normalizeWordTimings()` enforces
   sorted, non-overlapping words with ≥ 0.05 s display.
3. `detectHighlights()` → `requestHighlights()` — sentence-level timestamp lines
   (`formatTranscriptForModel()`) → LLM JSON schema (timestamps, title, hook,
   summary, payoff, virality score+reason, hashtags).
4. `refineClipEndings()` (second LLM pass for payoff/complete endings) →
   `refineClipStarts()` (trim throat-clearing) → `dedupeClips()`.
5. `analyzeClipASD()` → `analyzeClipLayout()` (persisted per clip, deduped via
   `ensureClipReframe()`) → `computeZoomEvents()` → `clipKeptSegments()` (tighten) →
   `buildAss()` → `render.ts` FFmpeg render with crop + sendcmd + libass burn-in.

### Algorithms (with numbers)

- **Virality scoring rubric (0–99 total):** Hook 0–30, High-arousal emotion 0–25,
  Value 0–20, Structure 0–15, Shareability 0–9 — text rubric inspired by
  Berger & Milkman (JMR 2012), plus measured vocal energy + an optional vision
  pass scoring sampled frames for scroll-stopping potential.
- **Clip count:** `targetClipCount()` ≈ 1 clip per source minute, bounded 4–40.
- **Clip duration:** minimum candidate 8 s; caps 45 s (short) / 75 s (medium) / 100 s (long/auto).
- **Boundary snapping:** `snapClipStart()`/`snapClipEnd()` snap to sentence/word boundaries.
- **Dedupe:** `dedupeClips()` keeps higher score; rejects later clip when overlap
  exceeds **40% of the shorter clip**.
- **ASD:** LR-ASD ONNX frontend/backend. YuNet face detection preferred,
  UltraFace fallback. 25 FPS analysis, face detection every 5th frame.
  `buildFaceTracks()` builds per-person tracks; mouth-centred 112×112 grayscale
  crops; 13-dim MFCC, 4 audio-feature frames per video frame; audio-visual
  LR-ASD score > 0 = speaking; backend passes averaged over 1–6 s windows;
  scene-cut detection resets tracks (no tracking across transitions);
  ≥ 25% track coverage required before expensive ASD scoring.
- **VAD/tighten:** Silero VAD v5 ONNX (16 kHz mono PCM, 512-sample chunks):
  positive threshold 0.5, negative 0.35, min speech 0.25 s, min silence 0.1 s,
  speech padding 0.03 s, hysteresis in `speechRegions()`. `computeKeptSegments()`:
  trims pauses > 0.7 s, keeps 0.18 s pre-roll / 0.3 s post-roll, ignores
  removals < 0.35 s, minimum kept-span + gap-bridging to avoid jitter.
  Filler set: um, uh, uhm, umm, erm, er, ah, mmm, hmm, mhm.
- **Captions:** `buildAss()` — one event per spoken-word interval, whole group
  shown, active word emphasized; FFmpeg/libass burn-in; CSS→libass font sizing
  via font metrics; per-event positioning to avoid visual-detail regions;
  bundles OFL-licensed Anton + Poppins; 12 presets (`beast`, `karaoke`, `pill`,
  `minimal`, `hormozi`, `neon`, `ember`, `bubble`, `lemon`, `retro`, `crimson`, `whisper`).
- **Zoom:** `computeZoomEvents()` — deterministic: `cut` (alternate 1×/1.12× at
  tighten-cut joins), `punch` (up to 1.16× on energetic lines ≥ 0.75 energy),
  `creep` (gradual to 1.08× over static stretches > 6 s); max 2 punch-ins/clip,
  ≥ 1.2 s between events; `fitZoomEvents()` caps 64 events;
  `remapZoomEvents()` maps source-time events through tightened output time.
- **Render:** full-bleed crop around horizontal focus point (scaled to output),
  time-varying focus via FFmpeg `sendcmd`; blurred-fit/letterbox modes exist but
  are NOT the default — ClipForge2 must not adopt them (full-bleed only).
- **Ingest:** `downloadUrlVideo()` downloads the **entire source video**.
  ClipForge2's captions-first/range-only ingestion remains a custom differentiator.

### Tests

85 Vitest test files — the heaviest-tested repo in this audit. Integration
scripts in `scripts/` (`test-pipeline`, `test-e2e`, `test-quality`,
`test-wholevideo`, `test-encoders`, `test-resilience`, `test-broll`,
`test-youtube`, `test-asd`, `smoke-test.sh`); some e2e need `OPENAI_API_KEY`.
`scripts/eval-clips.ts` measures clip boundaries (mid-sentence opens/closes,
dead-air tails, length spread) and can `--rerun` detection to compare prompts.

### License / compliance

MIT (JeremySNR/cutawan). Bundled ONNX weights (`resources/models/lr-asd-*.onnx`)
are exported from MIT-licensed LR-ASD weights (Junhua-Liao/LR-ASD) — license
clean. Fonts Anton + Poppins are OFL — fine for redistribution/bundling.
Reuse is legally straightforward; only hard requirement is retaining the MIT
notice.

### Reuse / adapt / rewrite verdict

- **Reuse ideas/algorithms (rewrite in Python):** virality rubric weights,
  dedupe overlap rule (40% of shorter), sentence-boundary snapping,
  VAD thresholds + tighten parameters (0.7 s pause trim, pre/post rolls),
  zoom-event design, ASS-per-word-group caption pattern.
- **Adapt:** the active-speaker approach — but in Python we'd use Silero VAD +
  lighter face detection rather than porting the full Electron/ONNX stack;
  the `sendcmd` time-varying focus render technique is directly adaptable to
  an FFmpeg command builder.
- **Do NOT reuse:** the Electron/React/UI layer, the full-video yt-dlp download
  pattern (ClipForge2 needs range-only), the ChatGPT/Codex auth flow (we'll use
  multi-provider LLM keys).
- **Don't copy:** `broll.ts`/`imagesearch.ts` are out of scope for v1.

---

## Repo 2 — WyattBlue/auto-editor (Unlicense)

5,354 stars, 0 open issues, branch `master`, HEAD `7796222` (2026-09-19,
v31.6.1). "Effort free video editing" — now predominantly **Nim** (not
Python): `requires "nim >= 2.2.2"` + `nimcrypto == 0.7.3` are the only nimble
deps. FFmpeg is **built from source and statically linked** via nimble tasks
— the tool never shells out to an ffmpeg CLI; it calls libav* directly.
Ships a WASM build too.

### Module / file map

- `src/main.nim` — CLI entry. `main()` → hand-rolled arg loop (per-option enum
  `coEdit`, `coMargin`, …; `--edit:N`/`--when:N` labeled forms) → `editMedia(args)`.
- `src/conductor.nim` — pipeline orchestrator; `editMedia()` runs the full flow.
- `src/edit.nim` — `interpretEdit()` evaluates `--edit` into per-frame 0/1
  labels; labels ≥ 2 merged with priority-max; `editEval` s-expr evaluator;
  `orWithThreshold()` threshold→boolean mask.
- `src/editlexer.nim` / `src/editparse.nim` — lexer + recursive-descent parser
  → `Expr` AST; arg binding against `argOrderOf(name)`.
- `src/editmethods.nim` — single source of truth: method/operator definitions.
- `src/analyze/audio.nim` — `audio()` → per-frame loudness `seq[Unorm16]`;
  `loudness()`, `peaks()` iterators.
- `src/analyze/motion.nim` — `motion()`; `motionness(width, blur, rect)`;
  `VideoProcessor` + `videoPipeline(filter)` frame pump via libavfilter.
- `src/analyze/blackdetect.nim`, `src/analyze/subtitle.nim` — black-pixel
  ratio, regex subtitle time-window masking.
- `src/lib/editutil.nim` — `smoothing()` (mincut/minclip), `mutMargin()`
  (boundary dilation); `src/lib/dnorm16.nim` — Unorm16/Snorm16 types.
- `src/timeline.nim` — `v3` timeline model (layers v/a/s, `Clip{src,start,dur,
  offset,stream,effects}`, byte-packed `Actions`); `initLinearTimeline`,
  `applyArgs`, `bakeTransitions`.
- `src/action.nim` — `ActionKind` (`actSpeed`, `actVolume`, `actBlur`, …;
  `actCut` = speed ≥ 99999).
- `src/render/{format,video,audio,smart,partialplan,subtitle}.nim` —
  `makeMedia()` render entry; own software frame compositor (~2k lines);
  `smartRenderPlan()` GOP-aligned copy/encode spans.
- `src/transcribe.nim`, `src/cmds/whisper.nim` — bundled whisper.cpp/Parakeet/
  Apple Speech with energy-gated chunking; `auto-editor whisper <file> <model>`.
- `src/cache.nim` — disk cache `$TMPDIR/ae-31.6.1/`, SHA-1 key over
  (path, mtime, size, timebase, args), raw Unorm16 binary.
- `src/exports/{fcp7,fcp11,otio,shotcut,kdenlive,mlt,json}.nim` — NLE timeline
  exports (Premiere/Resolve/FCP/Shotcut/Kdenlive/OTIO/JSON).
- `src/cmds/levels.nim` — dumps per-frame audio/motion levels (diagnostic).

### Data flow

```
main.nim: main()
 └─ conductor.nim: editMedia(args)
      ├─ edit.nim: interpretEdit()     # --edit s-expr → seq[uint8] per-frame labels
      │    ├─ analyze/audio.nim: audio()   # per-frame peak amplitude, all streams OR'd
      │    ├─ analyze/motion.nim: motion() # per-frame motion ratio
      │    ├─ analyze/blackdetect.nim     # per-frame black-pixel ratio
      │    └─ analyze/subtitle.nim        # regex subtitle windows
      ├─ margin (0.2s) + smooth (0.2s/0.1s) on active mask (lib/editutil.nim)
      ├─ actionMap[label] = whenInactive(aCut)/whenActive(aNil)/labeled
      ├─ timeline.nim: initLinearTimeline()  # labels+actions → Clip segments
      └─ render/format.nim: makeMedia()      # encode via linked libav
```

### Algorithms (with numbers)

- **Audio analysis (`src/analyze/audio.nim`):** PEAK amplitude, not RMS —
  decode → resample S16 → per timebase chunk `max(|sample|)/32767` across all
  channels (SIMD: SSE2/NEON/WASM paths). Chunking = one video frame (33 ms @
  30 fps, ~1600 samples @ 48 kHz). Default `defaultAudioThres = 0.04` →
  **≈ −27.96 dBFS peak** per frame-window. `parseThres` accepts bare float, `%`
  (÷100), or `dB` (10^(num/20)), clamped [0,1]. Results disk-cached by
  SHA-1(path, mtime, size, tb, args).
- **Motion analysis (`src/analyze/motion.nim`):** decode → libavfilter chain
  `scale={width}:-1,format=gray,gblur=sigma={blur}` (optional crop first) →
  **count differing pixels vs previous frame** (exact byte equality, 16-byte
  SIMD compare) → `diffCount/totalPixels`. First frame = 0. Defaults:
  threshold **0.02 (2% pixels differ)**, width=400, blur=9, full-frame region.
- **Blackdetect:** luma ≤ `pixelBlack` (default 0.10 ≈ 25.5/255) on ≥ 0.98
  (98%) of pixels. Wrap in `not` to cut black frames.
- **Edit DSL:** `(audio [thr] [stream] [channel])` default 0.04/all/all;
  `(motion [thr] [stream] [w] [blur] [x y w h])` default 0.02/0/400/9/full;
  `(blackdetect …)`; `(subtitle pattern …)`; `(word value …)`.
  Operators `or`/`and`/`xor` (variadic), `not` (unary). `--edit:2`/`--when:2`
  add labels 2–255, merged priority-max. `--when-active` default nil (keep),
  `--when-inactive` default cut.
- **Margin & smoothing (`lib/editutil.nim`, pure functions):** `--margin 0.2s`
  (default, both sides; negative = shrink). `--smooth 0.2s,0.1s` (mincut 0.2 s,
  minclip 0.1 s): iterate to fixed point (2-cycle guard) — active runs <
  minclip → drop; inactive gaps < mincut → fill.
- **Transcription chunking (energy-gated):** mono f32 @ 16 kHz; 480-sample
  (30 ms) windows; peak ≥ 0.04 starts utterance with 0.2 s preroll; ends after
  0.5 s trailing silence (`SilenceGap = Rate div 2`); discard < 0.2 s speech
  (`MinSpeech`); cap 30 s segments; worker thread + 16-deep queue.
- **Render:** `smartRenderPlan()` — copy complete GOPs stream-copy, re-encode
  only fragments touching edit points (partial-lossless H.264/HEVC/VP9).
  **No subtitle burn-in anywhere** (`grep burn` = 0 hits) — subtitles are
  remuxed/retimed only.
- **License gates:** unlicensed single-source render capped 3200×1800
  (auto-downscale); multi-source capped at SD.

### Dependencies

Build: nim ≥ 2.2.2 + nimcrypto 0.7.3; FFmpeg/whisper.cpp/Parakeet compiled
from source by nimble tasks. Runtime models user-supplied (whisper/Parakeet
GGUF; Apple Speech on macOS 26+). **No ffmpeg CLI binary shipped or used.**

### Tests

**Zero.** `nimble test` targets a non-existent `tests/unit`; `tests/` does not
exist in the checkout. CI only smoke-builds. All threshold/lexer behavior is
untested upstream.

### License / compliance

**Unlicense — verified** (`LICENSE` contains the canonical public-domain
dedication). Legally reusable for ClipForge2 with zero attribution
requirement. Practically, Nim code can't be reused in a Python project —
only the design/parameters transfer.

### Reuse / adapt / rewrite verdict

- **ADAPT (highest value):** `mutMargin()` + `smoothing()` — tiny, pure,
  battle-tested; port almost verbatim with defaults margin 0.2 s / mincut
  0.2 s / minclip 0.1 s (per-niche tunable; note fixed-point iteration).
- **ADAPT:** audio peak recipe (decode→S16→per-window peak→threshold), but for
  Shorts prefer **RMS or RMS+peak hybrid at 100 ms windows** — their 0.04 peak
  (−28 dBFS) per-frame default is likely too sensitive for loud gaming audio;
  validate on real footage. Keep: all-stream OR, named channels, SHA-keyed
  disk cache.
- **ADAPT:** motion recipe verbatim as reference (400 px wide, gray, σ=9 blur,
  frame-diff pixel ratio, 0.02 threshold) — numpy on 400 px frames is fast
  enough in Python; region-of-interest crop maps to facecam-aware cropping.
- **ADAPT:** blackdetect (luma ≤ 10% on ≥ 98% pixels) — handy for cutting
  fade-to-black in gameplay.
- **REUSE concept:** priority-max multi-label merge — elegant way to let
  "LLM moment selection" outrank "silence detector" in ClipForge2.
- **REUSE semantics, REWRITE:** edit-DSL combination semantics
  (`or`/`and`/`not` over per-frame masks, threshold→bool); skip the parser —
  a Python dict/config is enough.
- **REWRITE:** timeline v3/actions model (overkill NLE engine), render engine
  (ffmpeg CLI instead), transcription (faster-whisper + Silero VAD, keeping
  their 0.5 s gap / 0.2 s preroll / 0.2 s min-speech numbers).
- **ADAPT:** `levels`-style diagnostic subcommand (dump per-window audio/
  motion levels as JSON/CSV) — cheap, high value for threshold tuning.

---


## Repo 3 — ClipsAI/clipsai (MIT)

Python library (pip install `clipsai`) for converting long videos into clips:
transcribe → find clips → reframe to 9:16. Designed for audio-centric
narrative content (podcasts, interviews, speeches, sermons). HEAD `8e73c8a`
(2024-01-17 — **stale ~2.7 years**, 15 open issues; a known crash issue
`#4` exists on `resize()`; the PyPI version is 0.2.1).

### Module / file map

- `clipsai/transcribe/` — `transcriber.py` (`Transcriber`), `transcription.py`
  (`Transcription` with char-level info), `transcription_element.py`
- `clipsai/clip/` — `clipfinder.py` (`ClipFinder`), `texttiler.py`
  (`TextTiler`), `text_embedder.py` (`TextEmbedder`), `clip.py` (`Clip`)
- `clipsai/resize/` — `resize.py` (orchestrator), `resizer.py` (`Resizer`),
  `crops.py` (`Crops`), `segment.py`, `rect.py`, `vid_proc.py`
  (`detect_scenes`, `extract_frames`), `img_proc.py`
- `clipsai/diarize/` — `pyannote.py` (`PyannoteDiarizer`, pyannote
  speaker-diarization-3.1)
- `clipsai/media/` — `editor.py` (`MediaEditor`), `video_file.py`,
  `audio_file.py`, `audiovideo_file.py`, `temporal_media_file.py`
- `clipsai/filesys/`, `clipsai/utils/` — file abstractions, config/type checking, torch helpers
- `tests/` — 6 files: `test_transcribe.py`, `test_resize.py`, `test_rect.py`,
  `test_files.py`, `test_diarize.py`, `test_clip.py`

### Data flow

```
Transcriber().transcribe(path)          # whisperx -> char-level Transcription
    -> ClipFinder().find_clips(transcription)   # TextTiling over sentence embeddings
    -> resize(video_path, auth_token, clips=...) # diarize + scenes + faces -> Crops
```

### Algorithms (with numbers)

- **Transcription:** WhisperX (`whisperx.load_model`, arch large-v2/float16 on
  CUDA else tiny/int8 CPU, batch_size=16) + `whisperx.load_align_model` word/char
  alignment; raises `NoSpeechError` when zero segments; output is char-level
  (`char_info` with start/end/speaker per char). 10 supported languages.
- **Clip finding (TextTiling — non-LLM, deterministic, offline):**
  sentences embedded with sentence-transformers `all-roberta-large-v1`
  (~1.4 GB model). Adjacent window embeddings pooled (mean/max), cosine gap
  scores computed, smoothed (width 3), converted to depth scores
  (peak-valley differences). Boundary where depth > cutoff; cutoff policies:
  `average` (avg), `high` (avg + std — default), `low` (avg − std).
  Multi-round hierarchical tiling: k ∈ [5,7] for <3 min clips, k ∈ [11,17]
  for 3+ min, k ∈ [37,53,73,97] for 10+ min. Dedupe: `_is_duplicate()` —
  reject when |Δstart| + |Δend| < 15 s vs any kept clip.
- **Active-speaker reframe:** pyannote speaker-diarization-3.1 (min segment
  duration 1.5 s) → scene detection (scenedetect) → FaceNet MTCNN face
  detection (margin 20 px, post_process False, detect at 960 px width,
  13 samples/segment, 8 GPU batches) → KMeans clustering of bounding boxes
  (k = max faces per frame) → per-cluster mouth movement via MediaPipe
  FaceMesh mouth-aspect-ratio (inner-lip landmarks, MAR = avg mouth height /
  width, summed |ΔMAR|); fallback = face with most frames. `_calc_crop()`
  centers the 9:16 crop on the ROI; adjacent segments merged when x/y differ
  < 4% of dims; scene changes within 0.25 s of segment edges get merged.
- **Model sizes/costs:** torch + whisperx + facenet-pytorch + mediapipe +
  pyannote.audio + sentence-transformers + scenedetect + nltk + opencv +
  scikit-learn. Heavy GPU-oriented stack; **pyannote requires a gated HF
  access token**; WhisperX installed separately from git
  (`whisperx@git+https://github.com/m-bain/whisperx.git`). Roughly multi-GB
  total download.

### Tests

6 pytest files covering transcribe/clip/resize/rect/diarize/file
abstractions. Modest coverage relative to repo size; integration-level
quality eval is absent (unlike cutawan's eval-clips).

### License / compliance

MIT (Copyright 2023 Clips AI, Inc.). Clean for reuse with attribution.
No copyleft dependencies in the audited core path.

### Reuse / adapt / rewrite verdict

- **Best idea to adapt:** TextTiling clip finding — a deterministic,
  offline, no-LLM-cost clip segmenter. Valuable as ClipForge2's
  **fallback when no LLM key is available** (instead of dumping the whole
  video). Needs rewriting around our word-level transcript and
  smaller/cheaper embedding models.
- **Adapt (simplified):** the speaker-aware reframe pipeline shape
  (diarize → scenes → face ROI → crop merge) — but ClipForge2 should use
  lighter pieces (Silero VAD + our own face tracking) rather than
  FaceNet+MediaPipe+pyannote, which are heavy and gated.
- **Do NOT reuse as-is:** `Transcriber` (whisperx is heavier and fussier
  than faster-whisper), the whole dependency stack (too heavy for our
  "beginner-friendly one-time setup" constraint), anything relying on the
  stale codebase (no commits since 2024-01-17).

---

## Repo 4 — sebetancurch/auto-caption (MIT)

Minimal Python tool (CLI + CustomTkinter GUI): faster-whisper transcription →
word grouping → animated karaoke/pop captions burned into the video.
HEAD `b4e7cf6` (2026-07-17); 7 stars, 1 open issue.

### Module / file map

- `autocaption/cli.py` (+ `__main__.py`) — argparse CLI entry
- `autocaption/gui.py` — CustomTkinter GUI
- `autocaption/transcribe.py` — `transcribe()` (Faster-Whisper)
- `autocaption/grouping.py` — `group_words()` (caption-line grouping)
- `autocaption/ass_builder.py` — `build_ass()` (ASS karaoke/pop generation)
- `autocaption/srt_builder.py` — `build_srt()` (DaVinci-compatible SRT)
- `autocaption/styles.py` — `PRESETS` (2 caption styles)
- `autocaption/media.py` — `find_tool()`, `probe_video_size()`, `burn_subtitles()`
- `autocaption/fonts/` — bundled Anton font
- `tests/test_core.py` — pure-logic pytest tests

### Data flow

`transcribe()` (word timestamps) → `group_words()` (CaptionLine list) →
`build_ass()` (1080×1920 reference scale) → `burn_subtitles()` (FFmpeg
`ass=` filter, libx264 CRF 18 / h264_nvenc) → captioned MP4 + `.ass` + `.srt`.

### Algorithms (with numbers)

- **Transcription:** `faster_whisper.WhisperModel`, `word_timestamps=True`,
  `vad_filter=True`. Device auto: CUDA → float16 (after verifying cuBLAS 12 +
  cuDNN 9 DLLs genuinely usable on Windows), else CPU int8. Models:
  medium/medium.en on GPU, small/small.en on CPU.
- **Grouping:** `group_words()` defaults — max **4 words** per line, max
  **18 chars**, break after gaps > 0.6 s, break on sentence punctuation.
  `_snap_gaps()` extends each word's active interval to the next word's start
  so the highlight never blinks off between words.
- **ASS rendering (two modes):**
  - **pop:** one dialogue event per word interval; whole line shown, active
    word recolored + `\t` pop 115→100 over 60 ms; supports fades.
  - **karaoke:** one event per line, ASS `\kf` sweep; each word duration
    converted to centiseconds.
  Font sizes computed from height/1920 scale; `WrapStyle 2`.
- **Styles:** only 2 presets (`pop`, `karaoke`) — thin vs cutawan's 12.
- **Burn-in:** `ass=filename=…:fontsdir=…`, libx264 `-crf 18 -preset medium`
  or `h264_nvenc -preset p5 -cq 19`, audio copy, `+faststart`.
  `find_tool()` locates ffmpeg/ffprobe via PATH with OS-specific hints
  (winget/brew/apt); `_filter_path()` escapes Windows paths.
- **SRT:** DaVinci-compatible — one block per word with `<font color>` on the
  active word (DaVinci parses `<font color>` tags).
- CLI options: style, model, device (auto/cuda/cpu), language, highlight
  color, font, font size, caps on/off, words-per-line, max-chars, position
  (high 0.32 / mid 0.50 / low 0.68 of frame height).

### Tests

`tests/test_core.py` — pure-logic tests only (timestamp formatting
`ass_time`/`srt_time`, BGR color conversion, grouping break rules,
`_filter_path`). No ffmpeg/whisper/GUI tests. Light but honest coverage of
the pure functions.

### Dependencies

`faster-whisper>=1.0.0`, `customtkinter>=5.2.0`, `tkinterdnd2>=0.4.0`;
dev: pytest. Minimal footprint — installs in minutes.

### License / compliance

MIT (Copyright 2026 Sergio Betancur Chaves). Bundled Anton font is OFL.
Clean.

### Reuse / adapt / rewrite verdict

- **Adapt (closest to ClipForge2's caption engine):** the
  transcribe→group→ASS→burn data flow and `_snap_gaps()` no-blink trick are
  directly reusable logic. Its per-word pop event pattern matches our karaoke
  caption needs better than cutawan's heavier system.
- **Extend:** only 2 styles — ClipForge2 needs ~10+ styles (we can take
  cutawan's 12-preset parameter space as the design reference).
- **Adopt the pattern, not the code:** `find_tool()` platform-aware ffmpeg
  discovery is a good pattern for our one-time setup wizard (Windows/macOS/
  Linux).
- **Do NOT reuse:** CustomTkinter GUI (we're building a web dashboard).

---

## Repo 5 — fyrek1d/yt-automation (MIT) — upload/scheduling focus

Fully automated faceless-Shorts pipeline (Reddit stories + TTS over Minecraft
gameplay → YouTube/TikTok/Instagram). Audited ONLY for its upload/scheduling
orchestration. HEAD `f288023` (2026-08-18); 1 star, 0 open issues, branch `main`.

### Module / file map (upload-relevant)

- `src/uploader.py` — `YouTubeUploader` (OAuth + resumable upload + retries)
- `src/auth_upload.py` — one-time **manual paste-back OAuth** flow
- `src/gen_auth.py` / `src/finish_auth.py` / `src/exchange_upload.py` — auth helpers
- `src/crosspost.py` — `TikTokClient`, `InstagramClient` (official APIs)
- `src/auth_tiktok.py` / `src/auth_instagram.py` — per-platform one-time OAuth
- `src/main.py` — pipeline orchestration: scrape → TTS → render → upload → crosspost
- `src/dashboard.py` — Flask web dashboard (schedule display, settings, logs)
- `src/cleanup.py` — retention-based artifact pruning
- `src/delete_video.py` / `src/reupload.py` — video management helpers
- `config/config.json` — channel, metadata, crosspost, cleanup config

### Data flow (upload path)

1. `main()` acquires `logs/run.lock` (PID file). If the lock exists, it checks
   `os.kill(pid, 0)`: dead PID → stale lock removed; live PID → abort with
   error. Empty/non-digit lock → treated as stale (bugfix from an Aug 2026
   ENOSPC crash). `atexit` releases the lock if this process still owns it.
2. **Watchdog:** `threading.Timer` armed for `PIPELINE_MAX_RUN_SECONDS` (default
   2700 s = 45 min) → force-exits (`os._exit(3)`) so a hung TTS/network call
   never blocks the next scheduled cron.
3. Pipeline runs; `cleanup_old_outputs()` prunes old artifacts first
   (retention_days, default 7 — "videos only live on YouTube").
4. YouTube upload via `YouTubeUploader.upload()`; on success
   `scraper.mark_posted()` + `_record_published()` appends to
   `logs/published.json` (dashboard display, capped at 200 records).
5. `_crosspost()` → TikTok and/or Instagram. **Cross-post failures are logged
   as warnings and NEVER abort the run** (YouTube already succeeded).
6. CLI flags: `--no-upload` (dry run), `--privacy` override,
   `--story-file`, `--no-crosspost`.

### Key logic

- **YouTube OAuth:** scope `https://www.googleapis.com/auth/youtube.upload`.
  `Credentials.from_authorized_user_file(token.json)` → if expired and a
  refresh token exists, `creds.refresh(Request())`; else
  `InstalledAppFlow.run_local_server(host="127.0.0.1", bind_addr="127.0.0.1",
  port=0, timeout_seconds=300)` — note the explicit 127.0.0.1 (not
  "localhost") because some browsers resolve localhost to IPv6 first.
  `auth_upload.py` additionally supports a **manual paste-back flow** for
  browsers that can't reach the localhost callback: auth URL written to
  `logs/auth_url.txt`, user pastes the callback URL into
  `config/oauth_paste.txt` (2 s poll, 10 min timeout), `flow.fetch_token()`
  → `token.json`.
- **YouTube upload retry:** `videos().insert(part="snippet,status")` with
  `MediaFileUpload(chunksize=1 MB, resumable=True)`; `request.next_chunk()`
  loop; on exception — `HttpError` with status **< 500 → raise immediately**
  (auth/validation errors are not retried); other errors retried up to **5
  times** with linear backoff `attempts * 5` s (5/10/15/20/25 s). Title
  truncated to 100 chars, description to 4950. Thumbnail set via
  `thumbnails().set()` — failure is non-fatal (logged only).
- **Shorts gate:** render is probed with moviepy; aborts if not portrait
  (`size[1] <= size[0]` → RuntimeError, no upload).
- **TikTok (Content Posting API, Direct Post):** `creator_info/query` →
  validate requested `privacy_level` against allowed options (fallback
  `SELF_ONLY`); `video/init` → chunked PUT upload (64 MB chunks,
  `Content-Range: bytes s-e/size`, 600 s timeout) → `video/publish` →
  `status/fetch` polling every 10 s up to 600 s until `PUBLISH_COMPLETE`
  (raise on `FAILED`). Token refresh 5 min before expiry; `is_aigc=True`
  flag set; title ≤ 2200 chars.
- **Instagram (Graph API v24.0, FB Login for Business):** code → short-lived
  FB token → `fb_exchange_token` grant → long-lived (~60 days) → find managed
  FB Page with linked IG Business account. Upload: `/{ig_id}/media` container
  (`media_type=REELS`, `upload_type=resumable`, `caption` ≤ 2200,
  `thumb_offset`) → POST bytes to resumable URI (`Authorization: OAuth`,
  `offset`, `file_size`) → poll `status_code` every 15 s up to 600 s until
  `FINISHED` (raise on `ERROR`/`EXPIRED`) → `media_publish`. Long-lived token
  refreshed via re-exchange within 5 days of expiry.
- **Scheduling:** system **cron** (e.g. `0 9,15,21 * * *`, 3×/day). The
  dashboard reads `crontab -l` and renders human-readable times
  (`_cron_schedule()` + `_format_schedule()`; in Docker reads a bind-mounted
  `host-crontab`). The repo also ships a Flask dashboard (run logs, recent
  posts, settings editor, on-demand cleanup).

### Dependencies

`google-api-python-client>=2.100.0`, `google-auth-httplib2`, `google-auth-oauthlib`,
`requests`, `praw`, TTS engines (ElevenLabs/Kokoro/edge-tts/gTTS), `moviepy`,
`faster-whisper`, `flask`, `python-dotenv`. No test files found.

### License / compliance

MIT (Copyright 2026 "yt-automation contributors", anonymized). Google/TikTok/
Meta API terms apply at runtime (quota, app review for TikTok `video.publish`
beyond SELF_ONLY). Clean for reuse with attribution.

### Reuse / adapt / rewrite verdict

- **Adapt heavily (the upload module is the best reference found):**
  ClipForge2's YouTube upload should mirror this design — lazy auth with
  refresh, resumable 1 MB-chunk uploads, retry 5xx/network only (≤5 attempts,
  backoff), non-fatal thumbnail failure, `--no-upload` dry run. The manual
  paste-back OAuth flow is worth adapting for headless/server environments.
- **Reuse the pattern:** `run.lock` singleton + stale-PID detection + watchdog
  timer for scheduled runs; cross-post failures never aborting the pipeline;
  `published.json`-style append-only post log for the dashboard.
- **Adapt the scheduling UI:** dashboard reading real cron lines and showing
  human times — good UX pattern for our scheduler tab.
- **Do NOT copy verbatim:** TikTok/Instagram clients are solid but tied to
  this repo's config layout; rewrite against our config schema. Also note:
  this repo has **no tests** and its niche (Reddit stories) is irrelevant —
  only the upload machinery transfers.

---

## Conclusions

### Top 5 reusable pieces (adapt into ClipForge2)

1. **Silence/filler-removal parameter set** (cutawan + auto-editor).
   Silero VAD thresholds (0.5/0.35, min speech 0.25 s, min silence 0.1 s,
   padding 0.03 s) + tighten policy (trim pauses > 0.7 s, 0.18 s pre-roll /
   0.3 s post-roll, ignore removals < 0.35 s, gap-bridging) + auto-editor's
   margin/smoothing (0.2 s margin, mincut 0.2 s / minclip 0.1 s, fixed-point).
   These numbers are the best-calibrated starting point found anywhere.
2. **Virality scoring rubric + dedupe + snapping** (cutawan).
   Hook 0–30 / Emotion 0–25 / Value 0–20 / Structure 0–15 / Shareability 0–9,
   40%-of-shorter overlap dedupe, sentence/word boundary snapping, second-pass
   ending review. Use as the LLM prompt rubric for moment selection.
3. **ASS caption pipeline design** (auto-caption + cutawan).
   transcribe→group→ASS→burn data flow, per-word-interval events with active
   word emphasis, `_snap_gaps()` no-blink trick, 1080×1920 reference scale,
   libx264 CRF 18 burn-in. Cutawan's 12-preset parameter space gives us the
   style design; auto-caption gives the minimal working implementation.
4. **YouTube upload module design** (yt-automation).
   Lazy auth + refresh, resumable 1 MB-chunk uploads, retry 5xx/network only
   (≤ 5 attempts, linear backoff), non-fatal thumbnail set, `--no-upload` dry
   run, manual paste-back OAuth for headless setups, `run.lock` singleton +
   watchdog timer, cross-post failures never aborting the run.
5. **TextTiling offline clip fallback** (clipsai).
   Deterministic, no-LLM-cost topic segmentation (sentence embeddings →
   gap/depth scores → cutoff avg+std → multi-round k). Use as the fallback
   clip finder when no LLM key is available, replacing "dump whole video".

### Top 5 pieces to build ourselves

1. **Captions-first / range-only ingest.** cutawan downloads the full video;
   our differentiator (transcribe or captions first, download only selected
   ranges) exists nowhere in the audited repos.
2. **Caption style engine (10+ styles, random-per-clip).** auto-caption has 2,
   cutawan has 12 TS presets — we need our own parameterised Python engine.
3. **Smart crop / speaker tracking for 9:16.** Lightweight Python path
   (Silero VAD + YuNet/UltraFace + IOU tracking) rather than cutawan's
   Electron/ONNX stack or clipsai's FaceNet+MediaPipe+pyannote heavyweights.
4. **Full pipeline orchestration** (niche → discovery → selection → edit →
   metadata → upload → schedule → analytics). yt-automation's orchestration
   patterns transfer, but its Reddit niche logic doesn't; our 40-niche system
   and autopilot modes are custom.
5. **Setup wizard + beginner dashboard.** None of the audited repos has a
   one-time setup wizard for non-technical users; the dashboard (cron
   schedule display, preview/quality gate, cost tracking) is ours to design,
   borrowing UX patterns from yt-automation's dashboard and cutawan's React UI.

### License gotchas

- **All five repos are permissive — no copyleft in the audit.**
  cutawan: MIT. auto-editor: Unlicense (public domain — zero attribution
  required; verified canonical text). clipsai: MIT (2023 Clips AI, Inc.).
  auto-caption: MIT. yt-automation: MIT (anonymized "contributors").
- **Standing rule (Ashu, 2026-09-24): if copying would cause any problem,
  study the logic and write our own code.** Even where licenses permit
  copying, the plan is adapt/rewrite — so license risk is minimal. Where we
  do adapt algorithms closely, keep an attribution note in code comments.
- **Bundled assets to double-check at build time:** cutawan's OFL fonts
  (Anton/Poppins — fine) and its LR-ASD ONNX weights (exported from
  MIT-licensed weights — fine). We will NOT bundle these; if we ship fonts,
  use OFL fonts with license files included.
- **Gated/runtime requirements (not license issues, but adoption blockers):**
  clipsai's pyannote needs a HuggingFace access token (gated model);
  TikTok's `video.publish` beyond SELF_ONLY needs app audit; YouTube Data
  API uploads need OAuth consent + quota. None of these transfer by copying
  code — each requires our own credentials/app setup.
- **auto-caption and yt-automation are the legally simplest to lift code
  from** (MIT, tiny, no bundled models); cutawan's value is algorithmic, not
  copyable (TypeScript/Electron); auto-editor's value is parameter/design
  (Nim); clipsai's deps are the heaviest (multi-GB GPU stack) — avoid
  importing that stack into ClipForge2.

### Model sizes / cost notes (verified facts + clearly-marked estimates)

- Verified: clipsai pins heavy GPU deps (torch, whisperx, facenet-pytorch,
  mediapipe, pyannote.audio, sentence-transformers); clipsai's TextEmbedder
  loads `all-roberta-large-v1` (~1.4 GB — **estimate from model card, not the
  repo**). yt-automation's setup.sh downloads ~350 MB Kokoro TTS models
  (per its README).
- auto-caption: faster-whisper medium (~1.5 GB) on GPU / small (~460 MB) on
  CPU — standard faster-whisper model sizes.
- Cutawan: Whisper chunked transcription; LLM passes are API-based (cost =
  transcript tokens, "a few cents per project" per their eval script docs).
- auto-editor: whisper.cpp/Parakeet GGUF models user-supplied (no
  auto-download wired); Apple Speech downloads on first use (macOS 26+).
- ClipForge2 implication: keep the default install CPU-friendly
  (faster-whisper small/base, Silero VAD ~2 MB) and make GPU models optional.

---

*End of Phase 0 audit. No implementation code was written; nothing was copied
into ClipForge2. Next: architecture design (Phase 1).*
