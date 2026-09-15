"""TikTok Content Posting API: upload shorts to the creator's inbox as drafts.

Unaudited apps can only Direct Post privately (and only to private accounts),
so shorts go to the TikTok inbox instead: a notification opens the draft in
the app, where you paste the caption ("Légende TikTok" in Notion) and publish.
The API can't set a caption in this mode. Works from a sandbox app, no review.

Limits: 6 init requests/min per token, at most 5 pending drafts per 24 h.
OAuth: desktop flow with PKCE (hex SHA-256 challenge); the access token lasts
24 h and the refresh token 1 year, rotated on every refresh.
"""
import hashlib
import http.server
import json
import secrets
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Dict, Optional

import requests

from .. import notify
from ..config import (
    TIKTOK_CLIENT_KEY,
    TIKTOK_CLIENT_SECRET,
    TIKTOK_REDIRECT_PORT,
    TIKTOK_TOKEN_FILE,
    TIKTOK_USERNAME,
)
from .base import Publisher, QuotaExceeded, ShortPost

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com/v2"
SCOPES = "user.info.basic,video.upload"
MAX_SINGLE_CHUNK_BYTES = 64 * 1024 * 1024  # up to this size the video goes up as one chunk of its exact size
CHUNK_BYTES = 10 * 1024 * 1024             # larger videos: 5-64 MB chunks, the last one absorbs the remainder
STATUS_POLL_SECONDS = 10            # status endpoint allows 30 requests/min
STATUS_TIMEOUT_SECONDS = 600
AUTH_TIMEOUT_SECONDS = 300


def _redirect_uri() -> str:
    return f"http://127.0.0.1:{TIKTOK_REDIRECT_PORT}/callback/"


def _require_app() -> None:
    if not (TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET):
        raise RuntimeError(
            "TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET are not set. Copy them from your app "
            "on https://developers.tiktok.com (sandbox credentials) into .env."
        )


def _token_request(fields: Dict[str, str]) -> Dict:
    resp = requests.post(
        f"{API}/oauth/token/",
        data={"client_key": TIKTOK_CLIENT_KEY, "client_secret": TIKTOK_CLIENT_SECRET, **fields},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    try:
        data = resp.json()
    except ValueError:
        data = {"error": resp.text[:300]}
    if resp.status_code != 200 or "access_token" not in data:
        raise RuntimeError(f"TikTok token request failed: {data.get('error_description') or data.get('error') or data}")
    return data


def _save_token(data: Dict, previous: Optional[Dict] = None) -> Dict:
    now = time.time()
    previous = previous or {}
    token = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token") or previous.get("refresh_token"),
        "open_id": data.get("open_id") or previous.get("open_id"),
        "scope": data.get("scope") or previous.get("scope"),
        "expires_at": now + int(data.get("expires_in", 86400)),
        "refresh_expires_at": (
            now + int(data["refresh_expires_in"]) if data.get("refresh_expires_in") else previous.get("refresh_expires_at", 0)
        ),
    }
    Path(TIKTOK_TOKEN_FILE).write_text(json.dumps(token, indent=2), encoding="utf-8")
    return token


