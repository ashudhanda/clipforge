"""Tests for LLM API key storage (core/moments/llm_keys.py) and the /api/llm endpoints.

Keys live in ~/.clipforge/llm_keys.json (0600) — never in config.json.
No network calls; get_provider() only constructs provider objects here.
"""

import json
import os
import stat

import pytest

from core.moments import llm_keys
from core.moments.llm import (
    GeminiProvider,
    LLMError,
    OpenAIProvider,
    get_provider,
)


@pytest.fixture()
def cfg_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CF_CONFIG_DIR", str(tmp_path / ".clipforge"))
    for v in ("GEMINI_API_KEY", "OPENAI_API_KEY", "CF_LLM_PROVIDER"):
        monkeypatch.delenv(v, raising=False)
    return tmp_path / ".clipforge"


# ------------------------------------------------------------ save / load


def test_load_missing_file_returns_blanks(cfg_home):
    assert llm_keys.load_keys() == {
        "gemini_key": "", "openai_key": "", "provider": "auto", "model": ""}


def test_save_load_roundtrip(cfg_home):
    llm_keys.save_keys(gemini_key="  AIza123  ", openai_key="sk-abc",
                       provider="openai", model="gpt-4o-mini")
    assert llm_keys.load_keys() == {
        "gemini_key": "AIza123", "openai_key": "sk-abc",
        "provider": "openai", "model": "gpt-4o-mini"}


def test_save_creates_0600_file(cfg_home):
    p = llm_keys.save_keys(gemini_key="k")
    mode = stat.S_IMODE(os.stat(p).st_mode)
    assert mode == 0o600, f"key file must be owner-only, got {oct(mode)}"


def test_save_enforces_0600_on_existing_file(cfg_home):
    p = llm_keys.save_keys(gemini_key="k")
    os.chmod(p, 0o644)
    llm_keys.save_keys(gemini_key="k2")
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_load_corrupt_json_returns_blanks(cfg_home):
    cfg_home.mkdir(parents=True, exist_ok=True)
    (cfg_home / "llm_keys.json").write_text("{not json", encoding="utf-8")
    assert llm_keys.load_keys()["gemini_key"] == ""


def test_load_ignores_bad_provider(cfg_home):
    cfg_home.mkdir(parents=True, exist_ok=True)
    (cfg_home / "llm_keys.json").write_text(
        json.dumps({"gemini_key": "k", "provider": "wat"}), encoding="utf-8")
    out = llm_keys.load_keys()
    assert out["gemini_key"] == "k" and out["provider"] == "auto"


def test_save_bad_provider_raises(cfg_home):
    with pytest.raises(ValueError):
        llm_keys.save_keys(provider="wat")


def test_forget_key_clears_only_that_key(cfg_home):
    llm_keys.save_keys(gemini_key="gem-k", openai_key="oai-k")
    llm_keys.forget_key("gemini")
    out = llm_keys.load_keys()
    assert out["gemini_key"] == "" and out["openai_key"] == "oai-k"


def test_forget_key_bad_which_raises(cfg_home):
    with pytest.raises(ValueError):
        llm_keys.forget_key("anthropic")


# ------------------------------------------------------------ get_provider


def test_provider_falls_back_to_stored_gemini_key(cfg_home):
    llm_keys.save_keys(gemini_key="stored-gem")
    p = get_provider()
    assert isinstance(p, GeminiProvider)
    assert p.api_key == "stored-gem"


def test_provider_falls_back_to_stored_openai_key(cfg_home):
    llm_keys.save_keys(openai_key="stored-oai")
    p = get_provider()
    assert isinstance(p, OpenAIProvider)


def test_env_key_beats_stored_key(cfg_home, monkeypatch):
    llm_keys.save_keys(gemini_key="stored-gem")
    monkeypatch.setenv("GEMINI_API_KEY", "env-gem")
    assert get_provider().api_key == "env-gem"


def test_stored_provider_preference_respected(cfg_home):
    llm_keys.save_keys(gemini_key="g", openai_key="o", provider="openai")
    assert isinstance(get_provider(), OpenAIProvider)


def test_no_keys_anywhere_raises(cfg_home):
    with pytest.raises(LLMError):
        get_provider()


# ------------------------------------------------------------ /api/llm


@pytest.fixture()
def client(cfg_home):
    import app as app_mod
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def test_api_llm_status_hides_values(client):
    llm_keys.save_keys(gemini_key="AIzaSECRET")
    r = client.get("/api/llm")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["gemini_set"] is True
    assert body["openai_set"] is False
    assert "AIzaSECRET" not in r.get_data(as_text=True)
    assert body["active"]["provider"] == "gemini"
    assert body["active"]["model"] == "gemini-2.5-flash"
    assert body["models"]["gemini"][0] == "gemini-2.5-flash"


def test_api_llm_status_no_key_means_offline(client):
    r = client.get("/api/llm")
    body = r.get_json()
    assert body["active"] == {"provider": None, "model": None}


def test_api_llm_save_roundtrip(client):
    r = client.post("/api/llm", json={
        "gemini_key": "AIzaNEW", "openai_key": "", "provider": "auto"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    assert llm_keys.load_keys()["gemini_key"] == "AIzaNEW"
    assert stat.S_IMODE(os.stat(llm_keys.keys_path()).st_mode) == 0o600


def test_api_llm_save_bad_provider(client):
    r = client.post("/api/llm", json={"provider": "wat"})
    assert r.status_code == 400
    assert r.get_json()["ok"] is False
