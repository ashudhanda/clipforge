"""Packaging robustness tests — the NEXT missing-data-file incident gets caught here.

Covers packaging/clipforge.spec (parsed as text/AST — no installer build
needed) and core/paths.py (binary resolution order, first-run self-check)
with simulated frozen layouts via monkeypatched sys attributes.

Background: v0.1.6 shipped faster_whisper WITHOUT its data file
assets/silero_vad_v6.onnx because PyInstaller's import analysis misses data
files. These tests pin every known data-file collection and forbid the
silent-skip pattern that caused that incident.
"""

import ast
import os
import shutil
import sys
from pathlib import Path

import pytest

from core import paths

REPO = Path(__file__).resolve().parent.parent
SPEC = REPO / "packaging" / "clipforge.spec"


# ---------------------------------------------------------------------------
# spec parsing helpers (no PyInstaller build required)


def _spec_source() -> str:
    return SPEC.read_text(encoding="utf-8")


def _collect_call_sources() -> list[str]:
    """Source text of every collect_data_files(...) call in the spec."""
    src = _spec_source()
    tree = ast.parse(src, filename=str(SPEC))
    segs = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "collect_data_files":
            seg = ast.get_source_segment(src, node)
            assert seg is not None, "could not extract collect_data_files call source"
            segs.append(seg)
    return segs


# ---------------------------------------------------------------------------
# spec: every known data file must be collected


def test_spec_collects_faster_whisper_data():
    segs = _collect_call_sources()
    assert any("faster_whisper" in s for s in segs), \
        "spec must collect faster_whisper data files (v0.1.6 regression: silero_vad_v6.onnx)"


def test_spec_collects_tzdata():
    segs = _collect_call_sources()
    assert any("tzdata" in s for s in segs), \
        "spec must collect tzdata (frozen app crashes on Windows without zoneinfo data)"


def test_spec_collects_cv2_haar_cascades():
    segs = _collect_call_sources()
    matches = [s for s in segs if "cv2" in s]
    assert matches, "spec must collect cv2 data files"
    assert any("data" in s for s in matches), \
        "spec must collect cv2's data/ subdir (Haar cascade XMLs) — hook-cv2.py does not"


def test_spec_collects_youtube_discovery_doc_only():
    segs = _collect_call_sources()
    matches = [s for s in segs if "googleapiclient" in s]
    assert matches, "spec must collect the googleapiclient discovery doc"
    for s in matches:
        assert "youtube.v3.json" in s, \
            "spec must pin youtube.v3.json explicitly — the full documents/ dir is ~100MB"
        assert "discovery_cache" in s, "discovery doc must come from discovery_cache/documents"


def test_spec_data_collection_failures_are_loud():
    """Forbid the v0.1.6 pattern: `except Exception: pass` around data collection.

    A silently-skipped data dir ships a broken bundle with zero signal.
    Collection errors in a real build must fail the build loudly; only the
    ImportError fallback (spec parsed without PyInstaller installed) may pass.
    """
    tree = ast.parse(_spec_source(), filename=str(SPEC))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        has_collect = any(
            isinstance(n, ast.Call) and getattr(n.func, "id", None) == "collect_data_files"
            for n in ast.walk(node)
        )
        if not has_collect:
            continue
        for handler in node.handlers:
            broad = handler.type is None or getattr(handler.type, "id", "") == "Exception"
            silent = len(handler.body) == 1 and isinstance(handler.body[0], ast.Pass)
            assert not (broad and silent), (
                "spec swallows data-collection errors with `except Exception: pass` — "
                "the v0.1.6 failure pattern. Let collection errors fail the build loudly."
            )


def test_collect_calls_resolve_to_real_files():
    """The exact collect_data_files() calls the spec relies on must return the
    expected files in the build environment (skipped where PyInstaller is absent)."""
    hooks = pytest.importorskip("PyInstaller.utils.hooks")
    collect = hooks.collect_data_files

    fw = collect("faster_whisper")
    assert any(src.endswith("silero_vad_v6.onnx") for src, _ in fw), \
        "faster_whisper VAD asset not resolvable via collect_data_files"

    cv = collect("cv2", subdir="data")
    assert any(os.path.basename(src) == "haarcascade_frontalface_default.xml"
               for src, _ in cv), "cv2 Haar cascade not resolvable via collect_data_files"
    # .py files must NOT ride along (cv2 is collected in source form by
    # hook-cv2.py's module_collection_mode='py'; duplicates would clash).
    assert all(src.endswith(".xml") for src, _ in cv), \
        f"cv2 data collection pulled non-XML files: {[src for src, _ in cv if not src.endswith('.xml')]}"

    ga = collect("googleapiclient",
                 subdir=os.path.join("discovery_cache", "documents"),
                 includes=["youtube.v3.json"])
    assert len(ga) == 1 and ga[0][0].endswith("youtube.v3.json"), \
        f"expected exactly youtube.v3.json, got: {ga}"
    assert os.path.isfile(ga[0][0])


