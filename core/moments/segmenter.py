"""Offline (no-LLM, zero-cost) topic segmentation for ClipForge.

TextTiling-lite, adapted from the Phase 0 audit of clipsai's TextTiler
(sentence embeddings -> gap scores -> depth scores -> cutoff) — rewritten
with a lightweight TF-IDF + numpy stack instead of sentence-transformers +
torch, so offline mode installs in seconds and costs nothing.

The TF-IDF here is vendored in pure numpy (no scikit-learn dependency):
same token pattern, same english stop words, same smoothed IDF and L2
row normalisation as sklearn's TfidfVectorizer defaults. This keeps the
frozen one-click installer small and guarantees offline mode works with
zero heavy dependencies.

Honesty contract: offline scores measure TOPIC-SHIFT STRENGTH (how strongly a
boundary separates two topics), not virality. They are real computed values,
documented as such — never presented as engagement predictions.
"""

from __future__ import annotations

import logging
import math
import re

import numpy as np

from .scorer import Clip, group_sentences

log = logging.getLogger("clipforge.moments.segmenter")

# --- vendored TF-IDF (sklearn-compatible defaults, pure numpy) ---------------
# Token pattern (?u)\\b\\w\\w+\\b  == sklearn's default: 2+ word chars.
_TOKEN_RE = re.compile(r"(?u)\b\w\w+\b")

# sklearn's ENGLISH_STOP_WORDS (318 words), vendored so the
# frozen app needs no scikit-learn at all.
_ENGLISH_STOP_WORDS = frozenset({
    'a', 'about', 'above', 'across', 'after', 'afterwards', 'again', 'against',
    'all', 'almost', 'alone', 'along', 'already', 'also', 'although', 'always',
    'am', 'among', 'amongst', 'amoungst', 'amount', 'an', 'and', 'another',
    'any', 'anyhow', 'anyone', 'anything', 'anyway', 'anywhere', 'are', 'around',
    'as', 'at', 'back', 'be', 'became', 'because', 'become', 'becomes',
    'becoming', 'been', 'before', 'beforehand', 'behind', 'being', 'below', 'beside',
    'besides', 'between', 'beyond', 'bill', 'both', 'bottom', 'but', 'by',
    'call', 'can', 'cannot', 'cant', 'co', 'con', 'could', 'couldnt',
    'cry', 'de', 'describe', 'detail', 'do', 'done', 'down', 'due',
    'during', 'each', 'eg', 'eight', 'either', 'eleven', 'else', 'elsewhere',
    'empty', 'enough', 'etc', 'even', 'ever', 'every', 'everyone', 'everything',
    'everywhere', 'except', 'few', 'fifteen', 'fifty', 'fill', 'find', 'fire',
    'first', 'five', 'for', 'former', 'formerly', 'forty', 'found', 'four',
    'from', 'front', 'full', 'further', 'get', 'give', 'go', 'had',
    'has', 'hasnt', 'have', 'he', 'hence', 'her', 'here', 'hereafter',
    'hereby', 'herein', 'hereupon', 'hers', 'herself', 'him', 'himself', 'his',
    'how', 'however', 'hundred', 'i', 'ie', 'if', 'in', 'inc',
    'indeed', 'interest', 'into', 'is', 'it', 'its', 'itself', 'keep',
    'last', 'latter', 'latterly', 'least', 'less', 'ltd', 'made', 'many',
    'may', 'me', 'meanwhile', 'might', 'mill', 'mine', 'more', 'moreover',
    'most', 'mostly', 'move', 'much', 'must', 'my', 'myself', 'name',
    'namely', 'neither', 'never', 'nevertheless', 'next', 'nine', 'no', 'nobody',
    'none', 'noone', 'nor', 'not', 'nothing', 'now', 'nowhere', 'of',
    'off', 'often', 'on', 'once', 'one', 'only', 'onto', 'or',
    'other', 'others', 'otherwise', 'our', 'ours', 'ourselves', 'out', 'over',
    'own', 'part', 'per', 'perhaps', 'please', 'put', 'rather', 're',
    'same', 'see', 'seem', 'seemed', 'seeming', 'seems', 'serious', 'several',
    'she', 'should', 'show', 'side', 'since', 'sincere', 'six', 'sixty',
    'so', 'some', 'somehow', 'someone', 'something', 'sometime', 'sometimes', 'somewhere',
    'still', 'such', 'system', 'take', 'ten', 'than', 'that', 'the',
    'their', 'them', 'themselves', 'then', 'thence', 'there', 'thereafter', 'thereby',
    'therefore', 'therein', 'thereupon', 'these', 'they', 'thick', 'thin', 'third',
    'this', 'those', 'though', 'three', 'through', 'throughout', 'thru', 'thus',
    'to', 'together', 'too', 'top', 'toward', 'towards', 'twelve', 'twenty',
    'two', 'un', 'under', 'until', 'up', 'upon', 'us', 'very',
    'via', 'was', 'we', 'well', 'were', 'what', 'whatever', 'when',
    'whence', 'whenever', 'where', 'whereafter', 'whereas', 'whereby', 'wherein', 'whereupon',
    'wherever', 'whether', 'which', 'while', 'whither', 'who', 'whoever', 'whole',
    'whom', 'whose', 'why', 'will', 'with', 'within', 'without', 'would',
    'yet', 'you', 'your', 'yours', 'yourself', 'yourselves',
})

