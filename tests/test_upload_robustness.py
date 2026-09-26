"""Robustness regressions for core/upload.

Every test pins a CONCRETE failure mode found by auditing the upload
subsystem (see the bug-hunt report). All Google API calls are mocked:
no network, no real credentials, no browser.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.upload import oauth, upload_clip, youtube_api
from core.upload.youtube_api import (QuotaExceeded, QuotaTracker, UploadError,
                                     upload_short)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

def _http_error(status: int, reason: str | None = None):
    from googleapiclient.errors import HttpError

    if reason is not None:
        content = json.dumps(
            {"error": {"code": status,
                       "errors": [{"reason": reason,
                                   "message": "fake"}]}}).encode()
    else:
        content = b"fake-error"
    return HttpError(resp=SimpleNamespace(status=status, reason="fake"),
                     content=content)


class FakeRequest:
    """Scripted upload request: items are (status, response) or exceptions.

    Supports both the resumable path (next_chunk) and the single-shot
    multipart path (execute). ``resumable_mode=False`` mimics a real
    non-resumable request, which exposes ``resumable=None``.
    """

    def __init__(self, script, resumable_mode=True, stuck_progress=False):
        self.script = list(script)
        self.calls = 0
        self._resumable_mode = resumable_mode
        if stuck_progress:
            # Server accepts nothing: byte position never advances.
            self.resumable_progress = 0

    @property
    def resumable(self):
        return True if self._resumable_mode else None

    def _next_item(self):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def next_chunk(self):
        return self._next_item()

    def execute(self):
        return self._next_item()


class FakeVideos:
    def __init__(self, request):
        self._request = request
        self.captured = {}

    def insert(self, part=None, body=None, media_body=None):
        self.captured = {"part": part, "body": body}
        return self._request


class FakeService:
    def __init__(self, request):
        self.videos_obj = FakeVideos(request)

    def videos(self):
        return self.videos_obj


class FakeCreds:
    def __init__(self, valid=True, expired=False, refresh_token=None):
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token


class _FakeCompletedProcess:
    def __init__(self, stdout):
        self.stdout = stdout


def _ffprobe_stream_json(width, height, tags=None, side_data=None):
    stream = {"width": width, "height": height}
    if tags is not None:
        stream["tags"] = tags
    if side_data is not None:
        stream["side_data_list"] = side_data
    return json.dumps({"streams": [stream]})


@pytest.fixture()
def tmp_video(tmp_path):
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"\x00" * 1024)
    return str(p)


@pytest.fixture()
def quota(tmp_path, monkeypatch):
    q = QuotaTracker(tmp_path / "quota.json")
    monkeypatch.setattr(youtube_api, "_is_portrait", lambda p: True)
    return q


def _patch_upload_env(monkeypatch, service, backoffs, creds_fn=None):
    monkeypatch.setattr(youtube_api, "_build_service",
                        lambda creds: service)
    monkeypatch.setattr(youtube_api.oauth_mod, "get_credentials",
                        creds_fn or (lambda *a, **k: FakeCreds()))
    monkeypatch.setattr(youtube_api, "_backoff",
                        lambda attempt: backoffs.append(attempt))


# ---------------------------------------------------------------------------
# P0: resumable loop conflated chunk progress with retry attempts —
# any upload needing more than 5 chunks could never finish.
# ---------------------------------------------------------------------------

def test_resumable_completes_after_many_chunks(tmp_video, quota, monkeypatch):
    script = [(None, None)] * 7 + [(None, {"id": "bigvid"})]
    req = FakeRequest(script)
    svc = FakeService(req)
    backoffs: list = []
    _patch_upload_env(monkeypatch, svc, backoffs)
    out = upload_short(tmp_video, "t", _quota=quota)
    assert out["video_id"] == "bigvid"
    assert req.calls == 8
    assert backoffs == []
    assert quota.used_today() == 1


def test_resumable_intermittent_failures_reset_backoff(tmp_video, quota,
                                                       monkeypatch):
    script = [_http_error(500), (None, None),
              _http_error(503), (None, None),
              (None, {"id": "v"})]
    req = FakeRequest(script)
    svc = FakeService(req)
    backoffs: list = []
    _patch_upload_env(monkeypatch, svc, backoffs)
    out = upload_short(tmp_video, "t", _quota=quota)
    assert out["video_id"] == "v"
    # A clean chunk resets the failure count: two isolated blips back off
    # once each instead of accumulating.
    assert backoffs == [1, 1]


def test_resumable_gives_up_after_five_consecutive_failures(
        tmp_video, quota, monkeypatch):
    req = FakeRequest([_http_error(500)] * 8)
    svc = FakeService(req)
    backoffs: list = []
    _patch_upload_env(monkeypatch, svc, backoffs)
    with pytest.raises(UploadError, match="5 tries"):
        upload_short(tmp_video, "t", _quota=quota)
    assert req.calls == 5
    assert backoffs == [1, 2, 3, 4]
    assert quota.used_today() == 0  # failed uploads don't consume quota


def test_resumable_stall_guard(tmp_video, quota, monkeypatch):
    # Server never accepts a chunk and never errors: without the watchdog
    # this loops forever (a hang in a cron slot).
    req = FakeRequest([(None, None)] * 40, stuck_progress=True)
    svc = FakeService(req)
    backoffs: list = []
    _patch_upload_env(monkeypatch, svc, backoffs)
    with pytest.raises(UploadError, match="stalled"):
        upload_short(tmp_video, "t", _quota=quota)
    assert req.calls == youtube_api.STALL_LIMIT + 1
    assert backoffs == []


def test_411_proxy_workaround_still_clears_error_state(
        tmp_video, quota, monkeypatch):
    req = FakeRequest([_http_error(411), (None, {"id": "v"})])
    req._in_error_state = True
    svc = FakeService(req)
    backoffs: list = []
    _patch_upload_env(monkeypatch, svc, backoffs)
    out = upload_short(tmp_video, "t", _quota=quota)
    assert out["video_id"] == "v"
    assert req._in_error_state is False
    assert backoffs == [1]


# ---------------------------------------------------------------------------
# P1: server-side quota exhaustion (403 quotaExceeded) must degrade to the
# browser bundle, not raise — the module promises graceful degradation.
# ---------------------------------------------------------------------------

def test_server_quota_exceeded_becomes_quota_exceeded(
        tmp_video, quota, monkeypatch):
    req = FakeRequest([_http_error(403, "quotaExceeded")])
    svc = FakeService(req)
    backoffs: list = []
    _patch_upload_env(monkeypatch, svc, backoffs)
    with pytest.raises(QuotaExceeded):
        upload_short(tmp_video, "t", _quota=quota)
    assert req.calls == 1 and backoffs == []  # never retried
    assert quota.used_today() == 0


def test_server_quota_exhaustion_falls_back_to_browser(
        tmp_video, tmp_path, monkeypatch):
    # End-to-end: a 403 quotaExceeded from the API takes the same graceful
    # path as the local quota guard.
    req = FakeRequest([_http_error(403, "quotaExceeded")],
                       resumable_mode=False)
    svc = FakeService(req)
    backoffs: list = []
    _patch_upload_env(monkeypatch, svc, backoffs)
    monkeypatch.setattr(youtube_api, "_is_portrait", lambda p: True)
    monkeypatch.setattr(youtube_api, "QuotaTracker",
                        lambda *a, **k: QuotaTracker(tmp_path / "q.json"))
    monkeypatch.setattr(oauth, "is_connected", lambda: True)
    out = upload_clip({"file": tmp_video, "title": "T"}, mode="api")
    assert out["ok"] and out["method"] == "browser"
    assert "quota" in out["note"].lower()


def test_non_quota_403_stays_upload_error(tmp_video, quota, monkeypatch):
    req = FakeRequest([_http_error(403, "accessDenied")])
    svc = FakeService(req)
    backoffs: list = []
    _patch_upload_env(monkeypatch, svc, backoffs)
    with pytest.raises(UploadError, match="HTTP 403"):
        upload_short(tmp_video, "t", _quota=quota)
    assert req.calls == 1 and backoffs == []


def test_rate_limit_403_stays_upload_error(tmp_video, quota, monkeypatch):
    # rateLimitExceeded is transient throttling, not quota exhaustion:
    # an honest error, never a silent fallback.
    req = FakeRequest([_http_error(403, "rateLimitExceeded")])
    svc = FakeService(req)
    backoffs: list = []
    _patch_upload_env(monkeypatch, svc, backoffs)
    with pytest.raises(UploadError, match="HTTP 403"):
        upload_short(tmp_video, "t", _quota=quota)
    assert req.calls == 1 and backoffs == []


# ---------------------------------------------------------------------------
# P1: expired/revoked token in an unattended context must fail fast —
# never open a blocking browser consent flow on a headless machine.
# ---------------------------------------------------------------------------

def test_get_credentials_non_interactive_raises_instead_of_consent(
        monkeypatch):
    creds = FakeCreds(valid=False, expired=True, refresh_token="rt")
    monkeypatch.setattr(oauth, "_load_token", lambda: creds)
    monkeypatch.setattr(oauth, "_refresh", lambda c: False)  # revoked
    consent = []
    monkeypatch.setattr(oauth, "_run_consent_flow",
                        lambda cfg: consent.append(1))
    with pytest.raises(oauth.OAuthNotConfigured, match="reconnect"):
        oauth.get_credentials(interactive=False)
    assert consent == []


def test_get_credentials_interactive_still_consents_by_default(monkeypatch):
    creds = FakeCreds(valid=False, expired=True, refresh_token="rt")
    monkeypatch.setattr(oauth, "_load_token", lambda: creds)
    monkeypatch.setattr(oauth, "_refresh", lambda c: False)  # revoked
    fresh = FakeCreds(valid=True)
    monkeypatch.setattr(oauth, "_run_consent_flow", lambda cfg: fresh)
    monkeypatch.setattr(oauth, "_load_client_config", lambda: {"x": 1})
    assert oauth.get_credentials() is fresh  # default: interactive


def test_upload_short_non_interactive_surfaces_reauth(
        tmp_video, quota, monkeypatch):
    def _nope(*a, **k):
        raise oauth.OAuthNotConfigured("reconnect in the dashboard")

    monkeypatch.setattr(youtube_api.oauth_mod, "get_credentials", _nope)
    with pytest.raises(oauth.OAuthNotConfigured, match="reconnect"):
        upload_short(tmp_video, "t", _quota=quota, interactive=False)


def test_upload_clip_passes_interactive_through(tmp_video, monkeypatch):
    monkeypatch.setattr(oauth, "is_connected", lambda: True)
    seen = {}

    def fake_short(*a, **k):
        seen.update(k)
        return {"video_id": "v", "url": "https://youtu.be/v"}

    monkeypatch.setattr("core.upload.upload_short", fake_short)
    out = upload_clip({"file": tmp_video, "title": "T"}, mode="api",
                      interactive=False)
    assert out["method"] == "api"
    assert seen.get("interactive") is False


# ---------------------------------------------------------------------------
# P1: corrupt token file (valid JSON, wrong shape) must not crash.
# ---------------------------------------------------------------------------

def test_corrupt_token_file_treated_as_no_token(tmp_path, monkeypatch):
    p = tmp_path / "youtube_token.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")  # valid JSON, wrong shape
    monkeypatch.setattr(oauth, "token_path", lambda: p)
    assert oauth._load_token() is None
    assert oauth.is_connected() is False


# ---------------------------------------------------------------------------
# P1: portrait check must honor rotation metadata — ffprobe width/height
# are the *coded* dims; phone clips are often stored landscape + rotate=90.
# ---------------------------------------------------------------------------

def _mock_ffprobe(monkeypatch, payload):
    monkeypatch.setattr(youtube_api, "_ffprobe_path",
                        lambda: "/fake/ffprobe")
    monkeypatch.setattr("subprocess.run",
                        lambda *a, **k: _FakeCompletedProcess(payload))


def test_is_portrait_honors_rotate_tag(monkeypatch):
    _mock_ffprobe(monkeypatch,
                  _ffprobe_stream_json(1920, 1080, tags={"rotate": "90"}))
    assert youtube_api._is_portrait("/fake/clip.mp4") is True


def test_is_portrait_landscape_without_rotation(monkeypatch):
    _mock_ffprobe(monkeypatch, _ffprobe_stream_json(1920, 1080))
    assert youtube_api._is_portrait("/fake/clip.mp4") is False


def test_is_portrait_honors_display_matrix_side_data(monkeypatch):
    _mock_ffprobe(monkeypatch,
                  _ffprobe_stream_json(
                      1920, 1080,
                      side_data=[{"side_data_type": "Display Matrix",
                                  "rotation": -90}]))
    assert youtube_api._is_portrait("/fake/clip.mp4") is True


# ---------------------------------------------------------------------------
# P2 (fixed): tags list was passed to the API uncapped while a dead
# tags_str variable showed the 500-char cap was intended.
# ---------------------------------------------------------------------------

def test_tags_capped_at_500_chars(tmp_video, quota, monkeypatch):
    req = FakeRequest([(None, {"id": "v"})])
    svc = FakeService(req)
    backoffs: list = []
    _patch_upload_env(monkeypatch, svc, backoffs)
    upload_short(tmp_video, "t",
                 hashtags=["tag%02d" % i + "x" * 40 for i in range(30)],
                 _quota=quota)
    tags = svc.videos_obj.captured["body"]["snippet"]["tags"]
    assert len(",".join(tags)) <= 500


# ---------------------------------------------------------------------------
# P2 (fixed): a quota-bookkeeping failure must not turn a successful
# upload into a reported failure.
# ---------------------------------------------------------------------------

def test_quota_record_failure_does_not_fail_upload(
        tmp_video, quota, monkeypatch):
    req = FakeRequest([(None, {"id": "v"})])
    svc = FakeService(req)
    backoffs: list = []
    _patch_upload_env(monkeypatch, svc, backoffs)

    def _boom():
        raise OSError("disk full")

    monkeypatch.setattr(quota, "record_upload", _boom)
    out = upload_short(tmp_video, "t", _quota=quota)
    assert out == {"video_id": "v", "url": "https://youtu.be/v"}


# ---------------------------------------------------------------------------
# P2 (fixed): a multipart execute() that returns without a video id must
# NOT be retried — a retry could publish a duplicate video.
# ---------------------------------------------------------------------------

def test_multipart_response_without_id_never_retried(
        tmp_video, quota, monkeypatch):
    req = FakeRequest([{}], resumable_mode=False)
    svc = FakeService(req)
    backoffs: list = []
    _patch_upload_env(monkeypatch, svc, backoffs)
    with pytest.raises(UploadError, match="without returning a video id"):
        upload_short(tmp_video, "t", _quota=quota)
    assert req.calls == 1
    assert backoffs == []
