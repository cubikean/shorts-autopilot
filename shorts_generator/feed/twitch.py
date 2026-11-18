"""Twitch discovery: the community's most-viewed clips from the last FEED_MAX_AGE_HOURS.

Viewers already clipped the best moments, and each clip knows where it sits in
the VOD. Nearby clips are merged into one "moment", and only a window around
it gets downloaded later — no need to transcribe a 6-hour stream.

Score = the moment's clip views divided by the median of that channel's
moments today (1.0 when there is a single moment). Moments whose VOD is gone
are skipped; the VOD's length doesn't matter since only a window is downloaded.

Categories ("twitch_categories" in sources.json, e.g. a game launch) work the
other way round: the category's most viewed clips, whoever the streamer. The
clip itself is the source: it is always public, while the VODs of most big
French channels need a subscription.
"""
import re
import statistics
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

from ..config import (
    FEED_MAX_AGE_HOURS,
    FEED_TWITCH_MIN_VIEWS,
    FEED_TWITCH_MOMENTS_PER_CHANNEL,
    TWITCH_CLIENT_ID,
    TWITCH_CLIENT_SECRET,
)

HELIX = "https://api.twitch.tv/helix"
MERGE_GAP_SECONDS = 60     # clips closer than this belong to the same moment
WINDOW_PAD_SECONDS = 45    # context kept before/after the clipped moment


