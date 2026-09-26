"""Tests for core/upload — YouTube OAuth, resumable upload, quota guard.

Everything Google is mocked: no network, no real credentials, no
browser. Quota math, retry behavior, token-refresh path, manual bundle
and validation rejections are all exercised against fakes.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.upload import browser_upload, oauth
from core.upload import upload_clip
from core.upload.youtube_api import (MAX_UPLOADS_PER_DAY, QuotaExceeded,
                                     QuotaTracker, UploadError, upload_short)
from core.upload import youtube_api


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

def _http_error(status: int):
    from googleapiclient.errors import HttpError

    return HttpError(resp=SimpleNamespace(status=status, reason="fake"),
                     content=b"fake-error")


class FakeRequest:
    """Scripted next_chunk(): items are (status, response) or exceptions."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def next_chunk(self):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


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


def _patch_upload_env(monkeypatch, service, quota_obj, backoffs):
    monkeypatch.setattr(youtube_api, "_build_service",
                        lambda creds: service)
    monkeypatch.setattr(youtube_api.oauth_mod, "get_credentials",
                        lambda: FakeCreds())
    monkeypatch.setattr(youtube_api, "_backoff",
                        lambda attempt: backoffs.append(attempt))


# ---------------------------------------------------------------------------
# quota guard
# ---------------------------------------------------------------------------

def test_quota_fresh_allows(quota):
    assert quota.used_today() == 0
    assert quota.remaining() == MAX_UPLOADS_PER_DAY == 100
    assert quota.can_upload()
    quota.check_or_raise()  # no raise


def test_quota_blocks_after_limit(quota):
    for _ in range(MAX_UPLOADS_PER_DAY):
        quota.record_upload()
    assert quota.used_today() == MAX_UPLOADS_PER_DAY
    assert quota.remaining() == 0
    assert not quota.can_upload()
    with pytest.raises(QuotaExceeded) as e:
        quota.check_or_raise()
    msg = str(e.value)
    assert "100/100" in msg and "midnight Pacific" in msg


def test_quota_resets_on_new_pacific_day(quota, monkeypatch):
    for _ in range(MAX_UPLOADS_PER_DAY):
        quota.record_upload()
    assert not quota.can_upload()
    monkeypatch.setattr(youtube_api, "_pacific_today",
                        lambda: "2099-01-02")
    assert quota.used_today() == 0
    assert quota.can_upload()


def test_quota_corrupt_file_tolerated(tmp_path):
    p = tmp_path / "quota.json"
    p.write_text("not json{{{", encoding="utf-8")
    q = QuotaTracker(p)
    assert q.used_today() == 0 and q.can_upload()


# ---------------------------------------------------------------------------
# upload: success + retries
# ---------------------------------------------------------------------------

def test_upload_success_returns_id_and_url(tmp_video, quota, monkeypatch):
    req = FakeRequest([(None, {"id": "vid123"})])
    svc = FakeService(req)
    backoffs: list = []
    _patch_upload_env(monkeypatch, svc, quota, backoffs)
    out = upload_short(tmp_video, "My title", "desc", ["Gaming", "shorts"],
                       _quota=quota)
    assert out == {"video_id": "vid123", "url": "https://youtu.be/vid123"}
    assert quota.used_today() == 1
    assert backoffs == []
    body = svc.videos_obj.captured["body"]
    assert body["snippet"]["title"] == "My title"
    assert body["status"]["privacyStatus"] == "public"


def test_upload_retries_500_then_succeeds(tmp_video, quota, monkeypatch):
    req = FakeRequest([_http_error(500), _http_error(503),
                       (None, {"id": "vid9"})])
    svc = FakeService(req)
    backoffs: list = []
    _patch_upload_env(monkeypatch, svc, quota, backoffs)
    out = upload_short(tmp_video, "t", _quota=quota)
    assert out["video_id"] == "vid9"
    assert req.calls == 3
    assert backoffs == [1, 2]  # linear backoff attempts*5 (scaled in test)


