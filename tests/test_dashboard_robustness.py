"""Dashboard/API robustness regression tests (bug-hunter subsystem).

Covers the failure modes found in app.py + templates/dashboard.html:
persist truncation/atomicity, job state-machine holes, malformed input
handling, and JS<->API contract mismatches. Flask test client only;
no network calls (background workers are stubbed out).
"""

import json

import pytest

import app as app_mod
from core import config as cfg_mod


@pytest.fixture()
def cfg_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CF_CONFIG_DIR", str(tmp_path / ".clipforge"))
    return tmp_path / ".clipforge"


@pytest.fixture()
def client(cfg_home):
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


@pytest.fixture()
def clean_jobs():
    """Isolate the global job registry per test (module-global in app)."""
    with app_mod._jobs_lock:
        saved = dict(app_mod._jobs)
        app_mod._jobs.clear()
    yield
    with app_mod._jobs_lock:
        app_mod._jobs.clear()
        app_mod._jobs.update(saved)


def _seed_job(job_id="job1", **over):
    job = {
        "id": job_id,
        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "niche_id": "ai-news",
        "custom_niche": "",
        "caption_style": "karaoke",
        "max_clips": 3,
        "mode": "manual",
        "quality_gate": 50,
        "status": "done",
        "step": "Done!",
        "progress": 100,
        "clips": [{
            "index": 0, "start": 1.0, "end": 30.0,
            "title": "Clip title", "description": "desc",
            "hashtags": ["#ai"], "score": 80,
        }],
        "error": None,
        "notes": [],
        "created": 1234567890.0,
    }
    job.update(over)
    with app_mod._jobs_lock:
        app_mod._jobs[job_id] = job
    return job


# ---------------------------------------------------------------- persist

def test_persist_huge_job_stays_valid_json(client, clean_jobs):
    """P0: the old [:200_000] slice wrote invalid JSON, so the job silently
    vanished from the dashboard on the next restart. _persist_job must
    always write parseable JSON."""
    huge = "x" * 300_000
    _seed_job("big1", clips=[{"index": 0, "title": "t",
                              "description": huge, "video": "/clips/big1/0.mp4"}])
    app_mod._persist_job("big1")
    raw = (app_mod.jobs_dir() / "big1.json").read_text(encoding="utf-8")
    assert len(raw) <= 200_000
    loaded = json.loads(raw)  # must not raise
    assert loaded["id"] == "big1"
    assert loaded["clips"][0]["title"] == "t"


def test_persist_leaves_no_tmp_file(client, clean_jobs):
    """Atomic write: no .tmp leftovers, target is complete JSON."""
    _seed_job("atomic1")
    app_mod._persist_job("atomic1")
    jd = app_mod.jobs_dir()
    assert not list(jd.glob("*.tmp"))
    assert json.loads((jd / "atomic1.json").read_text())["id"] == "atomic1"


def test_persist_then_load_roundtrip_for_huge_job(client, clean_jobs):
    """A huge job persisted then reloaded via _load_jobs still shows up."""
    huge = "y" * 250_000
    _seed_job("big2", clips=[{"index": 0, "title": "t",
                              "description": huge, "video": "/clips/big2/0.mp4"}])
    app_mod._persist_job("big2")
    with app_mod._jobs_lock:
        app_mod._jobs.pop("big2")
    app_mod._load_jobs()
    with app_mod._jobs_lock:
        assert app_mod._jobs["big2"]["status"] == "done"


def test_load_jobs_skips_corrupt_file(client, clean_jobs):
    jd = app_mod.jobs_dir()
    (jd / "corrupt.json").write_text("{not json", encoding="utf-8")
    app_mod._load_jobs()  # must not raise
    with app_mod._jobs_lock:
        assert "corrupt" not in app_mod._jobs


def test_slim_job_text_always_valid_json(clean_jobs):
    """_slim_job_text never returns truncated/invalid JSON, even for
    pathological inputs."""
    job = {"id": "path", "error": "e" * 500_000,
           "clips": [{"index": i, "description": "d" * 100_000,
                      "title": "t" * 10_000} for i in range(20)]}
    text = app_mod._slim_job_text(job)
    loaded = json.loads(text)  # must not raise
    assert loaded["id"] == "path"
    assert isinstance(loaded["clips"], list)


# ---------------------------------------------------------------- job list vs legacy shapes

def test_api_jobs_list_survives_legacy_shape(client, clean_jobs):
    """P0: a persisted job missing clips/created (accepted by _load_jobs)
    used to 500 the whole /api/jobs list via KeyError."""
    with app_mod._jobs_lock:
        app_mod._jobs["legacy"] = {"id": "legacy", "status": "done"}
    r = client.get("/api/jobs")
    assert r.status_code == 200
    entry = next(j for j in r.get_json() if j["id"] == "legacy")
    assert entry["clip_count"] == 0