def _app_token() -> str:
    resp = requests.post(
        "https://id.twitch.tv/oauth2/token",
        params={"client_id": TWITCH_CLIENT_ID, "client_secret": TWITCH_CLIENT_SECRET, "grant_type": "client_credentials"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _helix(path: str, token: str, params) -> List[Dict]:
    resp = requests.get(
        f"{HELIX}/{path}",
        headers={"Client-Id": TWITCH_CLIENT_ID, "Authorization": f"Bearer {token}"},
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def _duration_minutes(value: str) -> float:
    """Helix durations look like "3h8m33s", "45m10s" or "30s"."""
    parts = dict((unit, float(num)) for num, unit in re.findall(r"(\d+)([hms])", value or ""))
    return parts.get("h", 0) * 60 + parts.get("m", 0) + parts.get("s", 0) / 60


def _vod_minutes(token: str, vod_ids: List[str]) -> Dict[str, float]:
    """VOD id → length in minutes; expired or deleted VODs are simply absent."""
    minutes: Dict[str, float] = {}
    ids = sorted(set(vod_ids))
    for i in range(0, len(ids), 100):
        for video in _helix("videos", token, [("id", vid) for vid in ids[i:i + 100]]):
            minutes[video["id"]] = _duration_minutes(video.get("duration", ""))
    return minutes


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _moments(clips: List[Dict]) -> List[Dict]:
    """Merge clips that overlap in the same VOD into moments."""
    usable = sorted(
        (c for c in clips if c.get("video_id") and c.get("vod_offset") is not None),
        key=lambda c: (c["video_id"], c["vod_offset"]),
    )
    moments: List[Dict] = []
    for clip in usable:
        start = float(clip["vod_offset"])
        end = start + float(clip.get("duration") or 30)
        last = moments[-1] if moments else None
        if last and last["vod_id"] == clip["video_id"] and start <= last["end"] + MERGE_GAP_SECONDS:
            last["end"] = max(last["end"], end)
            last["views"] += clip["view_count"]
            if clip["view_count"] > last["top_views"]:
                last["title"], last["top_views"], last["top_url"] = clip["title"], clip["view_count"], clip["url"]
            continue
        moments.append({
            "vod_id": clip["video_id"], "start": start, "end": end,
            "views": clip["view_count"], "top_views": clip["view_count"], "top_url": clip["url"],
            "title": clip["title"], "created_at": clip["created_at"],
            "channel": clip["broadcaster_name"],
        })
    return moments


def _category_candidates(token: str, category: Dict, now: datetime) -> List[Dict]:
    """The most viewed clips of one category, {"name": ..., "top": 10, "language": "fr" (optional)}.

    Clips of the same moment (several viewers clipping one action) are merged
    so it yields one short; clips without a VOD stand alone.
    """
    games = _helix("games", token, {"name": category["name"]})
    if not games:
        print(f"[feed/twitch] category {category['name']!r} not found on Twitch", flush=True)
        return []
    clips = _helix("clips", token, {
        "game_id": games[0]["id"],
        "started_at": _rfc3339(now - timedelta(hours=FEED_MAX_AGE_HOURS)),
        "ended_at": _rfc3339(now),
        "first": 100,
    })
    if category.get("language"):
        clips = [c for c in clips if c.get("language") == category["language"]]
    moments = _moments(clips) + [
        {"clip_id": c["id"], "views": c["view_count"], "top_url": c["url"], "title": c["title"],
         "created_at": c["created_at"], "channel": c["broadcaster_name"]}
        for c in clips if not c.get("video_id") or c.get("vod_offset") is None
    ]
    moments = [m for m in moments if m["views"] >= FEED_TWITCH_MIN_VIEWS]
    print(f"[feed/twitch] {category['name']}: {len(clips)} clips → {len(moments)} moments", flush=True)
    if not moments:
        return []
    baseline = statistics.median(m["views"] for m in moments)
    return [{
        # Same key as a whitelisted channel's moment, so the two never queue it twice.
        "key": f"tw:{m['vod_id']}:{int(m['start'] // 120)}" if "vod_id" in m else f"twclip:{m['clip_id']}",
        "platform": "Twitch",
        "title": m["title"],
        "url": m["top_url"],
        "channel": m["channel"],
        "published": datetime.fromisoformat(m["created_at"].replace("Z", "+00:00")),
        "views": int(m["views"]),
        "score": round(m["views"] / baseline, 2),
        "start": None,   # the whole clip is the source
        "end": None,
    } for m in sorted(moments, key=lambda m: -m["views"])[:int(category.get("top", 10))]]


def discover_twitch(sources: List[Dict], now: Optional[datetime] = None,
                    categories: Optional[List[Dict]] = None) -> List[Dict]:
    if not (TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET):
        print("[feed/twitch] TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET not set; skipping Twitch", flush=True)
        return []
    now = now or datetime.now(timezone.utc)
    token = _app_token()
    logins = [str(s["login"]).lower() for s in sources if s.get("login")]
    # Helix rejects a users lookup without any login (sources with only categories).
    users = _helix("users", token, [("login", login) for login in logins[:100]]) if logins else []

    candidates: List[Dict] = []
    for user in users:
        clips = _helix("clips", token, {
            "broadcaster_id": user["id"],
            "started_at": _rfc3339(now - timedelta(hours=FEED_MAX_AGE_HOURS)),
            "ended_at": _rfc3339(now),
            "first": 100,
        })
        moments = [m for m in _moments(clips) if m["views"] >= FEED_TWITCH_MIN_VIEWS]
        vod_minutes = _vod_minutes(token, [m["vod_id"] for m in moments]) if moments else {}
        kept = []
        for m in moments:
            if m["vod_id"] not in vod_minutes:
                print(f"[feed/twitch]   skip {m['title'][:60]!r}: VOD {m['vod_id']} unavailable", flush=True)
            else:
                kept.append(m)
        moments = kept
        if not moments:
            print(f"[feed/twitch] {user['login']}: no usable clipped VOD moment in the last {FEED_MAX_AGE_HOURS:g}h", flush=True)
            continue
        baseline = statistics.median(m["views"] for m in moments)
        for m in sorted(moments, key=lambda m: -m["views"])[:FEED_TWITCH_MOMENTS_PER_CHANNEL]:
            start = max(0.0, m["start"] - WINDOW_PAD_SECONDS)
            candidates.append({
                # Bucket by 2 minutes so tomorrow's extra clips of the same moment don't re-queue it.
                "key": f"tw:{m['vod_id']}:{int(m['start'] // 120)}",
                "platform": "Twitch",
                "title": m["title"],
                "url": f"https://www.twitch.tv/videos/{m['vod_id']}",
                "channel": m["channel"],
                "published": datetime.fromisoformat(m["created_at"].replace("Z", "+00:00")),
                "views": int(m["views"]),
                "score": round(m["views"] / baseline, 2),
                "start": round(start),
                "end": round(m["end"] + WINDOW_PAD_SECONDS),
            })
        print(f"[feed/twitch] {user['login']}: {len(clips)} clips → {len(moments)} moments", flush=True)
    for category in categories or []:
        try:
            candidates += _category_candidates(token, category, now)
        except Exception as e:
            print(f"[feed/twitch] category {category.get('name')!r} skipped: {e}", flush=True)
    return candidates