# ---------------------------------------------------------------------------
# binary resolution order: env -> bundled (_MEIPASS) -> repo packaging/bin -> PATH


@pytest.fixture()
def frozen_bundle(tmp_path, monkeypatch):
    mp = tmp_path / "bundle"
    mp.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(mp), raising=False)
    monkeypatch.delenv("CLIPFORGE_FFMPEG", raising=False)
    monkeypatch.delenv("CLIPFORGE_FFPROBE", raising=False)
    return mp


@pytest.fixture()
def unfrozen(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.delenv("CLIPFORGE_FFMPEG", raising=False)
    monkeypatch.delenv("CLIPFORGE_FFPROBE", raising=False)


@pytest.fixture()
def fake_repo(monkeypatch, tmp_path):
    """A cloned-repo layout whose paths.py we pretend to be."""
    repo = tmp_path / "repo"
    (repo / "core").mkdir(parents=True)
    monkeypatch.setattr(paths, "__file__", str(repo / "core" / "paths.py"))
    return repo


def _write_exe(path: Path) -> None:
    path.write_bytes(b"fake-binary")
    if os.name == "posix":
        path.chmod(0o755)


def test_is_frozen_false_by_default(unfrozen):
    assert paths.is_frozen() is False


def test_env_override_beats_everything(frozen_bundle, monkeypatch):
    _write_exe(frozen_bundle / paths._exe_name("ffmpeg"))
    monkeypatch.setenv("CLIPFORGE_FFMPEG", "/custom/ffmpeg")
    assert paths.ffmpeg_path() == "/custom/ffmpeg"
    assert paths.ffmpeg_status()["ffmpeg"]["source"] == "env"


def test_bundled_binary_beats_system(frozen_bundle, monkeypatch):
    exe = paths._exe_name("ffmpeg")
    _write_exe(frozen_bundle / exe)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ffmpeg")
    status = paths.ffmpeg_status()
    assert status["ffmpeg"]["source"] == "bundled"
    assert status["ffmpeg"]["path"] == str(frozen_bundle / exe)


def test_bundled_binary_bin_subdir_fallback(frozen_bundle):
    bindir = frozen_bundle / "bin"
    bindir.mkdir()
    exe = paths._exe_name("ffmpeg")
    _write_exe(bindir / exe)
    path, src = paths._resolve_bin("ffmpeg", "CLIPFORGE_FFMPEG")
    assert src == "bundled"
    assert path == str(bindir / exe)


def test_repo_bin_used_from_cloned_repo(unfrozen, fake_repo, monkeypatch):
    exe = paths._exe_name("ffmpeg")
    bindir = fake_repo / "packaging" / "bin"
    bindir.mkdir(parents=True)
    _write_exe(bindir / exe)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    path, src = paths._resolve_bin("ffmpeg", "CLIPFORGE_FFMPEG")
    assert src == "repo"
    assert path == str(bindir / exe)


def test_system_path_is_last_resort(unfrozen, fake_repo, monkeypatch):
    # fake_repo has no packaging/bin -> repo step misses; PATH is the fallback.
    monkeypatch.setattr(shutil, "which",
                        lambda name: "/usr/bin/" + name if name in ("ffmpeg", "ffprobe") else None)
    path, src = paths._resolve_bin("ffmpeg", "CLIPFORGE_FFMPEG")
    assert (path, src) == ("/usr/bin/ffmpeg", "system")


def test_no_binary_found_reports_actionable_guidance(unfrozen, fake_repo, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert paths.ffmpeg_path() is None
    assert paths.ffprobe_path() is None
    status = paths.ffmpeg_status()
    assert status["found"] is False
    assert status["guidance"], "missing ffmpeg must come with actionable guidance, not silence"
    assert "ffmpeg" in status["guidance"].lower()


def test_resource_path_frozen(frozen_bundle):
    assert paths.resource_path("templates") == frozen_bundle / "templates"
    assert paths.resource_path("assets", "fonts") == frozen_bundle / "assets" / "fonts"


def test_package_dir_frozen_layout(frozen_bundle):
    docdir = frozen_bundle / "googleapiclient" / "discovery_cache" / "documents"
    docdir.mkdir(parents=True)
    assert paths._package_dir("googleapiclient") == frozen_bundle / "googleapiclient"


def test_package_dir_dev_resolves_installed_package(unfrozen):
    d = paths._package_dir("cv2")
    assert d is not None and d.is_dir()
    assert (d / "data" / "haarcascade_frontalface_default.xml").is_file()


# ---------------------------------------------------------------------------
# bundle_self_check(): first-run verification, never raises


def _healthy_bins(monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name in ("ffmpeg", "ffprobe"):
        p = bindir / name
        p.write_bytes(b"fake")
        if os.name == "posix":
            p.chmod(0o755)
    monkeypatch.setattr(paths, "_resolve_bin",
                        lambda name, env: (str(bindir / name), "env"))


@pytest.fixture()
def fake_pkg_data(tmp_path, monkeypatch):
    """Hermetic third-party data tree standing in for installed packages."""
    root = tmp_path / "pkgs"

    def _put(*parts: str) -> None:
        f = root.joinpath(*parts)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")

    _put("faster_whisper", "assets", "silero_vad_v6.onnx")
    _put("cv2", "data", "haarcascade_frontalface_default.xml")
    _put("googleapiclient", "discovery_cache", "documents", "youtube.v3.json")
    monkeypatch.setattr(paths, "_package_dir",
                        lambda pkg: root / pkg if (root / pkg).is_dir() else None)
    return root


def test_self_check_clean_when_healthy(monkeypatch, fake_pkg_data, tmp_path):
    _healthy_bins(monkeypatch, tmp_path)
    # repo checkout has templates/, assets/fonts/, previews/
    assert paths.bundle_self_check() == []


def test_self_check_missing_ffmpeg_is_error(monkeypatch, fake_pkg_data):
    monkeypatch.setattr(paths, "_resolve_bin", lambda name, env: (None, None))
    findings = paths.bundle_self_check()
    by_asset = {f["asset"]: f for f in findings}
    assert by_asset["ffmpeg"]["level"] == "error"
    assert by_asset["ffprobe"]["level"] == "error"
    assert by_asset["ffmpeg"]["detail"], "ffmpeg error must carry actionable guidance"


def test_self_check_missing_templates_is_error(monkeypatch, fake_pkg_data, tmp_path):
    _healthy_bins(monkeypatch, tmp_path)
    monkeypatch.setattr(paths, "resource_path",
                        lambda *parts: tmp_path / "nowhere" / "_".join(parts))
    findings = paths.bundle_self_check()
    by_asset = {f["asset"]: f for f in findings}
    assert by_asset["templates/"]["level"] == "error"
    # fonts/previews degrade gracefully -> warnings, not errors
    assert by_asset["assets/fonts/"]["level"] == "warning"


def test_self_check_missing_discovery_doc_is_error(monkeypatch, fake_pkg_data, tmp_path):
    _healthy_bins(monkeypatch, tmp_path)
    (fake_pkg_data / "googleapiclient" / "discovery_cache" / "documents"
     / "youtube.v3.json").unlink()
    findings = paths.bundle_self_check()
    doc = [f for f in findings if "youtube.v3.json" in f["asset"]]
    assert len(doc) == 1, f"expected one discovery-doc finding, got: {findings}"
    assert doc[0]["level"] == "error"
    assert "UnknownApiNameOrVersion" in doc[0]["detail"]


def test_self_check_missing_haar_cascade_is_warning_only(monkeypatch, fake_pkg_data, tmp_path):
    _healthy_bins(monkeypatch, tmp_path)
    (fake_pkg_data / "cv2" / "data" / "haarcascade_frontalface_default.xml").unlink()
    findings = paths.bundle_self_check()
    haar = [f for f in findings if "haarcascade" in f["asset"]]
    assert len(haar) == 1, f"expected one haar finding, got: {findings}"
    assert haar[0]["level"] == "warning"
    assert not any(f["level"] == "error" for f in findings), \
        "missing Haar cascade must not be an error (callers fall back to center crop)"


def test_self_check_missing_vad_model_is_warning_only(monkeypatch, fake_pkg_data, tmp_path):
    _healthy_bins(monkeypatch, tmp_path)
    (fake_pkg_data / "faster_whisper" / "assets" / "silero_vad_v6.onnx").unlink()
    findings = paths.bundle_self_check()
    vad = [f for f in findings if "silero_vad" in f["asset"]]
    assert len(vad) == 1 and vad[0]["level"] == "warning"


def _boom(*args, **kwargs):
    raise RuntimeError("boom")


def test_self_check_never_raises(monkeypatch):
    monkeypatch.setattr(paths, "_resolve_bin", _boom)
    monkeypatch.setattr(paths, "resource_path", _boom)
    monkeypatch.setattr(paths, "_package_dir", _boom)
    findings = paths.bundle_self_check()  # must not raise
    assert isinstance(findings, list) and findings, \
        "self-check must report findings, never raise"


@pytest.mark.skipif(os.name != "posix", reason="exec-bit check is posix-only")
def test_self_check_flags_non_executable_binary(monkeypatch, fake_pkg_data, tmp_path):
    ff = tmp_path / "ffmpeg"
    ff.write_bytes(b"fake")
    ff.chmod(0o644)  # present but not executable
    fp = tmp_path / "ffprobe"
    _write_exe(fp)
    monkeypatch.setattr(paths, "_resolve_bin",
                        lambda name, env: (str(tmp_path / name), "bundled"))
    findings = paths.bundle_self_check()
    by_asset = {f["asset"]: f for f in findings}
    assert by_asset["ffmpeg"]["level"] == "error"
    assert "executable" in by_asset["ffmpeg"]["detail"]
    assert "ffprobe" not in by_asset, "executable ffprobe must not be flagged"
