"""Robustness tests for config + job persistence (the config/persistence subsystem).

Covers: core/config.py (load/save/validation of ~/.clipforge/config.json)
and the job JSON store in app.py (_load_jobs / _persist_job / jobs_dir).

No network, no browser. Filesystem tests use tmp dirs via CF_CONFIG_DIR.
"""

import copy
import json
import os
import threading
from pathlib import Path

import pytest

import app as app_mod
from core import config as cfg_mod


@pytest.fixture()
def cfg_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CF_CONFIG_DIR", str(tmp_path / ".clipforge"))
    return tmp_path / ".clipforge"


@pytest.fixture()
def appmod(cfg_home):
    """app module with CF_CONFIG_DIR pointed at tmp and an empty job registry."""
    with app_mod._jobs_lock:
        app_mod._jobs.clear()
    yield app_mod
    with app_mod._jobs_lock:
        app_mod._jobs.clear()


def _seed_job(appmod, job_id="abc123def456", **kw):
    job = {
        "id": job_id,
        "url": "https://youtu.be/dQw4w9WgXcQ",
        "niche_id": "gaming-news",
        "custom_niche": "",
        "caption_style": "karaoke",
        "max_clips": 3,
        "mode": "manual",
        "quality_gate": 50,
        "status": "done",
        "step": "Done!",
        "progress": 100,
        "clips": [],
        "error": None,
        "notes": [],
        "created": 1758800000.0,
    }
    job.update(kw)
    with appmod._jobs_lock:
        appmod._jobs[job_id] = job
    return job


# ---------------------------------------------------------------------------
# core/config.py — load_config tolerance
# ---------------------------------------------------------------------------

def _write_config(cfg_home, payload: bytes):
    cfg_home.mkdir(parents=True, exist_ok=True)
    (cfg_home / "config.json").write_bytes(payload)


def test_load_config_invalid_utf8_falls_back_to_defaults(cfg_home):
    # Was: UnicodeDecodeError propagated out of load_config() -> 500s.
    _write_config(cfg_home, b'{"mode": "\xff\xfe broken}')
    cfg = cfg_mod.load_config()  # must not raise
    assert cfg == cfg_mod.default_config()


def test_load_config_tolerates_bom(cfg_home):
    _write_config(cfg_home, b'\xef\xbb\xbf{"mode": "semi-auto"}')
    assert cfg_mod.load_config()["mode"] == "semi-auto"


def test_load_config_wrong_typed_values_fall_back_to_defaults(cfg_home):
    # Was: wrong types merged through and crashed consumers, e.g.
    # int("abc") ValueError in _new_job -> POST /api/jobs 500.
    _write_config(cfg_home, json.dumps({
        "mode": 123,
        "quality_gate": "abc",
        "daily_count": "x",
        "niches": "not-a-list",
        "times": None,
        "autopilot": "yes",
        "custom_niche": 5,
        "caption_style": "",
        "upload_privacy": "everywhere",
    }).encode())
    cfg = cfg_mod.load_config()
    dflt = cfg_mod.default_config()
    assert cfg["mode"] == "manual"
    assert cfg["quality_gate"] == dflt["quality_gate"] == 50
    assert cfg["daily_count"] == 2
    assert cfg["niches"] == dflt["niches"]
    assert cfg["times"] == ["09:00", "18:00"]
    assert cfg["autopilot"] is False
    assert cfg["custom_niche"] == ""
    # "" is a valid str: kept as-is, and every consumer treats it as unset
    # via `... or "karaoke"` fallbacks (validate_config rejects it at save).
    assert cfg["caption_style"] == ""
    # "everywhere" is a str: kept as-is. Membership ("public"/"unlisted"/
    # "private") is enforced at save time by validate_config and again at
    # upload time by core/upload (falls back to "unlisted" with a warning).
    assert cfg["upload_privacy"] == "everywhere"


def test_load_config_accepts_quality_gate_preset_string(cfg_home):
    # Hand-edited "high" means the same as it does at save time.
    _write_config(cfg_home, b'{"quality_gate": "high"}')
    assert cfg_mod.load_config()["quality_gate"] == 70