def _tfidf_matrix(texts: list[str]) -> np.ndarray:
    """TF-IDF matrix, rows L2-normalised.

    Matches sklearn TfidfVectorizer(stop_words="english", lowercase=True)
    defaults: raw term counts, smooth IDF ``ln((1+n)/(1+df)) + 1``,
    L2 row normalisation.
    """
    docs: list[list[str]] = []
    doc_freq: dict[str, int] = {}
    for text in texts:
        toks = [w for w in _TOKEN_RE.findall(text.lower())
                if w not in _ENGLISH_STOP_WORDS]
        docs.append(toks)
        for w in set(toks):
            doc_freq[w] = doc_freq.get(w, 0) + 1
    vocab = {w: i for i, w in enumerate(doc_freq)}
    n = len(texts)
    mat = np.zeros((n, len(vocab)), dtype=float)
    for i, toks in enumerate(docs):
        for w in toks:
            mat[i, vocab[w]] += 1.0
    if vocab:
        df = np.array([doc_freq[w] for w in vocab], dtype=float)
        idf = np.log((1.0 + n) / (1.0 + df)) + 1.0
        mat *= idf
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return mat / norms

def _window_mean(vecs: np.ndarray, lo: int, hi: int) -> np.ndarray:
    seg = vecs[lo:hi]
    if seg.shape[0] == 0:
        return np.zeros(vecs.shape[1])
    return seg.mean(axis=0)


