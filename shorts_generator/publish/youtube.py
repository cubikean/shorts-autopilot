"""YouTube Data API v3 upload: OAuth installed-app flow + resumable upload.

Quota: videos.insert costs 1600 of the default 10 000 units a day, i.e. about
6 uploads a day per Google Cloud project.
Pacing: releases are spaced by YOUTUBE_MIN_GAP_MINUTES; uploads that come in
sooner are scheduled (private + publishAt) instead of going public at once.
"""
import json
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from ..config import (
    LOCAL_OUTPUT_DIR,
    YOUTUBE_CATEGORY_ID,
    YOUTUBE_CLIENT_SECRETS,
    YOUTUBE_MIN_GAP_MINUTES,
    YOUTUBE_PRIVACY,
    YOUTUBE_TOKEN_FILE,
)
from .base import PostStats, Publisher, QuotaExceeded, ShortPost

SCHEDULE_FILE = Path(LOCAL_OUTPUT_DIR) / "youtube_schedule.json"
READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"  # daily stats
SCOPES = ["https://www.googleapis.com/auth/youtube.upload", READONLY_SCOPE]
RETRY_STATUSES = {500, 502, 503, 504}
QUOTA_REASONS = {"quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded", "rateLimitExceeded"}
MAX_RETRIES = 5


