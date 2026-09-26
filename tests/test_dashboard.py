"""Phase 5 tests: config, niches, wizard validation, dashboard routes.

No browser automation — Flask test client only. No network calls.
"""

import io
import json
import os

import pytest

from core import config as cfg_mod
from core import niches as niches_mod
from core.edit import AVAILABLE_STYLES


@pytest.fixture()
def cfg_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CF_CONFIG_DIR", str(tmp_path / ".clipforge"))
    return tmp_path / ".clipforge"


def valid_payload():
    return {
        "niches": ["ai-news", "gaming-news"],
        "custom_niche": "",
        "caption_style": "karaoke",
        "mode": "semi-auto",
        "daily_count": 2,
        "times": ["09:00", "18:00"],
        "quality_gate": 50,
        "autopilot": False,
    }


# ---------------------------------------------------------------- niches

def test_niche_list_integrity():
    niches = niches_mod.list_niches()
    assert len(niches) == 41, f"expected 40 + custom = 41, got {len(niches)}"
    ids = [n["id"] for n in niches]
    assert len(set(ids)) == 41, "niche ids must be unique"
    assert niches[-1]["id"] == "custom", "custom must be last"
    real = [n for n in niches if n["id"] != "custom"]
    assert len(real) == 40
    for n in niches:
        assert n["name"] and n["category"]


def test_niche_name_lookup():
    assert niches_mod.niche_name("ai-news") == "AI news"
    with pytest.raises(ValueError):
        niches_mod.niche_name("nope")


# ---------------------------------------------------------------- config

def test_config_save_load_roundtrip(cfg_home):
    path = cfg_mod.save_config(valid_payload())
    assert path.exists()
    loaded = cfg_mod.load_config()
    for k, v in valid_payload().items():
        assert loaded[k] == v
    assert loaded["setup_done"] is True


def test_config_never_stores_secrets(cfg_home):
    payload = valid_payload()
    payload["GEMINI_API_KEY"] = "sk-should-never-be-saved"
    path = cfg_mod.save_config(payload)
    raw = path.read_text()
    assert "sk-should-never-be-saved" not in raw
    assert "GEMINI_API_KEY" not in raw


def test_config_validation_problems(cfg_home):
    bad = valid_payload(); bad["niches"] = []
    with pytest.raises(ValueError, match="niche"):
        cfg_mod.save_config(bad)

    bad = valid_payload(); bad["niches"] = ["bogus"]
    with pytest.raises(ValueError, match="[Uu]nknown niche"):
        cfg_mod.save_config(bad)

    bad = valid_payload(); bad["niches"] = ["custom"]; bad["custom_niche"] = ""
    with pytest.raises(ValueError, match="[Cc]ustom"):
        cfg_mod.save_config(bad)

    bad = valid_payload(); bad["daily_count"] = 0
    with pytest.raises(ValueError):
        cfg_mod.save_config(bad)

    bad = valid_payload(); bad["times"] = ["09:00"]  # count=2 needs 2 times
    with pytest.raises(ValueError, match="[Tt]ime"):
        cfg_mod.save_config(bad)

    bad = valid_payload(); bad["times"] = ["25:99", "18:00"]
    with pytest.raises(ValueError, match="[Tt]ime"):
        cfg_mod.save_config(bad)

    bad = valid_payload(); bad["mode"] = "turbo"
    with pytest.raises(ValueError, match="[Mm]ode"):
        cfg_mod.save_config(bad)


def test_config_load_missing_file_returns_defaults(cfg_home):
    cfg = cfg_mod.load_config()
    assert cfg["setup_done"] is False
    assert cfg["daily_count"] == 2


def test_config_load_corrupt_file_returns_defaults(cfg_home):
    cfg_mod.config_dir().mkdir(parents=True, exist_ok=True)
    cfg_mod.config_path().write_text("{not json", encoding="utf-8")
    assert cfg_mod.load_config()["setup_done"] is False


# ---------------------------------------------------------------- routes