def authorize() -> None:
    """One-time browser consent through a local callback server."""
    _require_app()
    verifier = secrets.token_urlsafe(72)[:96]  # 43-128 unreserved characters
    # TikTok's desktop PKCE expects the hex digest, not the RFC 7636 base64url one.
    challenge = hashlib.sha256(verifier.encode("ascii")).hexdigest()
    state = secrets.token_urlsafe(16)
    result: Dict[str, str] = {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path.rstrip("/") == "/callback":
                result.update({k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()})
            ok = "code" in result and result.get("state") == state
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            message = "TikTok autorisé, tu peux fermer cet onglet." if ok else "Autorisation TikTok échouée, regarde le terminal."
            self.wfile.write(f"<h2>{message}</h2>".encode("utf-8"))

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", TIKTOK_REDIRECT_PORT), Callback)
    server.timeout = 5
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_key": TIKTOK_CLIENT_KEY,
        "scope": SCOPES,
        "redirect_uri": _redirect_uri(),
        "state": state,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    print(f"Opening the TikTok consent page. If no browser opens, visit:\n{url}")
    webbrowser.open(url)
    deadline = time.time() + AUTH_TIMEOUT_SECONDS
    try:
        while "code" not in result and "error" not in result and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    if "code" not in result or result.get("state") != state:
        reason = result.get("error_description") or result.get("error") or "no authorisation code received"
        raise RuntimeError(f"TikTok authorisation failed: {reason}")
    token = _save_token(_token_request({
        "code": result["code"],
        "grant_type": "authorization_code",
        "redirect_uri": _redirect_uri(),
        "code_verifier": verifier,
    }))
    missing = set(SCOPES.split(",")) - set((token.get("scope") or "").split(","))
    if missing:
        print(f"Warning: scopes not granted: {', '.join(sorted(missing))} (enable them on the app, then re-run).")
    print(f"TikTok authorised, token saved to {TIKTOK_TOKEN_FILE}")


def _access_token() -> str:
    _require_app()
    path = Path(TIKTOK_TOKEN_FILE)
    if not path.exists():
        raise RuntimeError(f"{TIKTOK_TOKEN_FILE} not found. Run `python feed.py auth-tiktok` once.")
    token = json.loads(path.read_text(encoding="utf-8"))
    if time.time() < token["expires_at"] - 300:
        return token["access_token"]
    if time.time() >= token.get("refresh_expires_at", 0):
        raise RuntimeError("TikTok refresh token expired (1 year). Run `python feed.py auth-tiktok` again.")
    # The refresh token rotates: always keep the one TikTok hands back.
    refreshed = _token_request({"grant_type": "refresh_token", "refresh_token": token["refresh_token"]})
    return _save_token(refreshed, previous=token)["access_token"]


def caption(post: ShortPost) -> str:
    """What to paste in the TikTok app (mirrors the "Légende TikTok" Notion formula)."""
    return "\n\n".join(part for part in (post.title, post.description, " ".join(post.hashtags)) if part)[:2200]


class TikTokPublisher(Publisher):
    def __init__(self):
        # Refreshed up front so a dead token skips TikTok for the run instead of failing each short.
        self.token = _access_token()

    def _api(self, path: str, body: Dict) -> Dict:
        resp = requests.post(
            f"{API}/{path}",
            json=body,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json; charset=UTF-8"},
            timeout=60,
        )
        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError(f"TikTok {path} [{resp.status_code}]: {resp.text[:300]}")
        error = data.get("error") or {}
        code = error.get("code") or "ok"
        if code == "ok" and resp.status_code < 400:
            return data.get("data") or {}
        if resp.status_code == 429 or code == "rate_limit_exceeded" or code.startswith("spam_risk"):
            # Includes the 5-pending-drafts cap: publish the drafts waiting in the app.
            raise QuotaExceeded(f"TikTok {code}: {error.get('message')}")
        raise RuntimeError(f"TikTok {path} failed [{resp.status_code} {code}]: {error.get('message')}")

    def publish(self, post: ShortPost) -> str:
        size = post.file.stat().st_size
        # A single chunk must declare chunk_size == video_size: an 11.7 MB file sent as
        # "10 MB x 1 chunk" is rejected with "The chunk size is invalid".
        chunk = size if size <= MAX_SINGLE_CHUNK_BYTES else CHUNK_BYTES
        count = size // chunk  # rounded down: the last chunk carries the remainder
        init = self._api("post/publish/inbox/video/init/", {
            "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count},
        })
        publish_id, upload_url = init["publish_id"], init["upload_url"]

        with open(post.file, "rb") as f:
            for i in range(count):
                first = i * chunk
                last = size - 1 if i == count - 1 else first + chunk - 1
                f.seek(first)
                body = f.read(last - first + 1)
                resp = requests.put(
                    upload_url,
                    data=body,
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Length": str(len(body)),
                        "Content-Range": f"bytes {first}-{last}/{size}",
                    },
                    timeout=300,
                )
                if resp.status_code not in (200, 201, 206):
                    raise RuntimeError(f"TikTok chunk {i + 1}/{count} upload failed [{resp.status_code}]: {resp.text[:300]}")

        deadline = time.time() + STATUS_TIMEOUT_SECONDS
        while time.time() < deadline:
            time.sleep(STATUS_POLL_SECONDS)
            status = self._api("post/publish/status/fetch/", {"publish_id": publish_id})
            state = status.get("status")
            if state in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
                text = caption(post)
                print(f"[publish/tiktok]   draft sent to the TikTok inbox. Caption to paste:\n{text}", flush=True)
                # The API can't pre-fill the caption, so push it to the phone, ready to copy.
                notify.send(
                    f"Brouillon TikTok prêt : {post.title}"[:200], text,
                    copy=text, copy_label="Copier la légende",
                    open_url="https://www.tiktok.com/", open_label="Ouvrir TikTok",
                )
                return f"https://www.tiktok.com/@{TIKTOK_USERNAME}" if TIKTOK_USERNAME else "https://www.tiktok.com/"
            if state == "FAILED":
                reason = str(status.get("fail_reason") or "unknown")
                if reason.startswith("spam_risk"):
                    raise QuotaExceeded(f"TikTok {reason}")
                raise RuntimeError(f"TikTok processing failed: {reason}")
        raise RuntimeError(f"TikTok still processing after {STATUS_TIMEOUT_SECONDS}s (publish_id {publish_id})")