def test_load_config_accepts_integral_float(cfg_home):
    _write_config(cfg_home, b'{"daily_count": 2.0}')
    assert cfg_mod.load_config()["daily_count"] == 2


def test_load_config_valid_handwritten_file_loads_as_is(cfg_home):
    _write_config(cfg_home, json.dumps({
        "mode": "autopilot", "daily_count": 3,
        "times": ["09:00", "12:00", "18:00"],
        "niches": ["gaming-news"], "quality_gate": 70,
    }).encode())
    cfg = cfg_mod.load_config()
    assert (cfg["mode"], cfg["daily_count"], cfg["quality_gate"]) == \
        ("autopilot", 3, 70)
    assert cfg["times"] == ["09:00", "12:00", "18:00"]


def test_load_config_corrupt_json_falls_back_to_defaults(cfg_home):
    _write_config(cfg_home, b'{"mode": "manual", ')
    assert cfg_mod.load_config() == cfg_mod.default_config()


def test_load_config_non_dict_json_falls_back_to_defaults(cfg_home):
    _write_config(cfg_home, b'[1, 2, 3]')
    assert cfg_mod.load_config() == cfg_mod.default_config()


def test_config_dir_empty_env_falls_back_to_default(tmp_path, monkeypatch):
    # Was: CF_CONFIG_DIR="" -> Path(".") -> config/jobs written into CWD.
    monkeypatch.setenv("CF_CONFIG_DIR", "")
    assert cfg_mod.config_dir() == Path.home() / ".clipforge"


# ---------------------------------------------------------------------------
# core/config.py — save_config atomicity
# ---------------------------------------------------------------------------

def _valid_payload():
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


def test_save_config_writes_valid_json_and_no_tmp_left_behind(cfg_home):
    path = cfg_mod.save_config(_valid_payload())
    assert path.exists()
    json.loads(path.read_text(encoding="utf-8"))  # parses
    assert not path.with_name(path.name + ".tmp").exists()


def test_save_config_crash_mid_write_keeps_old_file(cfg_home, monkeypatch):
    # Atomicity: if the process dies during the rename, the previous valid
    # config must survive (not a torn half-file).
    cfg_mod.save_config(_valid_payload())
    before = cfg_mod.config_path().read_text(encoding="utf-8")

    def boom(*a, **k):
        raise KeyboardInterrupt("simulated kill")

    monkeypatch.setattr(os, "replace", boom)
    payload = _valid_payload()
    payload["mode"] = "autopilot"
    with pytest.raises(KeyboardInterrupt):
        cfg_mod.save_config(payload)
    after = cfg_mod.config_path().read_text(encoding="utf-8")
    assert after == before
    assert json.loads(after)["mode"] == "semi-auto"


def test_atomic_write_text_roundtrip(tmp_path):
    p = tmp_path / "x.txt"
    cfg_mod.atomic_write_text(p, "héllo")
    assert p.read_text(encoding="utf-8") == "héllo"


# ---------------------------------------------------------------------------
# app.py — _persist_job
# ---------------------------------------------------------------------------

def test_persist_job_writes_valid_json(appmod, cfg_home):
    _seed_job(appmod, clips=[{"index": 0, "title": "héllo wörld"}])
    appmod._persist_job("abc123def456")
    p = cfg_home / "jobs" / "abc123def456.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["id"] == "abc123def456"
    assert data["clips"][0]["title"] == "héllo wörld"
    assert "notes" not in data  # slim: notes are memory-only
    assert not p.with_name(p.name + ".tmp").exists()


def test_persist_job_unknown_id_is_noop(appmod, cfg_home):
    appmod._persist_job("nope00000000")
    assert not (cfg_home / "jobs").exists() or \
        not list((cfg_home / "jobs").glob("*.json"))