def authorize() -> None:
    """One-time browser consent; stores a refresh token for unattended runs."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not Path(YOUTUBE_CLIENT_SECRETS).exists():
        raise RuntimeError(
            f"{YOUTUBE_CLIENT_SECRETS} not found. Download the OAuth client (type Desktop app) "
            "JSON from Google Cloud Console → APIs & Services → Credentials."
        )
    flow = InstalledAppFlow.from_client_secrets_file(YOUTUBE_CLIENT_SECRETS, SCOPES)
    # offline + consent guarantees a refresh token even if the app was authorised before.
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    Path(YOUTUBE_TOKEN_FILE).write_text(creds.to_json(), encoding="utf-8")
    print(f"YouTube authorised, token saved to {YOUTUBE_TOKEN_FILE}")


def _credentials():
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path = Path(YOUTUBE_TOKEN_FILE)
    if not path.exists():
        raise RuntimeError(f"{YOUTUBE_TOKEN_FILE} not found. Run `python feed.py auth-youtube` once.")
    # Keep the scopes the token was granted: forcing new ones would make the refresh fail
    # (invalid_scope) and break uploads until auth-youtube is run again.
    creds = Credentials.from_authorized_user_file(str(path))
    if not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            # Consent screen left in "Testing" mode: refresh tokens die after 7 days.
            raise RuntimeError(f"YouTube token expired or revoked ({e}). Run `python feed.py auth-youtube` again.") from e
        path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _release_slot(requested: Optional[datetime]) -> datetime:
    """When the next short goes public: never sooner than YOUTUBE_MIN_GAP_MINUTES after
    the previous release, so a batch of uploads comes out one by one instead of all at once."""
    now = datetime.now(timezone.utc)
    candidates = [now]
    if requested:
        candidates.append(requested.astimezone(timezone.utc))
    try:
        last = datetime.fromisoformat(json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))["last_release"])
        candidates.append(last + timedelta(minutes=YOUTUBE_MIN_GAP_MINUTES))
    except (OSError, ValueError, KeyError):
        pass
    return max(candidates)


def _remember_release(release: datetime) -> None:
    # Only one publish run at a time (feed.py lock), so this file never races.
    SCHEDULE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SCHEDULE_FILE.write_text(json.dumps({"last_release": release.isoformat()}), encoding="utf-8")


def _clean(text: str) -> str:
    # YouTube rejects titles and descriptions containing angle brackets.
    return text.replace("<", "‹").replace(">", "›").strip()


def _truncate_bytes(text: str, limit: int) -> str:
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _tags(hashtags: List[str]) -> List[str]:
    tags, total = [], 0
    for tag in (h.lstrip("#") for h in hashtags):
        if tag and total + len(tag) + 1 <= 500:  # 500-character budget for all tags
            tags.append(tag)
            total += len(tag) + 1
    return tags


def _count(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None  # hidden like counts are simply absent


def _reason(error) -> str:
    try:
        return json.loads(error.content)["error"]["errors"][0]["reason"]
    except Exception:
        return ""


class YouTubePublisher(Publisher):
    def __init__(self):
        from googleapiclient.discovery import build

        # Built up front so an auth problem aborts the run instead of failing every row.
        self.creds = _credentials()
        self.client = build("youtube", "v3", credentials=self.creds, cache_discovery=False)

    def fetch_stats(self, items):
        if not self.creds.has_scopes([READONLY_SCOPE]):
            raise RuntimeError("reading YouTube stats needs one more permission: run `python feed.py auth-youtube` again")
        page_of: Dict[str, str] = {}
        for page_id, link, _post in items:
            page_of[link.rstrip("/").split("/")[-1].split("?")[0]] = page_id  # youtube.com/shorts/<id>
        ids = list(page_of)
        stats: Dict[str, PostStats] = {}
        for i in range(0, len(ids), 50):  # 1 quota unit per call, 50 ids max
            response = self.client.videos().list(part="statistics", id=",".join(ids[i:i + 50])).execute()
            for video in response.get("items", []):
                numbers = video.get("statistics", {})
                stats[page_of[video["id"]]] = PostStats(
                    views=_count(numbers.get("viewCount")),
                    likes=_count(numbers.get("likeCount")),
                    comments=_count(numbers.get("commentCount")),
                )
        return stats

    def publish(self, post: ShortPost) -> str:
        import httplib2
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        title = _clean(post.title or post.hook or "Short")[:100]
        parts = [f"« {post.hook} »" if post.hook else "", post.description, " ".join(post.hashtags)]
        description = _truncate_bytes(_clean("\n\n".join(p for p in parts if p)), 5000)

        status = {"privacyStatus": YOUTUBE_PRIVACY, "selfDeclaredMadeForKids": False}
        release = None
        if YOUTUBE_PRIVACY == "public":
            release = _release_slot(post.publish_at)
            if release > datetime.now(timezone.utc) + timedelta(minutes=2):
                # Scheduled release: YouTube requires the video to be private until publishAt.
                status.update(privacyStatus="private", publishAt=release.strftime("%Y-%m-%dT%H:%M:%SZ"))

        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": _tags(post.hashtags),
                "categoryId": YOUTUBE_CATEGORY_ID,
            },
            "status": status,
        }
        media = MediaFileUpload(str(post.file), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
        request = self.client.videos().insert(part="snippet,status", body=body, media_body=media)

        response, attempt = None, 0
        while response is None:
            try:
                progress, response = request.next_chunk()
            except HttpError as e:
                reason = _reason(e)
                if reason in QUOTA_REASONS:
                    raise QuotaExceeded(f"YouTube: {reason}") from e
                if e.resp.status not in RETRY_STATUSES:
                    raise RuntimeError(f"YouTube upload failed [{e.resp.status} {reason}]: {e}") from e
                error = e
            except (OSError, httplib2.HttpLib2Error) as e:
                error = e
            else:
                if progress:
                    print(f"[publish/youtube]   {progress.progress():.0%}", flush=True)
                continue
            attempt += 1
            if attempt > MAX_RETRIES:
                raise RuntimeError(f"YouTube upload failed after {MAX_RETRIES} retries: {error}")
            time.sleep(min(60, 2 ** attempt) + random.random())

        if release:
            _remember_release(release)
            if "publishAt" in status:
                local = release.astimezone().strftime("%d/%m %H:%M")
                print(f"[publish/youtube]   uploaded private, goes public on {local}", flush=True)
        return f"https://youtube.com/shorts/{response['id']}"