def test_load_jobs_normalizes_legacy_shape(client, clean_jobs):
    jd = app_mod.jobs_dir()
    (jd / "legacy2.json").write_text(
        json.dumps({"id": "legacy2", "status": "done"}), encoding="utf-8")
    app_mod._load_jobs()
    r = client.get("/api/jobs")
    assert r.status_code == 200
    entry = next(j for j in r.get_json() if j["id"] == "legacy2")
    assert entry["clip_count"] == 0


def test_api_job_get_missing_returns_404(client, clean_jobs):
    r = client.get("/api/jobs/doesnotexist")
    assert r.status_code == 404
    assert r.get_json()["ok"] is False


# ---------------------------------------------------------------- state machine holes

def test_run_job_with_missing_id_does_not_raise(clean_jobs):
    """_run_job used to KeyError on an unknown id, killing the thread
    without ever marking the job failed."""
    app_mod._run_job("no-such-job")  # must not raise


def test_restyle_double_start_rejected(client, clean_jobs, monkeypatch):
    """Two rapid Restyle clicks must not spawn two worker threads."""
    monkeypatch.setattr(app_mod, "_restyle_clip", lambda *a: None)
    _seed_job("rs1")
    r = client.post("/api/jobs/rs1/clips/0/restyle",
                    json={"caption_style": "karaoke"})
    assert r.status_code == 200
    r = client.post("/api/jobs/rs1/clips/0/restyle",
                    json={"caption_style": "karaoke"})
    assert r.status_code == 409
    assert r.get_json()["ok"] is False


def test_restyle_while_running_rejected(client, clean_jobs):
    _seed_job("rs2", status="running")
    r = client.post("/api/jobs/rs2/clips/0/restyle",
                    json={"caption_style": "karaoke"})
    assert r.status_code == 409


# ---------------------------------------------------------------- restyle key contract (P1)