def test_persist_job_rejects_unsafe_job_ids(appmod, cfg_home, tmp_path):
    # Defense at the sink: a crafted id must never escape the jobs dir,
    # even if a route's 404 guard were ever bypassed.
    _seed_job(appmod, job_id="abc123def456")
    for bad in ("../evil", "..\\evil", "..", "", "a/b"):
        appmod._persist_job(bad)  # must not raise
    jobs = cfg_home / "jobs"
    names = [p.name for p in jobs.glob("*")] if jobs.exists() else []
    assert names == [], f"unsafe ids wrote files: {names}"
    assert not (tmp_path / "evil").exists()


def test_persist_job_concurrent_writes_never_tear_the_file(appmod, cfg_home):
    # Worker thread (finally block) and dashboard requests can persist the
    # same job at once. The file must always be whole valid JSON afterwards.
    _seed_job(appmod)
    barrier = threading.Barrier(8)
    errors = []

    def worker(n):
        try:
            barrier.wait(timeout=10)
            for i in range(25):
                with appmod._jobs_lock:
                    appmod._jobs["abc123def456"]["progress"] = (n * 25 + i) % 101
                    appmod._jobs["abc123def456"]["step"] = f"t{n}-{i}-" + "x" * 50
                appmod._persist_job("abc123def456")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, f"persist raised under concurrency: {errors!r}"
    p = cfg_home / "jobs" / "abc123def456.json"
    data = json.loads(p.read_text(encoding="utf-8"))  # must parse
    assert data["id"] == "abc123def456"


def test_persist_job_crash_mid_write_keeps_old_file(appmod, cfg_home,
                                                    monkeypatch):
    _seed_job(appmod, step="old-step")
    appmod._persist_job("abc123def456")
    p = cfg_home / "jobs" / "abc123def456.json"
    before = p.read_text(encoding="utf-8")

    def boom(*a, **k):
        raise KeyboardInterrupt("simulated kill")

    monkeypatch.setattr(os, "replace", boom)
    with appmod._jobs_lock:
        appmod._jobs["abc123def456"]["step"] = "new-step"
    with pytest.raises(KeyboardInterrupt):
        appmod._persist_job("abc123def456")
    assert p.read_text(encoding="utf-8") == before
    assert json.loads(before)["step"] == "old-step"


def test_persist_job_huge_job_always_writes_valid_json(appmod, cfg_home):
    # A job past the size cap must still persist as VALID JSON (the old
    # code sliced the string mid-JSON -> unloadable -> job lost on restart).
    big_clip = {"index": 0, "title": "t", "description": "d" * 300_000}
    _seed_job(appmod, clips=[big_clip])
    appmod._persist_job("abc123def456")
    p = cfg_home / "jobs" / "abc123def456.json"
    data = json.loads(p.read_text(encoding="utf-8"))  # must parse
    assert data["id"] == "abc123def456"
    assert len(p.read_text(encoding="utf-8")) <= 200_000


def test_slim_job_text_always_returns_valid_json(appmod):
    job = _seed_job(appmod, clips=[
        {"index": i, "description": "z" * 100_000} for i in range(10)])
    text = appmod._slim_job_text(job)
    assert len(text) <= 200_000
    assert json.loads(text)["id"] == "abc123def456"


def test_persist_job_nonserializable_value_does_not_raise(appmod, cfg_home):
    # A weird in-memory value must not kill the worker thread's finally
    # block: persist is skipped, the exception never escapes.
    _seed_job(appmod, clips=[{"weird": {1, 2, 3}}])
    appmod._persist_job("abc123def456")  # must not raise


# ---------------------------------------------------------------------------
# app.py — _load_jobs
# ---------------------------------------------------------------------------

def test_load_jobs_skips_corrupt_files(appmod, cfg_home):
    jobs = cfg_home / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    (jobs / "truncated.json").write_text('{"id": "a", "status": "do',
                                         encoding="utf-8")
    (jobs / "notjson.json").write_text("{not json", encoding="utf-8")
    (jobs / "badutf8.json").write_bytes(b'{"id": "\xff"}')
    (jobs / "list.json").write_text("[1, 2]", encoding="utf-8")
    (jobs / "empty.json").write_text("", encoding="utf-8")
    (jobs / "noid.json").write_text('{"status": "done"}', encoding="utf-8")
    (jobs / "dir.json").mkdir()  # a directory named *.json
    appmod._load_jobs()  # must not raise
    assert appmod._jobs == {}