def segment_offline(
    transcript: list[dict],
    clips_per_minute: float = 1.0,
    min_clip_sec: float = 20.0,
    max_clip_sec: float = 180.0,
    window: int = 3,
) -> list[Clip]:
    """Split transcript into topic segments without any LLM call.

    Returns Clips sorted by boundary-strength score desc. Score semantics:
    topic-shift strength 0-100, NOT virality — see module docstring.

    Needs nothing but numpy — the TF-IDF is vendored above, so this works
    in the frozen app with zero heavy dependencies.
    """
    sentences = group_sentences(transcript)
    if len(sentences) < 6:
        log.warning("segment_offline: only %d sentences -> no segments", len(sentences))
        return []
    duration = sentences[-1].end
    target = max(1, min(40, round(duration / 60 * clips_per_minute)))

    texts = [s.text for s in sentences]
    vecs = _tfidf_matrix(texts)
    n = len(sentences)

    # Gap score at each inter-sentence boundary: cosine similarity of the
    # mean TF-IDF vectors of `window` sentences on each side.
    gaps = np.zeros(n - 1)
    for i in range(n - 1):
        left = _window_mean(vecs, max(0, i - window + 1), i + 1)
        right = _window_mean(vecs, i + 1, min(n, i + 1 + window))
        denom = np.linalg.norm(left) * np.linalg.norm(right)
        gaps[i] = float(left @ right / denom) if denom > 0 else 0.0

    # Smooth (width 3) then depth = valley depth vs neighbours.
    sm = gaps.copy()
    for i in range(1, n - 2):
        sm[i] = (gaps[i - 1] + gaps[i] + gaps[i + 1]) / 3.0
    depth = np.zeros(n - 1)
    for i in range(n - 1):
        l = sm[i - 1] if i > 0 else sm[i]
        r = sm[i + 1] if i < n - 2 else sm[i]
        depth[i] = max(0.0, (l - sm[i]) + (r - sm[i]))

    cutoff = float(depth.mean() + depth.std())
    max_depth = float(depth.max()) or 1.0

    # Pick boundaries: all above cutoff, but at most target-1, strongest first,
    # then re-sorted by position. Document edges (first/last gap) are never
    # topic boundaries — there is nothing on one side to contrast — so they
    # are excluded from candidacy. Merge segments shorter than min_clip_sec.
    ranked = sorted(
        (i for i in range(1, n - 2) if depth[i] > cutoff),
        key=lambda i: depth[i],
        reverse=True,
    )[: max(0, target - 1)]
    bounds = sorted(ranked)
    bounds = _merge_short(bounds, sentences, depth, min_clip_sec)

    clips: list[Clip] = []
    edges = [0] + [b + 1 for b in bounds] + [n]
    for j in range(len(edges) - 1):
        a, b = edges[j], edges[j + 1]
        s, e = sentences[a].start, sentences[b - 1].end
        if e - s < 5.0:
            continue
        # Boundary strength of this clip = mean of its two edge depths.
        d_vals = []
        if j > 0:
            d_vals.append(depth[bounds[j - 1]])
        if j < len(bounds):
            d_vals.append(depth[bounds[j]])
        strength = (sum(d_vals) / len(d_vals) / max_depth) if d_vals else 0.0
        first_text = sentences[a].text
        clips.append(
            Clip(
                start=round(s, 2),
                end=round(e, 2),
                score=int(round(100 * strength)),
                title=(first_text[:57] + "...") if len(first_text) > 60 else first_text,
                hook_line=first_text[:160],
                reason=(
                    "offline topic segment "
                    f"(boundary strength {int(round(100 * strength))}/100; "
                    "not a virality prediction)"
                ),
                source="offline",
            )
        )
    clips.sort(key=lambda c: c.score, reverse=True)
    log.info(
        "segment_offline: %d sentences -> %d clips (cutoff %.3f)",
        n,
        len(clips),
        cutoff,
    )
    return clips


def _merge_short(
    bounds: list[int], sentences, depth: np.ndarray, min_clip_sec: float
) -> list[int]:
    """Drop boundaries that would create clips shorter than min_clip_sec.

    Iteratively removes the weakest boundary adjacent to a too-short segment.
    """
    bounds = sorted(bounds)
    changed = True
    while changed and bounds:
        changed = False
        edges = [0] + [b + 1 for b in bounds] + [len(sentences)]
        for j in range(len(edges) - 1):
            a, b = edges[j], edges[j + 1]
            dur = sentences[b - 1].end - sentences[a].start
            if dur < min_clip_sec:
                # Remove the weaker of the two flanking boundaries.
                cands = []
                if j > 0:
                    cands.append(bounds[j - 1])
                if j < len(bounds):
                    cands.append(bounds[j])
                weakest = min(cands, key=lambda i: depth[i])
                bounds.remove(weakest)
                changed = True
                break
    return sorted(bounds)