def test_restyle_accepts_caption_style_key(client, clean_jobs, monkeypatch):
    """The dashboard posts {"caption_style": ...}; the server used to read
    only data["style"], so every Restyle click 400'd."""
    monkeypatch.setattr(app_mod, "_restyle_clip", lambda *a: None)
    _seed_job("rs3")
    r = client.post("/api/jobs/rs3/clips/0/restyle",
                    json={"caption_style": "hormozi"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["style"] == "hormozi"


def test_restyle_still_accepts_style_key(client, clean_jobs, monkeypatch):
    monkeypatch.setattr(app_mod, "_restyle_clip", lambda *a: None)
    _seed_job("rs4")
    r = client.post("/api/jobs/rs4/clips/0/restyle",
                    json={"style": "neon"})
    assert r.status_code == 200


def test_restyle_rejects_unknown_style(client, clean_jobs):
    _seed_job("rs5")
    r = client.post("/api/jobs/rs5/clips/0/restyle",
                    json={"caption_style": "comic-sans"})
    assert r.status_code == 400


# ---------------------------------------------------------------- malformed input -> 4xx not 500

def test_metadata_rejects_non_list_hashtags(client, clean_jobs):
    """P1: {"hashtags": 5} used to 500 (TypeError iterating an int)."""
    _seed_job("meta1")
    r = client.post("/api/jobs/meta1/clips/0/metadata",
                    json={"hashtags": 5})
    assert r.status_code == 400
    assert r.get_json()["ok"] is False


def test_metadata_accepts_list_hashtags(client, clean_jobs):
    _seed_job("meta2")
    r = client.post("/api/jobs/meta2/clips/0/metadata",
                    json={"hashtags": ["ai", "#news"]})
    assert r.status_code == 200
    assert r.get_json()["clip"]["hashtags"] == ["#ai", "#news"]


def test_metadata_missing_clip_returns_404(client, clean_jobs):
    _seed_job("meta3")
    r = client.post("/api/jobs/meta3/clips/9/metadata", json={"title": "x"})
    assert r.status_code == 404


def test_discover_rejects_unhashable_niche(client):
    """P1: {"niches": [{}]} used to 500 (TypeError on set membership)."""
    r = client.post("/api/discover", json={"niches": [{}]})
    assert r.status_code == 400
    assert r.get_json()["ok"] is False


def test_discover_rejects_mixed_niche_types(client):
    r = client.post("/api/discover", json={"niches": ["ai-news", 42]})
    assert r.status_code == 400


def test_jobs_create_clamps_max_clips(client, clean_jobs, monkeypatch):
    """Huge/negative max_clips must be clamped, and job creation must not
    hit the network in tests (worker stubbed)."""
    seen = {}

    def fake_run(job_id):
        seen["id"] = job_id

    monkeypatch.setattr(app_mod, "_run_job", fake_run)
    base = {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "niche_id": "ai-news", "caption_style": "karaoke"}
    r = client.post("/api/jobs", json={**base, "max_clips": 999})
    assert r.status_code == 200
    with app_mod._jobs_lock:
        assert app_mod._jobs[seen["id"]]["max_clips"] == 10
    r = client.post("/api/jobs", json={**base, "max_clips": -5})
    assert r.status_code == 200
    with app_mod._jobs_lock:
        assert app_mod._jobs[seen["id"]]["max_clips"] == 1


def test_jobs_create_bad_max_clips_falls_back(client, clean_jobs, monkeypatch):
    monkeypatch.setattr(app_mod, "_run_job", lambda job_id: None)
    r = client.post("/api/jobs", json={
        "url": "https://youtu.be/dQw4w9WgXcQ",
        "niche_id": "ai-news", "max_clips": "not-a-number"})
    assert r.status_code == 200
    job_id = r.get_json()["job_id"]
    with app_mod._jobs_lock:
        assert app_mod._jobs[job_id]["max_clips"] == 3


def test_approve_missing_job_returns_404(client, clean_jobs):
    r = client.post("/api/jobs/nope/clips/0/approve")
    assert r.status_code == 404


def test_upload_missing_job_returns_404(client, clean_jobs):
    r = client.post("/api/jobs/nope/clips/0/upload")
    assert r.status_code == 404


# ---------------------------------------------------------------- template <-> API contracts

def _template():
    return (app_mod.BASE_DIR / "templates" / "dashboard.html").read_text(
        encoding="utf-8")


def test_template_reads_discovery_candidates_key():
    """P1: pollDiscovery + autopilot discovery read r.results, but
    GET /api/discover/<id> returns {"candidates": ...} — discovery always
    reported 'No fresh videos found'. The template must use r.candidates."""
    html = _template()
    assert "r.candidates" in html
    assert "r.results" not in html


def test_template_retry_uses_niche_id():
    """P1: jobs carry niche_id (not niche); retryJob posted j.niche
    (undefined) -> niche_id='custom' -> 400 for every non-custom job."""
    html = _template()
    assert "j.niche ||" not in html
    assert 'niche_id: j.niche_id || "custom"' in html
    assert "nicheName(j.niche_id)" in html
    assert "nicheName(j.niche)" not in html


def test_template_escapes_dynamic_strings():
    """Spot-check that the esc() helper exists and is applied to
    user/API-derived strings rendered via innerHTML."""
    html = _template()
    assert "function esc(s)" in html
    for snippet in ("esc(jobTitle(j))", "esc(c.title", "esc(j.error)",
                    "esc(c.upload_error)", "esc(y.channel", "esc(n.title)"):
        assert snippet in html, f"missing escape for {snippet}"


def test_llm_save_null_key_preserves_stored_key(client, cfg_home, monkeypatch):
    """JSON null must behave like blank (preserve), not become the string "None"."""
    import core.moments.llm_keys as llm_keys_mod
    monkeypatch.setattr(llm_keys_mod, "KEYS_FILE", cfg_home / "llm_keys.json",
                        raising=False)
    llm_keys_mod.save_keys(gemini_key="REAL-KEY-123", openai_key="")
    r = client.post("/api/llm", json={"gemini_key": None, "openai_key": None})
    assert r.status_code == 200
    assert llm_keys_mod.load_keys()["gemini_key"] == "REAL-KEY-123"


def test_bg_upload_thread_is_non_interactive(client, cfg_home, clean_jobs, monkeypatch):
    """_upload_clip_bg must pass interactive=False: a background thread must
    never open a blocking browser consent flow on expired/revoked tokens."""
    import app as app_mod
    calls = {}

    def fake_upload(clip, mode="api", interactive=True):
        calls["interactive"] = interactive
        return {"ok": True, "method": "api", "video_id": "vid1",
                "url": "https://youtu.be/vid1"}

    monkeypatch.setattr(app_mod, "yt_upload_clip", fake_upload)
    job_id = "bg1"
    with app_mod._jobs_lock:
        app_mod._jobs[job_id] = {"id": job_id, "clips": [{"title": "t"}]}
    app_mod._upload_clip_bg(job_id, 0)
    assert calls.get("interactive") is False
    with app_mod._jobs_lock:
        assert app_mod._jobs[job_id]["clips"][0]["youtube_id"] == "vid1"