def test_load_jobs_marks_inflight_jobs_interrupted(appmod, cfg_home):
    jobs = cfg_home / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    for jid, status in (("aa11bb22cc33", "queued"), ("dd44ee55ff66", "running"),
                        ("ok77ok88ok99", "done")):
        (jobs / f"{jid}.json").write_text(
            json.dumps({"id": jid, "status": status, "clips": []}),
            encoding="utf-8")
    appmod._load_jobs()
    assert appmod._jobs["aa11bb22cc33"]["status"] == "interrupted"
    assert appmod._jobs["dd44ee55ff66"]["status"] == "interrupted"
    assert "restart" in appmod._jobs["aa11bb22cc33"]["error"].lower()
    assert appmod._jobs["ok77ok88ok99"]["status"] == "done"


def test_load_jobs_normalizes_legacy_schema(appmod, cfg_home):
    # Old/hand-edited files missing keys or with wrong types must not
    # KeyError/TypeError the dashboard (e.g. len(job["clips"])).
    jobs = cfg_home / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    (jobs / "legacy.json").write_text(json.dumps({
        "id": "legacy000001",
        "status": "done",
        # no "clips", no "notes", no "created"
    }), encoding="utf-8")
    (jobs / "weird.json").write_text(json.dumps({
        "id": "weird0000002",
        "status": "done",
        "clips": None,
        "notes": "oops",
    }), encoding="utf-8")
    appmod._load_jobs()
    legacy = appmod._jobs["legacy000001"]
    assert legacy["clips"] == [] and legacy["notes"] == []
    weird = appmod._jobs["weird0000002"]
    assert weird["clips"] == [] and weird["notes"] == []
    # The shapes the dashboard list route relies on:
    for j in appmod._jobs.values():
        assert isinstance(j["clips"], list)
        len(j["clips"])


def test_load_jobs_roundtrip(appmod, cfg_home):
    job = _seed_job(appmod, clips=[{"index": 0, "title": "hi"}])
    appmod._persist_job("abc123def456")
    with appmod._jobs_lock:
        appmod._jobs.clear()
    appmod._load_jobs()
    loaded = appmod._jobs["abc123def456"]
    assert loaded["clips"] == [{"index": 0, "title": "hi"}]
    assert loaded["progress"] == 100
    assert loaded["created"] == 1758800000.0


def test_load_jobs_unwritable_dir_does_not_crash_startup(appmod, tmp_path,
                                                        monkeypatch):
    # jobs_dir() cannot be created (CF_CONFIG_DIR points at a file):
    # startup must continue, persist must silently skip.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("CF_CONFIG_DIR", str(blocker))
    appmod._load_jobs()  # must not raise
    _seed_job(appmod)
    appmod._persist_job("abc123def456")  # must not raise


# ---------------------------------------------------------------------------
# blank key-save behavior (empty API key vs missing key)
# ---------------------------------------------------------------------------

def test_blank_key_save_preserves_stored_key(cfg_home):
    """POST /api/llm with blank fields must keep the stored key (never wipe)."""
    from core.moments import llm_keys

    appmod = app_mod
    appmod.app.config["TESTING"] = True
    client = appmod.app.test_client()
    llm_keys.save_keys(gemini_key="AIzaREAL")
    r = client.post("/api/llm", json={
        "gemini_key": "", "openai_key": "", "provider": "auto"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert llm_keys.load_keys()["gemini_key"] == "AIzaREAL"


def test_empty_vs_missing_key_both_mean_offline(cfg_home):
    """An explicitly-emptied key and a never-set key behave identically."""
    from core.moments import llm_keys
    from core.moments.llm import LLMError, get_provider

    assert llm_keys.load_keys()["gemini_key"] == ""  # missing file
    with pytest.raises(LLMError):
        get_provider()
    llm_keys.save_keys(gemini_key="")  # explicitly empty
    assert llm_keys.load_keys()["gemini_key"] == ""
    with pytest.raises(LLMError):
        get_provider()
