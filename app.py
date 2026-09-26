"""ClipForge — beginner-friendly local web app.

Dashboard with zero forced setup — first run writes sensible defaults and
goes straight in. One launch command, everything else is
buttons — no terminal needed for the user.

    python app.py

Opens http://127.0.0.1:5057 in the browser automatically.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from core import config as cfg_mod
from core import niches as niches_mod
from core.paths import is_frozen, resource_path
from core.analytics import (AnalyticsStore, build_insights,
                            tuning_suggestions)
from core.discovery import SeenStore, discover_for_autopilot
from core.edit import AVAILABLE_STYLES, resolve_style
from core.edit.pipeline import build_short
from core.ingest import download_ranges, ingest
from core.ingest.ytdlp_helper import extract_video_id
from core.metadata import generate_for_clips
from core.moments import detect_moments_with_usage
from core.moments import llm as llm_mod
from core.moments import llm_keys as llm_keys_mod
from core.moments.llm import LLMError
from core.upload import oauth as yt_oauth
from core.upload import quota_status as yt_quota_status
from core.upload import upload_clip as yt_upload_clip

log = logging.getLogger("clipforge.app")

BASE_DIR = resource_path()
PREVIEWS_DIR = BASE_DIR / "previews"

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def app_dir() -> Path:
    d = cfg_mod.config_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def clips_dir() -> Path:
    d = app_dir() / "clips"
    d.mkdir(parents=True, exist_ok=True)
    return d


def jobs_dir() -> Path:
    d = app_dir() / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


_analytics_store: "AnalyticsStore | None" = None
_analytics_lock = threading.Lock()


def _analytics() -> "AnalyticsStore":
    """Process-wide analytics store (lazy, thread-safe)."""
    global _analytics_store
    with _analytics_lock:
        if _analytics_store is None:
            _analytics_store = AnalyticsStore()
        return _analytics_store


def friendly_error(exc: Exception) -> str:
    """Turn technical errors into plain-language messages. Never fake."""
    msg = str(exc) or type(exc).__name__
    low = msg.lower()
    if "not a bot" in low or "sign in to confirm" in low:
        return ("YouTube is blocking downloads from this network right now "
                "(bot check). Wait a while and try again — your clips are safe.")
    if "429" in low or "too many requests" in low:
        return ("YouTube rate-limited us (too many requests). "
                "Wait 10–15 minutes and try again.")
    if isinstance(exc, LLMError) or "api key" in low or "gemini" in low:
        return ("AI service unavailable — set a GEMINI_API_KEY to enable "
                "AI scoring and AI titles. Offline mode still works.")
    if "ffmpeg" in low and "not found" in low:
        return "ffmpeg is not installed on this computer — clips can't be built."
    if "no such file" in low or "not found" in low:
        return f"A needed file went missing: {msg}"
    return f"Something went wrong: {msg}"


def synth_words(transcript: list[dict], start: float, end: float) -> list[dict]:
    """Cue-level transcript -> evenly-spread word timings, clip-relative.

    Honest approximation when word-level timestamps aren't available.
    """
    words: list[dict] = []
    for cue in transcript:
        cs, ce = float(cue["start"]), float(cue["end"])
        s, e = max(cs, start), min(ce, end)
        if e <= s:
            continue
        tokens = str(cue.get("text", "")).split()
        if not tokens:
            continue
        per = (e - s) / len(tokens)
        for i, tok in enumerate(tokens):
            ws, we = s + i * per, s + (i + 1) * per
            words.append({"start": ws - start, "end": we - start, "text": tok})
    return words


# ---------------------------------------------------------------------------
# background job manager
# ---------------------------------------------------------------------------

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _new_job(url: str, niche_id: str, custom_niche: str,
             caption_style: str, max_clips: int, cfg: dict) -> dict:
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "url": url,
        "niche_id": niche_id,
        "custom_niche": custom_niche,
        "caption_style": caption_style,
        "max_clips": max_clips,
        "mode": cfg.get("mode", "manual"),
        "quality_gate": int(cfg.get("quality_gate", 50)),
        "status": "queued",      # queued|running|done|failed
        "step": "Starting…",
        "progress": 0,           # 0-100
        "clips": [],
        "error": None,
        "notes": [],
        "created": time.time(),
    }
    with _jobs_lock:
        _jobs[job_id] = job
    t = threading.Thread(target=_run_job, args=(job_id,), daemon=True)
    t.start()
    return job


def _job_update(job_id: str, **kw):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job:
            job.update(kw)


def _run_job(job_id: str):
    with _jobs_lock:
        job = _jobs[job_id]
    try:
        _run_pipeline(job)
    except Exception as exc:  # never crash the thread silently
        log.error("job %s failed: %s\n%s", job_id, exc, traceback.format_exc())
        _job_update(job_id, status="failed", error=friendly_error(exc),
                    step="Failed", progress=100)
    finally:
        _persist_job(job_id)


def _persist_job(job_id: str):
    with _jobs_lock:
        job = dict(_jobs.get(job_id, {}))
    if not job:
        return
    slim = {k: v for k, v in job.items() if k != "notes"}
    try:
        (jobs_dir() / f"{job_id}.json").write_text(
            json.dumps(slim, indent=2, ensure_ascii=False)[:200_000],
            encoding="utf-8")
    except OSError:
        pass


def _load_jobs():
    """Reload persisted jobs into memory on startup.

    Without this, every app restart wipes the dashboard: the clips' mp4
    files are still on disk but unreachable. In-flight jobs from a previous
    run are marked interrupted (their worker thread is gone).
    """
    try:
        files = sorted(jobs_dir().glob("*.json"))
    except OSError:
        return
    loaded = 0
    for p in files:
        try:
            job = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(job, dict) or not job.get("id"):
            continue
        if job.get("status") in ("queued", "running"):
            job["status"] = "interrupted"
            job["step"] = "Interrupted by app restart"
            job["error"] = ("The app was restarted while this job was running. "
                            "Start a new job to rebuild the clips.")
        with _jobs_lock:
            _jobs[job["id"]] = job
        loaded += 1
    if loaded:
        log.info("reloaded %d job(s) from %s", loaded, jobs_dir())


_load_jobs()


def _run_pipeline(job: dict):
    jid = job["id"]
    url = job["url"]
    niche = job["custom_niche"] or niches_mod.niche_name(job["niche_id"])
    max_clips = job["max_clips"]
    style_choice = job["caption_style"]

    def step(text, pct):
        _job_update(jid, step=text, progress=pct, status="running")

    # 1. transcript (captions first, whisper fallback)
    step("Getting the video's transcript…", 5)
    res = ingest(url)
    transcript = res.get("transcript") or []
    if not transcript:
        raise RuntimeError("no transcript: video has no captions and "
                           "transcription failed")
    if res.get("source") == "whisper":
        with _jobs_lock:
            job["notes"].append("No YouTube captions — transcribed audio instead.")

    # 2. moments (LLM, offline fallback)
    step("Finding the best moments…", 20)
    try:
        clips, scorer_usage = detect_moments_with_usage(transcript, mode="llm")
    except LLMError as e:
        with _jobs_lock:
            job["notes"].append(
                "AI scoring unavailable (" + friendly_error(e) + ") — used offline mode.")
        clips, scorer_usage = detect_moments_with_usage(transcript,
                                                        mode="offline")
    clips = clips[:max_clips]
    if not clips:
        raise RuntimeError("no good moments found in this video")

    # 3. metadata (best effort — never blocks clip building)
    step("Writing titles & hashtags…", 30)
    metas: dict[int, dict] = {}
    meta_usage: dict[int, dict] = {}
    try:
        items = generate_for_clips(
            [{"transcript": [c for c in transcript
                             if c["end"] > cl.start and c["start"] < cl.end]}
             for cl in clips],
            niche=niche)
        for i, item in enumerate(items):
            metas[i] = item["metadata"]
            if item.get("usage"):
                meta_usage[i] = item["usage"]
    except Exception as e:  # noqa: BLE001 — metadata is optional
        with _jobs_lock:
            job["notes"].append("AI titles unavailable: " + friendly_error(e))

    # 4. download + build each clip
    built = []
    for i, cl in enumerate(clips):
        pct = 35 + int(60 * i / len(clips))
        step(f"Building clip {i + 1} of {len(clips)}…", pct)
        style = resolve_style(style_choice)  # "random" -> per-clip pick
        paths = download_ranges(url, [(cl.start, cl.end)], pad=0.0)
        if not paths:
            raise RuntimeError(f"couldn't download clip {i + 1}")
        seg = paths[0]
        dur = cl.end - cl.start
        words = synth_words(transcript, cl.start, cl.end)
        out = str(clips_dir() / f"{jid}_{i}.mp4")
        build_short(seg,
                    {"start": 0.0, "end": dur,
                     "transcript": words, "caption_style": style},
                    out)
        entry = {
            "index": i,
            "analytics_id": f"{jid}_{i}",
            "start": round(cl.start, 2),
            "end": round(cl.end, 2),
            "score": cl.score,
            "hook": cl.hook_line,
            "reason": cl.reason,
            "style": style,
            "video": f"/clips/{jid}/{i}.mp4",
            "title": (metas.get(i) or {}).get("title") or cl.title,
            "description": (metas.get(i) or {}).get("description") or "",
            "hashtags": (metas.get(i) or {}).get("hashtags") or [],
            "approved": False,
            "skipped": bool(job["mode"] == "autopilot"
                            and cl.score < job["quality_gate"]),
        }
        # Feedback loop: record the clip for analytics (never breaks builds).
        try:
            _analytics().record_clip(
                clip_id=entry["analytics_id"],
                niche=job["niche_id"],
                caption_style=style,
                score=cl.score,
                source_url=url,
                title=entry["title"],
                usage=[meta_usage[i]] if meta_usage.get(i) else [],
                shared_usage=({**scorer_usage, "shared_among": len(clips)}
                               if scorer_usage else None),
            )
        except Exception:  # noqa: BLE001 — analytics must never break a build
            log.debug("analytics record failed", exc_info=True)
        built.append(entry)
        with _jobs_lock:
            job["clips"] = list(built)

    skipped = sum(1 for c in built if c["skipped"])
    if skipped:
        with _jobs_lock:
            job["notes"].append(
                f"{skipped} low-scoring clip(s) auto-skipped by the quality gate "
                f"(score < {job['quality_gate']}, autopilot mode).")

    # Autopilot: upload non-skipped clips automatically (quota-aware).
    if job["mode"] == "autopilot" and yt_oauth.is_connected():
        step("Uploading clips to YouTube…", 96)
        for i, entry in enumerate(built):
            if entry.get("skipped"):
                continue
            path = str(clips_dir() / f"{jid}_{i}.mp4")
            try:
                res = yt_upload_clip({**entry, "file": path}, mode="api")
            except Exception as e:  # noqa: BLE001 — stop, don't spam retries
                with _jobs_lock:
                    job["notes"].append(
                        "Auto-upload stopped: " + friendly_error(e))
                break
            if res.get("method") == "api":
                entry["youtube_id"] = res["video_id"]
                entry["youtube_url"] = res["url"]
                aid = entry.get("analytics_id")
                if aid:
                    try:
                        _analytics().mark_uploaded(aid, res["video_id"])
                    except Exception:  # noqa: BLE001 — never break a build
                        log.debug("mark_uploaded failed", exc_info=True)
            else:
                # Quota exhausted (or lost connection) -> Studio fallback.
                with _jobs_lock:
                    job["notes"].append(
                        "YouTube quota used up — remaining clips need a quick "
                        "manual Studio upload. " + str(res.get("note") or ""))
                break
        with _jobs_lock:
            job["clips"] = list(built)

    step("Done!", 100)
    _job_update(jid, status="done")
    vid = extract_video_id(url)
    if vid:
        try:
            SeenStore().update_clips(vid, len(built))
        except Exception:
            log.debug("seen-store update failed", exc_info=True)


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------

from flask import render_template  # noqa: E402  (kept with other flask imports)


@app.route("/")
def index():
    # No forced setup wizard: on the very first run we write sensible
    # defaults (every niche selected, karaoke captions, manual mode) and
    # go straight to the dashboard. Everything stays editable in Settings.
    if not cfg_mod.config_path().exists():
        cfg_mod.save_config(cfg_mod.default_config())
    cfg = cfg_mod.load_config()
    return render_template("dashboard.html", cfg=cfg)


# ---------------------------------------------------------------------------
# config / catalogue APIs
# ---------------------------------------------------------------------------

@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(cfg_mod.load_config())


@app.route("/api/llm", methods=["GET"])
def api_llm_status():
    """Dashboard "AI brain" status — never returns key values, only set-flags."""
    stored = llm_keys_mod.load_keys()
    active = {"provider": None, "model": None}
    try:
        p = llm_mod.get_provider()
        active = {"provider": p.name, "model": (p.models or [None])[0]}
    except llm_mod.LLMError:
        pass
    return jsonify({
        "ok": True,
        "gemini_set": bool(os.environ.get("GEMINI_API_KEY") or stored["gemini_key"]),
        "openai_set": bool(os.environ.get("OPENAI_API_KEY") or stored["openai_key"]),
        "provider": stored["provider"],
        "models": {
            "gemini": list(llm_mod.DEFAULT_GEMINI_MODELS),
            "openai": list(llm_mod.DEFAULT_OPENAI_MODELS),
        },
        "active": active,
    })


@app.route("/api/llm", methods=["POST"])
def api_llm_save():
    """Save LLM keys from the dashboard (stored 0o600 in ~/.clipforge).

    Blank fields PRESERVE the already-stored key — they never wipe it.
    To forget a key, use POST /api/llm/forget with {"which": "gemini"|"openai"}.
    """
    data = request.get_json(force=True) or {}
    try:
        stored = llm_keys_mod.load_keys()
        gemini_key = str(data.get("gemini_key", "")).strip()
        openai_key = str(data.get("openai_key", "")).strip()
        if not gemini_key:
            gemini_key = stored["gemini_key"]
        if not openai_key:
            openai_key = stored["openai_key"]
        llm_keys_mod.save_keys(
            gemini_key=gemini_key,
            openai_key=openai_key,
            provider=str(data.get("provider", "") or "").strip() or stored["provider"],
        )
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/llm/forget", methods=["POST"])
def api_llm_forget():
    """Forget one stored key ("gemini" or "openai"); the other is kept."""
    data = request.get_json(force=True) or {}
    try:
        llm_keys_mod.forget_key(str(data.get("which", "")))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/meta", methods=["GET"])
def api_meta():
    """App version + ffmpeg/ffprobe health for the dashboard status pill."""
    from core.paths import ffmpeg_status
    from core.version import __version__

    return jsonify({"ok": True, "version": __version__,
                    "ffmpeg": ffmpeg_status()})


@app.route("/api/settings", methods=["POST"])
def api_settings():
    """Change anything, anytime: merges the posted fields over the current
    config and re-validates. (Replaces the old one-time /api/setup.)"""
    data = request.get_json(force=True) or {}
    style = data.get("caption_style")
    if style is not None and style != "random" and style not in AVAILABLE_STYLES:
        return jsonify({"ok": False,
                        "error": f"Unknown caption style {style!r}."}), 400
    merged = cfg_mod.load_config()
    for k in cfg_mod.DEFAULTS:
        if k in data:
            merged[k] = data[k]
    try:
        path = cfg_mod.save_config(merged)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "path": str(path)})


@app.route("/api/niches", methods=["GET"])
def api_niches():
    return jsonify(niches_mod.list_niches())


@app.route("/api/styles", methods=["GET"])
def api_styles():
    styles = [{"id": s, "preview": f"/previews/style_preview_{s}.mp4"}
              for s in AVAILABLE_STYLES]
    styles.append({"id": "random", "preview": None})
    return jsonify(styles)


@app.route("/api/upload-status", methods=["GET"])
def api_upload_status():
    cfg = cfg_mod.load_config()
    connected = yt_oauth.is_connected()
    quota = yt_quota_status()
    if connected:
        note = (f"Connected ✅ · {quota['used']}/{quota['limit']} uploads "
                f"used today (resets {quota['resets']}).")
    elif yt_oauth.has_client_config():
        note = ("YouTube client ready — click “🔗 Connect YouTube” and "
                "approve in your browser.")
    else:
        note = ("YouTube isn't connected yet — one-time setup takes ~5 min: "
                "see docs/YOUTUBE_SETUP.md, then click “🔗 Connect YouTube”.")
    return jsonify({
        "connected": connected,
        "has_client": yt_oauth.has_client_config(),
        "mode": cfg.get("mode", "manual"),
        "quota": quota,
        "note": note,
    })


_connecting = False
_connect_lock = threading.Lock()


@app.route("/api/youtube/connect", methods=["POST"])
def api_youtube_connect():
    """Start the one-time OAuth consent flow (opens the user's browser).

    Runs in a background thread so the request returns immediately; the
    dashboard polls /api/upload-status until it shows connected.
    """
    global _connecting
    with _connect_lock:
        if _connecting:
            return jsonify({"ok": True,
                            "note": "Already connecting — check your browser."})
        if not yt_oauth.has_client_config():
            return jsonify({"ok": False,
                            "error": "No YouTube client configured yet — "
                                     "follow docs/YOUTUBE_SETUP.md first."}), 400
        _connecting = True

    def _do():
        global _connecting
        try:
            yt_oauth.get_credentials()
            log.info("YouTube connected")
        except Exception as e:  # noqa: BLE001 — surfaced via status poll
            log.error("YouTube connect failed: %s", e)
        finally:
            with _connect_lock:
                _connecting = False

    threading.Thread(target=_do, daemon=True).start()
    return jsonify({"ok": True,
                    "note": "A browser tab opened — approve ClipForge there; "
                            "this page will show Connected ✅."})


@app.route("/api/youtube/disconnect", methods=["POST"])
def api_youtube_disconnect():
    return jsonify({"ok": True,
                    "disconnected": yt_oauth.disconnect()})


@app.route("/api/youtube/client", methods=["POST"])
def api_youtube_client_upload():
    """Accept the Google Cloud OAuth client JSON as a file upload.

    Saves it to the app config dir (outside the repo) so the user never
    has to place the file by hand. Validates that it looks like a real
    OAuth client JSON before saving.
    """
    f = request.files.get("client_json")
    if f is None or not f.filename:
        return jsonify({"ok": False,
                        "error": "No file selected — choose the JSON you "
                                 "downloaded from Google Cloud Console."}), 400
    try:
        raw = f.read(2 * 1024 * 1024)  # 2 MB cap; client JSONs are ~1 KB
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return jsonify({"ok": False,
                        "error": "That file isn't valid JSON — re-download "
                                 "the OAuth client JSON from Google Cloud "
                                 "Console (APIs & Services → Credentials)."}), 400
    section = data.get("installed") or data.get("web") or {}
    if not isinstance(section, dict) or not section.get("client_id"):
        return jsonify({"ok": False,
                        "error": "That JSON doesn't look like a Google OAuth "
                                 "client file (no client_id found). Download "
                                 "it via Credentials → ⬇ on your OAuth client."}), 400
    if "installed" not in data:
        return jsonify({"ok": False,
                        "error": "That client is the “Web application” type — "
                                 "ClipForge needs a “Desktop app” OAuth client. "
                                 "In Google Cloud Console create a new OAuth client "
                                 "ID of type “Desktop app”, download its JSON, "
                                 "and upload that file instead."}), 400
    try:
        p = yt_oauth.client_file_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(raw)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    except OSError as e:
        return jsonify({"ok": False,
                        "error": f"Couldn't save the file: {e}"}), 500
    log.info("YouTube client JSON uploaded (%s)", f.filename)
    return jsonify({"ok": True,
                    "note": "Client saved ✅ — now click “🔗 Connect YouTube” "
                            "and approve in your browser."})


@app.route("/previews/<path:filename>")
def previews(filename):
    return send_from_directory(str(PREVIEWS_DIR), filename)


# ---------------------------------------------------------------------------
# analytics API (Phase 8 — performance feedback loop)
# ---------------------------------------------------------------------------

def _clip_id_ok(clip_id: str) -> bool:
    return bool(clip_id) and all(c.isalnum() or c == "_" for c in clip_id)


@app.route("/api/analytics", methods=["GET"])
def api_analytics():
    records = _analytics().all_records()
    return jsonify({
        "ok": True,
        "insights": build_insights(records),
        "suggestions": tuning_suggestions(records),
    })


@app.route("/api/analytics/stats", methods=["POST"])
def api_analytics_stats():
    data = request.get_json(force=True) or {}
    clip_id = str(data.get("clip_id", ""))
    if not _clip_id_ok(clip_id):
        return jsonify({"ok": False, "error": "bad clip id"}), 400

    def _num(v):
        if v is None or v == "":
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ValueError("views/likes/comments must be whole numbers")
        if n < 0:
            raise ValueError("views/likes/comments can't be negative")
        return n

    try:
        views, likes, comments = (_num(data.get("views")),
                                  _num(data.get("likes")),
                                  _num(data.get("comments")))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    try:
        ok = _analytics().log_stats(clip_id, views=views,
                                    likes=likes, comments=comments)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if not ok:
        return jsonify({"ok": False, "error": "Clip not found."}), 404
    return jsonify({"ok": True})


@app.route("/api/analytics/uploaded", methods=["POST"])
def api_analytics_uploaded():
    """Phase 6 will call this when a clip goes live on YouTube."""
    data = request.get_json(force=True) or {}
    clip_id = str(data.get("clip_id", ""))
    yt_id = str(data.get("youtube_video_id", "")).strip()
    if not _clip_id_ok(clip_id) or not yt_id:
        return jsonify({"ok": False,
                        "error": "clip_id and youtube_video_id required"}), 400
    if not _analytics().mark_uploaded(clip_id, yt_id):
        return jsonify({"ok": False, "error": "Clip not found."}), 404
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# source discovery API (Phase 7)
# ---------------------------------------------------------------------------

_disc_runs: dict[str, dict] = {}
_disc_lock = threading.Lock()


def _run_discovery(run_id: str):
    """Background: find fresh source videos for the user's niches."""
    cfg = cfg_mod.load_config()
    try:
        grouped = discover_for_autopilot(cfg, per_niche=3)
        with _disc_lock:
            _disc_runs[run_id]["status"] = "done"
            _disc_runs[run_id]["candidates"] = grouped
    except Exception as exc:  # never leave the UI hanging
        log.exception("discovery run %s failed", run_id)
        with _disc_lock:
            _disc_runs[run_id]["status"] = "error"
            _disc_runs[run_id]["error"] = friendly_error(exc)


@app.route("/api/discover", methods=["POST"])
def api_discover_start():
    cfg = cfg_mod.load_config()
    if not cfg.get("niches"):
        return jsonify({"ok": False,
                        "error": "Pick at least one niche in Settings below."}), 400
    run_id = uuid.uuid4().hex[:12]
    with _disc_lock:
        _disc_runs[run_id] = {"status": "running", "candidates": {},
                              "started": time.time()}
    t = threading.Thread(target=_run_discovery, args=(run_id,), daemon=True)
    t.start()
    return jsonify({"ok": True, "run_id": run_id})


@app.route("/api/discover/<run_id>", methods=["GET"])
def api_discover_get(run_id):
    if not run_id.isalnum():
        return jsonify({"ok": False, "error": "bad run id"}), 400
    with _disc_lock:
        run = dict(_disc_runs.get(run_id) or {})
    if not run:
        return jsonify({"ok": False, "error": "unknown discovery run"}), 404
    return jsonify({"ok": True, "status": run["status"],
                    "candidates": run.get("candidates", {}),
                    "error": run.get("error")})


@app.route("/clips/<job_id>/<int:idx>.mp4")
def serve_clip(job_id, idx):
    if not job_id.isalnum():
        return jsonify({"ok": False, "error": "bad job id"}), 400
    return send_from_directory(str(clips_dir()), f"{job_id}_{idx}.mp4",
                               mimetype="video/mp4")


# ---------------------------------------------------------------------------
# jobs API
# ---------------------------------------------------------------------------

@app.route("/api/jobs", methods=["GET"])
def api_jobs_list():
    with _jobs_lock:
        jobs = [ {k: v for k, v in j.items() if k != "clips"}
                 | {"clip_count": len(j["clips"])} for j in _jobs.values() ]
    jobs.sort(key=lambda j: j["created"], reverse=True)
    return jsonify(jobs)


@app.route("/api/jobs", methods=["POST"])
def api_jobs_create():
    data = request.get_json(force=True) or {}
    url = str(data.get("url", "")).strip()
    if not url or ("youtube.com" not in url and "youtu.be" not in url):
        return jsonify({"ok": False,
                        "error": "Paste a YouTube video link first."}), 400
    cfg = cfg_mod.load_config()
    niche_id = str(data.get("niche_id") or (cfg.get("niches") or ["custom"])[0])
    if niche_id not in niches_mod.niche_ids():
        return jsonify({"ok": False, "error": "Unknown niche."}), 400
    custom_niche = str(data.get("custom_niche") or cfg.get("custom_niche") or "").strip()
    if niche_id == "custom" and not custom_niche:
        return jsonify({"ok": False,
                        "error": "You picked Custom — type your niche name."}), 400
    style = str(data.get("caption_style") or cfg.get("caption_style") or "karaoke")
    if style != "random" and style not in AVAILABLE_STYLES:
        return jsonify({"ok": False, "error": f"Unknown caption style {style!r}."}), 400
    try:
        max_clips = max(1, min(10, int(data.get("max_clips", 3))))
    except (TypeError, ValueError):
        max_clips = 3
    job = _new_job(url, niche_id, custom_niche, style, max_clips, cfg)
    # Record in the seen-store so discovery never suggests this video again.
    vid = extract_video_id(url)
    if vid:
        try:
            SeenStore().mark_used(vid, niche=niche_id, clips_made=0)
        except Exception:  # seen-store must never break job creation
            log.debug("seen-store mark failed", exc_info=True)
    return jsonify({"ok": True, "job_id": job["id"]})


@app.route("/api/jobs/<job_id>", methods=["GET"])
def api_job_get(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Job not found."}), 404
    return jsonify(job)


@app.route("/api/jobs/<job_id>/clips/<int:idx>/approve", methods=["POST"])
def api_clip_approve(job_id, idx):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or idx >= len(job["clips"]):
            return jsonify({"ok": False, "error": "Clip not found."}), 404
        job["clips"][idx]["approved"] = True
    _persist_job(job_id)
    return jsonify({"ok": True})


@app.route("/api/jobs/<job_id>/clips/<int:idx>/upload", methods=["POST"])
def api_clip_upload(job_id, idx):
    """Semi-auto: upload one clip on click (background thread)."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or idx >= len(job["clips"]):
            return jsonify({"ok": False, "error": "Clip not found."}), 404
        clip = job["clips"][idx]
        if clip.get("uploading"):
            return jsonify({"ok": True,
                            "note": "Upload already in progress."})
        if clip.get("youtube_id"):
            return jsonify({"ok": True, "note": "Already uploaded."})
        if clip.get("skipped"):
            return jsonify({"ok": False,
                            "error": "This clip was skipped by the quality gate."}), 400
        clip["uploading"] = True
        clip.pop("upload_error", None)
        clip.pop("upload_note", None)
    t = threading.Thread(target=_upload_clip_bg, args=(job_id, idx),
                         daemon=True)
    t.start()
    return jsonify({"ok": True, "started": True})


def _upload_clip_bg(job_id: str, idx: int):
    """Upload one clip via the API (quota-aware), record the video id."""
    try:
        with _jobs_lock:
            clip = dict(_jobs[job_id]["clips"][idx])
        path = str(clips_dir() / f"{job_id}_{idx}.mp4")
        res = yt_upload_clip({**clip, "file": path}, mode="api")
        with _jobs_lock:
            c = _jobs[job_id]["clips"][idx]
            c["uploading"] = False
            if res.get("method") == "api":
                c["youtube_id"] = res["video_id"]
                c["youtube_url"] = res["url"]
            else:  # quota exhausted / not connected -> Studio fallback
                c["upload_note"] = res.get("note")
                c["manual_steps"] = (res.get("manual") or {}).get("steps")
        if res.get("method") == "api":
            aid = clip.get("analytics_id")
            if aid:
                try:
                    _analytics().mark_uploaded(aid, res["video_id"])
                except Exception:  # noqa: BLE001 — never break upload
                    log.debug("mark_uploaded failed", exc_info=True)
    except Exception as exc:  # noqa: BLE001
        log.error("clip upload failed: %s", traceback.format_exc())
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job and idx < len(job["clips"]):
                c = job["clips"][idx]
                c["uploading"] = False
                c["upload_error"] = friendly_error(exc)
    finally:
        _persist_job(job_id)


@app.route("/api/jobs/<job_id>/clips/<int:idx>/restyle", methods=["POST"])
def api_clip_restyle(job_id, idx):
    data = request.get_json(force=True) or {}
    style = str(data.get("style", ""))
    if style not in AVAILABLE_STYLES:
        return jsonify({"ok": False, "error": f"Unknown caption style {style!r}."}), 400
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or idx >= len(job["clips"]):
            return jsonify({"ok": False, "error": "Clip not found."}), 404
        if job["status"] == "running":
            return jsonify({"ok": False,
                            "error": "Wait for this job to finish first."}), 409
        clip = job["clips"][idx]
    t = threading.Thread(target=_restyle_clip,
                         args=(job_id, idx, style), daemon=True)
    with _jobs_lock:
        job["restyling"] = idx
    t.start()
    return jsonify({"ok": True, "style": style})


def _restyle_clip(job_id: str, idx: int, style: str):
    """Re-render one clip with a different caption style (background)."""
    try:
        with _jobs_lock:
            job = _jobs[job_id]
            clip = job["clips"][idx]
        url, start, end = job["url"], clip["start"], clip["end"]
        paths = download_ranges(url, [(start, end)], pad=0.0)  # cache hit
        if not paths:
            raise RuntimeError("couldn't re-download the clip segment")
        res = ingest(url)  # transcript again (cheap: captions cached in code? no — refetch)
        transcript = res.get("transcript") or []
        words = synth_words(transcript, start, end)
        out = str(clips_dir() / f"{job_id}_{idx}.mp4")
        build_short(paths[0],
                    {"start": 0.0, "end": end - start,
                     "transcript": words, "caption_style": style},
                    out)
        with _jobs_lock:
            job["clips"][idx]["style"] = style
            job.pop("restyling", None)
        aid = clip.get("analytics_id")
        if aid:
            try:
                _analytics().update_clip(aid, caption_style=style)
            except Exception:  # noqa: BLE001 — never break restyle
                log.debug("analytics restyle update failed", exc_info=True)
    except Exception as exc:  # noqa: BLE001
        log.error("restyle failed: %s", traceback.format_exc())
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job:
                job.pop("restyling", None)
                job.setdefault("notes", []).append(
                    f"Re-style to {style} failed: {friendly_error(exc)}")
    finally:
        _persist_job(job_id)


# ---------------------------------------------------------------------------
# launch
# ---------------------------------------------------------------------------

def main():
    import argparse
    p = argparse.ArgumentParser(description="ClipForge dashboard")
    p.add_argument("--port", type=int, default=5057)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if is_frozen():
        # No console window in the packaged app — crashes would be invisible.
        # Mirror logs to a file in the user's private app folder.
        try:
            from core.config import config_dir
            log_dir = config_dir()
            log_dir.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(str(log_dir / "clipforge.log"),
                                     encoding="utf-8")
            fh.setFormatter(logging.Formatter(
                "%(asctime)s %(name)s %(levelname)s %(message)s"))
            logging.getLogger().addHandler(fh)
            log.info("frozen build — logging to %s", log_dir / "clipforge.log")
        except OSError as e:
            log.warning("couldn't set up file logging: %s", e)
    url = f"http://127.0.0.1:{args.port}"
    if not args.no_browser and not os.environ.get("CF_NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"\n  ClipForge is running → {url}\n  Press Ctrl+C to stop.\n")
    app.run(host="127.0.0.1", port=args.port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
