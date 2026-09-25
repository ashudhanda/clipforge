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


def test_index_shows_setup_before_config(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"Welcome to ClipForge" in r.data


def test_setup_flow_then_dashboard(client):
    r = client.post("/api/setup", json=valid_payload())
    assert r.status_code == 200
    assert r.get_json()["ok"] is True

    r = client.get("/")
    assert r.status_code == 200
    assert b"Make clips from a video" in r.data


def test_setup_rejects_bad_style(client):
    payload = valid_payload(); payload["caption_style"] = "comic-sans"
    r = client.post("/api/setup", json=payload)
    assert r.status_code == 400
    assert r.get_json()["ok"] is False


def test_setup_rejects_invalid_payload(client):
    payload = valid_payload(); payload["niches"] = []
    r = client.post("/api/setup", json=payload)
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


def test_setup_api_accepts_gate_preset_string(client):
    payload = valid_payload()
    payload["quality_gate"] = "low"
    r = client.post("/api/setup", json=payload)
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