@pytest.fixture()
def client(cfg_home):
    import app as app_mod
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def test_index_first_run_goes_straight_to_dashboard(client):
    # No forced wizard: first visit writes defaults and shows the dashboard.
    r = client.get("/")
    assert r.status_code == 200
    assert b"Make clips from a video" in r.data
    cfg = cfg_mod.load_config()
    assert cfg["setup_done"] is True
    assert len(cfg["niches"]) == 40  # every niche pre-selected, custom excluded


def test_settings_change_anything_anytime(client):
    r = client.get("/")  # first run writes defaults
    assert r.status_code == 200
    # partial update: only caption_style
    r = client.post("/api/settings", json={"caption_style": "hormozi"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    cfg = cfg_mod.load_config()
    assert cfg["caption_style"] == "hormozi"
    assert len(cfg["niches"]) == 40  # untouched fields preserved
    # niche list can be trimmed anytime
    r = client.post("/api/settings", json={"niches": ["ai-news"]})
    assert r.status_code == 200
    assert cfg_mod.load_config()["niches"] == ["ai-news"]


def test_settings_rejects_bad_style(client):
    r = client.post("/api/settings", json={"caption_style": "comic-sans"})
    assert r.status_code == 400
    assert r.get_json()["ok"] is False


def test_settings_rejects_invalid_payload(client):
    r = client.post("/api/settings", json={"niches": []})
    assert r.status_code == 400


def test_api_niches_count(client):
    r = client.get("/api/niches")
    assert r.status_code == 200
    assert len(r.get_json()) == 41


def test_api_styles_match_available_styles(client):
    r = client.get("/api/styles")
    assert r.status_code == 200
    got = r.get_json()
    ids = [s["id"] for s in got]
    assert ids == list(AVAILABLE_STYLES) + ["random"]
    for s in got:
        if s["id"] != "random":
            assert s["preview"] == f"/previews/style_preview_{s['id']}.mp4"


def test_api_upload_status_not_connected(client):
    r = client.get("/api/upload-status")
    j = r.get_json()
    assert j["connected"] is False
    assert j["has_client"] is False
    assert j["quota"]["used"] == 0 and j["quota"]["limit"] == 100
    assert "YOUTUBE_SETUP" in j["note"]


def test_jobs_reject_bad_url(client):
    r = client.post("/api/jobs", json={"url": "not a url"})
    assert r.status_code == 400
    r = client.post("/api/jobs", json={"url": ""})
    assert r.status_code == 400


def test_jobs_list_empty_initially(client):
    r = client.get("/api/jobs")
    assert r.status_code == 200
    assert r.get_json() == []


# ---------------------------------------------------------------- quality gate presets

def test_normalize_quality_gate_presets():
    assert cfg_mod.normalize_quality_gate("low") == 30
    assert cfg_mod.normalize_quality_gate("medium") == 50
    assert cfg_mod.normalize_quality_gate("HIGH") == 70
    assert cfg_mod.normalize_quality_gate("  Medium ") == 50


def test_normalize_quality_gate_numbers_and_invalid():
    assert cfg_mod.normalize_quality_gate(80) == 80
    assert cfg_mod.normalize_quality_gate("42") == 42
    assert cfg_mod.normalize_quality_gate("bogus") is None
    assert cfg_mod.normalize_quality_gate(None) is None


def test_save_config_accepts_gate_preset_string(cfg_home):
    payload = valid_payload()
    payload["quality_gate"] = "high"
    cfg_mod.save_config(payload)
    assert cfg_mod.load_config()["quality_gate"] == 70


def test_settings_api_accepts_gate_preset_string(client):
    r = client.post("/api/settings", json={"quality_gate": "low"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    assert cfg_mod.load_config()["quality_gate"] == 30


# ---------------------------------------------------------------- youtube client JSON upload

def _client_json_bytes():
    return json.dumps({
        "installed": {
            "client_id": "abc123.apps.googleusercontent.com",
            "client_secret": "shhh",
            "redirect_uris": ["http://localhost"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }).encode("utf-8")


def test_youtube_client_upload_saves_file(client, cfg_home):
    from core.upload import oauth as oauth_mod
    data = {"client_json": (io.BytesIO(_client_json_bytes()), "client.json")}
    r = client.post("/api/youtube/client", data=data,
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["ok"] is True
    p = oauth_mod.client_file_path()
    assert p.is_file()
    assert json.loads(p.read_text())["installed"]["client_id"].startswith("abc123")
    assert oauth_mod.has_client_config() is True
    # saved outside the repo, with locked-down perms
    assert ".clipforge" in str(p)


def test_youtube_client_upload_rejects_bad_json(client):
    data = {"client_json": (io.BytesIO(b"{nope"), "x.json")}
    r = client.post("/api/youtube/client", data=data,
                    content_type="multipart/form-data")
    assert r.status_code == 400
    assert r.get_json()["ok"] is False


def test_youtube_client_upload_rejects_non_client_json(client):
    data = {"client_json": (io.BytesIO(b'{"foo": 1}'), "x.json")}
    r = client.post("/api/youtube/client", data=data,
                    content_type="multipart/form-data")
    assert r.status_code == 400
    assert "client_id" in r.get_json()["error"]


def test_youtube_client_upload_requires_file(client):
    r = client.post("/api/youtube/client", data={},
                    content_type="multipart/form-data")
    assert r.status_code == 400
    assert r.get_json()["ok"] is False


# ---------------------------------------------------------------- frozen-aware paths

def test_paths_not_frozen_from_source():
    from core import paths as p
    assert p.is_frozen() is False
    assert p.resource_path().is_dir()
    assert p.resource_path("templates").is_dir()


def test_ffmpeg_path_env_override(monkeypatch):
    from core import paths as p
    monkeypatch.setenv("CLIPFORGE_FFMPEG", "/custom/ffmpeg")
    monkeypatch.setenv("CLIPFORGE_FFPROBE", "/custom/ffprobe")
    assert p.ffmpeg_path() == "/custom/ffmpeg"
    assert p.ffprobe_path() == "/custom/ffprobe"


def test_ffmpeg_path_falls_back_to_which(monkeypatch):
    from core import paths as p
    monkeypatch.delenv("CLIPFORGE_FFMPEG", raising=False)
    monkeypatch.delenv("CLIPFORGE_FFPROBE", raising=False)
    # system ffmpeg exists in this dev environment
    assert p.ffmpeg_path() is not None
    assert p.ffprobe_path() is not None


# ------------------------------------------------------- simulated frozen app

def _fake_meipass(tmp_path, monkeypatch):
    """Simulate a PyInstaller bundle: sys.frozen + _MEIPASS with binaries."""
    mp = tmp_path / "bundle"
    mp.mkdir()
    (mp / "ffmpeg").write_text("#!/bin/sh\n")
    (mp / "ffprobe").write_text("#!/bin/sh\n")
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys._MEIPASS", str(mp), raising=False)
    monkeypatch.delenv("CLIPFORGE_FFMPEG", raising=False)
    monkeypatch.delenv("CLIPFORGE_FFPROBE", raising=False)
    return mp


def test_frozen_app_finds_bundled_binaries(tmp_path, monkeypatch):
    from core import paths as p
    mp = _fake_meipass(tmp_path, monkeypatch)
    assert p.is_frozen() is True
    assert p.ffmpeg_path() == str(mp / "ffmpeg")
    assert p.ffprobe_path() == str(mp / "ffprobe")
    assert p.resource_path("templates") == mp / "templates"


def test_frozen_missing_binaries_falls_back_to_system(tmp_path, monkeypatch):
    from core import paths as p
    mp = tmp_path / "emptybundle"
    mp.mkdir()
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys._MEIPASS", str(mp), raising=False)
    monkeypatch.delenv("CLIPFORGE_FFMPEG", raising=False)
    monkeypatch.delenv("CLIPFORGE_FFPROBE", raising=False)
    # No bundled binaries here -> falls through to system PATH (exists on CI/dev)
    import shutil
    assert p.ffmpeg_path() == shutil.which("ffmpeg")
    status = p.ffmpeg_status()
    assert status["found"] is True
    assert status["ffmpeg"]["source"] == "system"


def test_ffmpeg_status_reports_missing_cleanly(tmp_path, monkeypatch):
    from core import paths as p
    mp = tmp_path / "emptybundle2"
    mp.mkdir()
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys._MEIPASS", str(mp), raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))  # nothing named ffmpeg here
    monkeypatch.delenv("CLIPFORGE_FFMPEG", raising=False)
    monkeypatch.delenv("CLIPFORGE_FFPROBE", raising=False)
    status = p.ffmpeg_status()
    assert status["found"] is False
    assert status["ffmpeg"]["path"] is None
    assert isinstance(status["guidance"], str) and len(status["guidance"]) > 20


def test_repo_local_packaging_bin_fallback(monkeypatch):
    # Dev from a cloned repo: packaging/bin is picked up without PATH setup.
    from core import paths as p
    import core.paths
    fake = "/tmp/fake-repo-packaging-bin-test"
    monkeypatch.delenv("CLIPFORGE_FFMPEG", raising=False)
    monkeypatch.setattr("sys.frozen", False, raising=False)
    monkeypatch.delenv("sys._MEIPASS", raising=False)
    monkeypatch.setattr(core.paths, "_repo_bin",
                        lambda name: f"{fake}/ffmpeg" if name == "ffmpeg" else None)
    monkeypatch.setattr("shutil.which", lambda name: None)
    path, src = core.paths._resolve_bin("ffmpeg", "CLIPFORGE_FFMPEG")
    assert path == f"{fake}/ffmpeg" and src == "repo"


# ------------------------------------------------------- /api/llm blank-safety

def test_api_llm_blank_fields_preserve_stored_keys(client):
    from core.moments import llm_keys
    llm_keys.save_keys(gemini_key="gem-keep", openai_key="oai-keep")
    r = client.post("/api/llm", json={"gemini_key": "gem-new",
                                      "openai_key": "", "provider": "auto"})
    assert r.get_json()["ok"] is True
    stored = llm_keys.load_keys()
    assert stored["gemini_key"] == "gem-new"
    assert stored["openai_key"] == "oai-keep"  # blank did NOT wipe it


def test_api_llm_forget_clears_one_key(client):
    from core.moments import llm_keys
    llm_keys.save_keys(gemini_key="gem-keep", openai_key="oai-keep")
    r = client.post("/api/llm/forget", json={"which": "gemini"})
    assert r.get_json()["ok"] is True
    stored = llm_keys.load_keys()
    assert stored["gemini_key"] == "" and stored["openai_key"] == "oai-keep"
    r = client.post("/api/llm/forget", json={"which": "bogus"})
    assert r.status_code == 400


def test_api_meta_reports_version_and_ffmpeg(client):
    r = client.get("/api/meta")
    j = r.get_json()
    assert j["ok"] is True
    from core.version import __version__
    assert j["version"] == __version__
    assert "found" in j["ffmpeg"] and "guidance" in j["ffmpeg"]


# ------------------------------------------------------- jobs survive restart

def test_load_jobs_reloads_persisted_jobs(client):
    import json
    import app as app_mod
    jd = app_mod.jobs_dir()
    job = {"id": "abc123", "status": "done", "progress": 100,
           "clips": [{"file": "x.mp4"}], "url": "https://youtu.be/x"}
    (jd / "abc123.json").write_text(json.dumps(job), encoding="utf-8")
    with app_mod._jobs_lock:
        app_mod._jobs.pop("abc123", None)
    app_mod._load_jobs()
    with app_mod._jobs_lock:
        loaded = app_mod._jobs.get("abc123")
    assert loaded is not None and loaded["status"] == "done"
    assert loaded["clips"][0]["file"] == "x.mp4"


def test_load_jobs_marks_inflight_as_interrupted(client):
    import json
    import app as app_mod
    jd = app_mod.jobs_dir()
    job = {"id": "zzz999", "status": "running", "progress": 42, "clips": []}
    (jd / "zzz999.json").write_text(json.dumps(job), encoding="utf-8")
    with app_mod._jobs_lock:
        app_mod._jobs.pop("zzz999", None)
    app_mod._load_jobs()
    with app_mod._jobs_lock:
        loaded = app_mod._jobs.get("zzz999")
    assert loaded["status"] == "interrupted"
    assert "restart" in loaded["error"].lower()


# ------------------------------------------------------- clip metadata editing

def _seed_job_with_clip(app_mod, job_id="meta1"):
    job = {
        "id": job_id, "status": "done", "progress": 100,
        "url": "https://youtu.be/x", "niche": "ai-news",
        "clips": [{
            "file": "x.mp4", "start": 0, "end": 30,
            "title": "Old title", "description": "Old desc",
            "hashtags": ["#Old"], "score": 80,
        }],
    }
    with app_mod._jobs_lock:
        app_mod._jobs[job_id] = job
    return job_id


def test_clip_metadata_partial_update(client):
    import app as app_mod
    jid = _seed_job_with_clip(app_mod)
    try:
        r = client.post(f"/api/jobs/{jid}/clips/0/metadata",
                        json={"title": "New title", "hashtags": "#a #b, c"})
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert body["clip"]["title"] == "New title"
        assert body["clip"]["hashtags"] == ["#a", "#b", "#c"]
        # untouched fields preserved
        assert body["clip"]["description"] == "Old desc"
        with app_mod._jobs_lock:
            assert app_mod._jobs[jid]["clips"][0]["title"] == "New title"
    finally:
        with app_mod._jobs_lock:
            app_mod._jobs.pop(jid, None)


def test_clip_metadata_not_found(client):
    r = client.post("/api/jobs/nope/clips/0/metadata", json={"title": "x"})
    assert r.status_code == 404
    import app as app_mod
    jid = _seed_job_with_clip(app_mod, "meta2")
    try:
        r = client.post(f"/api/jobs/{jid}/clips/9/metadata", json={"title": "x"})
        assert r.status_code == 404
    finally:
        with app_mod._jobs_lock:
            app_mod._jobs.pop(jid, None)


# ------------------------------------------------------- LLM model selection

def test_llm_model_save_and_status_roundtrip(client):
    r = client.post("/api/llm", json={"provider": "gemini", "model": "gemini-2.0-flash"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    r = client.get("/api/llm")
    j = r.get_json()
    assert j["model"] == "gemini-2.0-flash"
    assert j["provider"] == "gemini"
    # clearing back to default
    r = client.post("/api/llm", json={"model": ""})
    assert r.get_json()["ok"] is True
    assert client.get("/api/llm").get_json()["model"] == ""


def test_llm_model_rejects_unknown_id(client):
    r = client.post("/api/llm", json={"model": "gpt-99-turbo"})
    assert r.status_code == 400
    assert r.get_json()["ok"] is False


def test_llm_model_pin_reorders_provider_models(cfg_home, monkeypatch):
    from core.moments import llm as llm_mod
    from core.moments import llm_keys as keys_mod
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    keys_mod.save_keys(provider="gemini", model="gemini-1.5-flash")
    p = llm_mod.get_provider()
    assert p.name == "gemini"
    assert p.models[0] == "gemini-1.5-flash"  # pinned first
    assert set(p.models) == set(llm_mod.DEFAULT_GEMINI_MODELS)  # fallbacks kept
    keys_mod.save_keys(provider="gemini", model="")  # reset
    p2 = llm_mod.get_provider()
    assert p2.models[0] == llm_mod.DEFAULT_GEMINI_MODELS[0]


def test_discover_accepts_niche_focus_override(client, monkeypatch):
    seen = {}
    import app as app_mod
    monkeypatch.setattr(app_mod, "discover_for_autopilot",
                        lambda cfg, per_niche=3: (seen.update(cfg=cfg), {}))
    r = client.post("/api/discover", json={"niches": ["ai-news"]})
    assert r.status_code == 200
    run_id = r.get_json()["run_id"]
    # wait for background thread
    import time
    for _ in range(50):
        if app_mod._disc_runs[run_id]["status"] != "running":
            break
        time.sleep(0.1)
    assert seen["cfg"]["niches"] == ["ai-news"]


def test_discover_rejects_unknown_niche(client):
    r = client.post("/api/discover", json={"niches": ["not-a-niche"]})
    assert r.status_code == 400


def test_discover_custom_niche_needs_name(client, cfg_home):
    cfg = cfg_mod.load_config()
    cfg["custom_niche"] = ""
    cfg_mod.save_config(cfg)
    r = client.post("/api/discover", json={"niches": ["custom"]})
    assert r.status_code == 400