def test_upload_4xx_never_retried(tmp_video, quota, monkeypatch):
    req = FakeRequest([_http_error(403)])
    svc = FakeService(req)
    backoffs: list = []
    _patch_upload_env(monkeypatch, svc, quota, backoffs)
    with pytest.raises(UploadError, match="HTTP 403"):
        upload_short(tmp_video, "t", _quota=quota)
    assert req.calls == 1 and backoffs == []
    assert quota.used_today() == 0  # failed uploads don't consume quota


def test_upload_gives_up_after_max_attempts(tmp_video, quota, monkeypatch):
    req = FakeRequest([_http_error(500)] * 10)
    svc = FakeService(req)
    backoffs: list = []
    _patch_upload_env(monkeypatch, svc, quota, backoffs)
    with pytest.raises(UploadError, match="5 tries"):
        upload_short(tmp_video, "t", _quota=quota)
    assert req.calls == 5 and backoffs == [1, 2, 3, 4]


def test_upload_title_truncated_to_100(tmp_video, quota, monkeypatch):
    req = FakeRequest([(None, {"id": "v"})])
    svc = FakeService(req)
    backoffs: list = []
    _patch_upload_env(monkeypatch, svc, quota, backoffs)
    upload_short(tmp_video, "x" * 150, _quota=quota)
    assert len(svc.videos_obj.captured["body"]["snippet"]["title"]) == 100


def test_upload_validation_rejections(quota):
    with pytest.raises(UploadError, match="not found"):
        upload_short("/nope/missing.mp4", "t", _quota=quota)
    with pytest.raises(UploadError, match="privacy"):
        upload_short(__file__, "t", privacy="everyone", _quota=quota)


def test_upload_blocked_by_quota_never_hits_api(tmp_video, quota,
                                                monkeypatch):
    for _ in range(MAX_UPLOADS_PER_DAY):
        quota.record_upload()
    called = []
    monkeypatch.setattr(youtube_api, "_build_service",
                        lambda creds: called.append(1))
    with pytest.raises(QuotaExceeded):
        upload_short(tmp_video, "t", _quota=quota)
    assert called == []


def test_upload_non_portrait_rejected(tmp_video, quota, monkeypatch):
    monkeypatch.setattr(youtube_api, "_is_portrait", lambda p: False)
    with pytest.raises(UploadError, match="isn't vertical"):
        upload_short(tmp_video, "t", _quota=quota)


# ---------------------------------------------------------------------------
# oauth
# ---------------------------------------------------------------------------

def test_oauth_env_client_config(monkeypatch):
    monkeypatch.setenv("CF_YT_CLIENT_ID", "id-123")
    monkeypatch.setenv("CF_YT_CLIENT_SECRET", "shh")
    assert oauth.has_client_config()
    cfg = oauth._load_client_config()
    assert cfg["installed"]["client_id"] == "id-123"
    assert cfg["installed"]["client_secret"] == "shh"


def test_oauth_missing_client_raises_plain(monkeypatch, tmp_path):
    monkeypatch.delenv("CF_YT_CLIENT_ID", raising=False)
    monkeypatch.delenv("CF_YT_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(oauth, "client_file_path",
                        lambda: tmp_path / "nope.json")
    assert not oauth.has_client_config()
    with pytest.raises(oauth.OAuthNotConfigured, match="YOUTUBE_SETUP"):
        oauth._load_client_config()


def test_is_connected_true_when_valid(monkeypatch):
    monkeypatch.setattr(oauth, "_load_token",
                        lambda: FakeCreds(valid=True))
    assert oauth.is_connected()


def test_is_connected_true_when_refreshable(monkeypatch):
    monkeypatch.setattr(oauth, "_load_token",
                        lambda: FakeCreds(valid=False, expired=True,
                                          refresh_token="rt"))
    assert oauth.is_connected()


def test_is_connected_false_without_token(monkeypatch):
    monkeypatch.setattr(oauth, "_load_token", lambda: None)
    assert not oauth.is_connected()


def test_get_credentials_refreshes_silently(monkeypatch):
    creds = FakeCreds(valid=False, expired=True, refresh_token="rt")
    monkeypatch.setattr(oauth, "_load_token", lambda: creds)
    refreshed = []
    monkeypatch.setattr(oauth, "_refresh",
                        lambda c: refreshed.append(c) or True)
    flow_called = []
    monkeypatch.setattr(oauth, "_run_consent_flow",
                        lambda cfg: flow_called.append(1))
    assert oauth.get_credentials() is creds
    assert refreshed == [creds] and flow_called == []


