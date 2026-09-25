"""YouTube Data API OAuth for ClipForge2 (installed/desktop-app flow).

One-time consent in the user's own browser; afterwards the refresh token
is used silently. Secrets live in ~/.clipforge2/ (never in code, logs,
or the dashboard config) and the token file is chmod 600.

Adapted from the yt-automation audit: minimal youtube.upload scope,
InstalledAppFlow on explicit 127.0.0.1 (some browsers resolve
"localhost" to IPv6 first), refresh-when-expired, else full flow.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from core.config import config_dir

log = logging.getLogger("clipforge2.upload.oauth")

YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"

# Standard public Google OAuth endpoints (not secrets).
_GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
_GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"

CLIENT_ID_ENV = "CF2_YT_CLIENT_ID"
CLIENT_SECRET_ENV = "CF2_YT_CLIENT_SECRET"
CLIENT_FILE = "youtube_client.json"   # downloaded from Google Cloud Console
TOKEN_FILE = "youtube_token.json"


def client_file_path() -> Path:
    return config_dir() / CLIENT_FILE


def token_path() -> Path:
    return config_dir() / TOKEN_FILE


def _client_config_from_env() -> dict | None:
    """Build an installed-app client config from env vars (no file needed)."""
    cid = os.environ.get(CLIENT_ID_ENV, "").strip()
    secret = os.environ.get(CLIENT_SECRET_ENV, "").strip()
    if not cid or not secret:
        return None
    return {
        "installed": {
            "client_id": cid,
            "client_secret": secret,
            "redirect_uris": ["http://127.0.0.1"],
            "auth_uri": _GOOGLE_AUTH_URI,
            "token_uri": _GOOGLE_TOKEN_URI,
        }
    }


def has_client_config() -> bool:
    """True when we have *something* to start OAuth with (env or file)."""
    if _client_config_from_env() is not None:
        return True
    p = client_file_path()
    return p.is_file() and p.stat().st_size > 0


def _load_client_config() -> dict:
    cfg = _client_config_from_env()
    if cfg is not None:
        return cfg
    p = client_file_path()
    if not p.is_file():
        raise OAuthNotConfigured(
            "No YouTube client configured yet — follow docs/YOUTUBE_SETUP.md "
            "to create the OAuth client, then try again.")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise OAuthNotConfigured(
            f"Couldn't read {p}: {e}. Re-download the client JSON "
            "from Google Cloud Console.") from e


class OAuthNotConfigured(Exception):
    """Raised when the user hasn't set up the Google Cloud OAuth client yet."""


def _save_token(creds) -> None:
    p = token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Never log the token contents.
    p.write_text(creds.to_json(), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _load_token():
    """Return stored Credentials or None (never launches a browser)."""
    from google.oauth2.credentials import Credentials

    p = token_path()
    if not p.is_file():
        return None
    try:
        return Credentials.from_authorized_user_file(
            str(p), scopes=[YOUTUBE_UPLOAD_SCOPE])
    except (OSError, ValueError) as e:
        log.warning("stored YouTube token unreadable: %s", e)
        return None


def _refresh(creds) -> bool:
    """Try a silent refresh. True on success."""
    from google.auth.transport.requests import Request

    try:
        creds.refresh(Request())
    except Exception as e:  # noqa: BLE001 — any refresh failure -> re-consent
        log.info("YouTube token refresh failed: %s", e)
        return False
    _save_token(creds)
    return True


def _run_consent_flow(client_config: dict):
    """First-time browser consent (blocking — call from a thread)."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_config(
        client_config, scopes=[YOUTUBE_UPLOAD_SCOPE])
    # Explicit 127.0.0.1 (audit lesson): some browsers resolve
    # "localhost" to IPv6 first and the callback then fails.
    creds = flow.run_local_server(host="127.0.0.1", port=0,
                                  timeout_seconds=300,
                                  open_browser=True)
    _save_token(creds)
    return creds


def get_credentials():
    """Return valid YouTube credentials.

    Order: stored token (refresh silently if expired) -> first-time
    browser consent flow. Raises OAuthNotConfigured when the user
    hasn't created the Google Cloud client yet.
    """
    creds = _load_token()
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        if _refresh(creds):
            return creds
        # Refresh failed (revoked/expired) — fall through to re-consent.
    return _run_consent_flow(_load_client_config())


def is_connected() -> bool:
    """True when a usable token exists — never opens a browser."""
    creds = _load_token()
    if creds is None:
        return False
    if creds.valid:
        return True
    # Expired but refreshable counts as connected (next API call
    # refreshes silently); a revoked token will fail then and the
    # user re-connects.
    return bool(creds.expired and creds.refresh_token)


def disconnect() -> bool:
    """Forget the stored token. Returns True if one existed."""
    p = token_path()
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False