def test_get_credentials_falls_back_to_consent(monkeypatch):
    creds = FakeCreds(valid=False, expired=True, refresh_token="rt")
    monkeypatch.setattr(oauth, "_load_token", lambda: creds)
    monkeypatch.setattr(oauth, "_refresh", lambda c: False)  # revoked
    fresh = FakeCreds(valid=True)
    monkeypatch.setattr(oauth, "_run_consent_flow", lambda cfg: fresh)
    monkeypatch.setattr(oauth, "_load_client_config", lambda: {"x": 1})
    assert oauth.get_credentials() is fresh


# ---------------------------------------------------------------------------
# browser fallback
# ---------------------------------------------------------------------------

def test_prepare_manual_upload_contents(tmp_video):
    clip = {"file": tmp_video, "title": "T", "description": "D",
            "hashtags": ["Gaming", "Shorts"]}
    m = browser_upload.prepare_manual_upload(clip)
    assert m["file"] == tmp_video
    assert m["title"] == "T"
    assert "#gaming" in m["description"] and "#shorts" in m["description"]
    assert len(m["steps"]) >= 5
    assert "studio.youtube.com" in " ".join(m["steps"])
    assert m["warnings"] == []
    txt = browser_upload.steps_text(m)
    assert "MANUAL UPLOAD" in txt and "T" in txt


def test_prepare_manual_upload_warns_on_missing_file():
    m = browser_upload.prepare_manual_upload(
        {"file": "/nope.mp4", "title": "T"})
    assert any("not found" in w for w in m["warnings"])


# ---------------------------------------------------------------------------
# upload_clip routing
# ---------------------------------------------------------------------------

def test_upload_clip_manual_mode():
    out = upload_clip({"file": "/tmp/a.mp4"}, mode="manual")
    assert out["ok"] and out["method"] == "manual"
    assert out["file"] == "/tmp/a.mp4"


def test_upload_clip_browser_mode(tmp_video):
    out = upload_clip({"file": tmp_video, "title": "T"}, mode="browser")
    assert out["ok"] and out["method"] == "browser"
    assert out["manual"]["file"] == tmp_video


def test_upload_clip_api_not_connected_falls_back(tmp_video, monkeypatch):
    monkeypatch.setattr(oauth, "is_connected", lambda: False)
    out = upload_clip({"file": tmp_video, "title": "T"}, mode="api")
    assert out["ok"] and out["method"] == "browser"
    assert "isn't connected" in out["note"]


def test_upload_clip_api_quota_falls_back(tmp_video, monkeypatch):
    monkeypatch.setattr(oauth, "is_connected", lambda: True)
    monkeypatch.setattr("core.upload.upload_short",
                        lambda *a, **k: (_ for _ in ()).throw(
                            QuotaExceeded("quota up")))
    out = upload_clip({"file": tmp_video, "title": "T"}, mode="api")
    assert out["ok"] and out["method"] == "browser"
    assert "quota up" in out["note"]


def test_upload_clip_api_success(tmp_video, monkeypatch):
    monkeypatch.setattr(oauth, "is_connected", lambda: True)
    monkeypatch.setattr("core.upload.upload_short",
                        lambda *a, **k: {"video_id": "v1",
                                         "url": "https://youtu.be/v1"})
    out = upload_clip({"file": tmp_video, "title": "T"}, mode="api")
    assert out == {"ok": True, "method": "api", "video_id": "v1",
                   "url": "https://youtu.be/v1"}


def test_upload_clip_unknown_mode():
    with pytest.raises(UploadError, match="unknown upload mode"):
        upload_clip({}, mode="carrier-pigeon")


def test_quota_status_reports_units(quota):
    s = quota.status()
    assert s["units_limit"] == 10000
    assert s["units_per_upload"] == 1600
    assert s["units_used"] == s["used"] * 1600
    # existing keys preserved
    assert s["limit"] == 100 and "remaining" in s and "resets" in s
