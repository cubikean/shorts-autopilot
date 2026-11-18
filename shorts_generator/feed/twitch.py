"""Twitch discovery: the community's most-viewed clips from the last FEED_MAX_AGE_HOURS.

Viewers already clipped the best moments, and each clip knows where it sits in
the VOD. Nearby clips are merged into one "moment", and only a window around
it gets downloaded later — no need to transcribe a 6-hour stream.

Score = the moment's clip views divided by the median of that channel's
moments today (1.0 when there is a single moment). Moments whose VOD is gone
are skipped; the VOD's length doesn't matter since only a window is downloaded.
When the VOD is subscriber-only (most big French channels), the moment's most
viewed clip is used instead: clips stay public.

Besides the whitelist, sources.json can target Twitch categories ("twitch_categories"):
the biggest live channels in them right now are watched too. Clips made during a
live already point into the VOD being recorded, so a moment can be cut while the
stream is still running.
"""
import json
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

from ..config import (
    FEED_MAX_AGE_HOURS,
    LOCAL_OUTPUT_DIR,
    FEED_TWITCH_MIN_VIEWS,
    FEED_TWITCH_MOMENTS_PER_CHANNEL,
    TWITCH_CLIENT_ID,
    TWITCH_CLIENT_SECRET,
)

HELIX = "https://api.twitch.tv/helix"
MERGE_GAP_SECONDS = 60     # clips closer than this belong to the same moment
WINDOW_PAD_SECONDS = 45    # context kept before/after the clipped moment
# Channels found live in a targeted category, with when they were last seen there:
# a stream that just ended keeps its fresh moments for FEED_MAX_AGE_HOURS.
LIVE_SEEN_FILE = Path(LOCAL_OUTPUT_DIR) / "feed_twitch_live.json"


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


def _vod_locked(vod_url: str, cache: Dict[str, bool]) -> bool:
    """True when yt-dlp can't read the VOD without a subscriber login.

    Helix still reports these VODs as "public", so only a real request tells.
    Any other error counts as readable: the download step reports it properly.
    """
    if vod_url not in cache:
        import yt_dlp  # type: ignore

        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
                ydl.extract_info(vod_url, download=False, process=False)
            cache[vod_url] = False
        except Exception as e:
            cache[vod_url] = "subscriber-only" in str(e)
    return cache[vod_url]


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


def _top_live(token: str, categories: List[Dict], now: datetime) -> List[str]:
    """Logins of the most-watched live channels in each targeted category.

    Each category is {"name": "Grand Theft Auto VI", "language": "fr", "top": 10}.
    Viewer count is what ranks them: the biggest audiences are the ones whose
    moments get searched and reposted. Best effort: a failure targets nobody new.
    """
    try:
        seen = json.loads(LIVE_SEEN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = {}
    try:
        names = [c["name"] for c in categories if c.get("name")]
        games = {g["name"].lower(): g["id"] for g in _helix("games", token, [("name", n) for n in names])}
        for category in categories:
            game_id = games.get(str(category.get("name", "")).lower())
            if not game_id:
                print(f"[feed/twitch] category {category.get('name')!r} not found on Twitch", flush=True)
                continue
            params = [("game_id", game_id), ("first", str(min(100, int(category.get("top", 10)))))]
            if category.get("language"):
                params.append(("language", category["language"]))
            streams = _helix("streams", token, params)  # Helix returns them by viewer count, biggest first
            print(f"[feed/twitch] live in {category['name']}: "
                  + ", ".join(f"{s['user_login']} ({s['viewer_count']})" for s in streams), flush=True)
            for stream in streams:
                seen[stream["user_login"].lower()] = _rfc3339(now)
    except Exception as e:
        print(f"[feed/twitch] category lookup failed: {e}", flush=True)
    cutoff = now - timedelta(hours=FEED_MAX_AGE_HOURS)
    seen = {login: when for login, when in seen.items()
            if datetime.fromisoformat(when.replace("Z", "+00:00")) >= cutoff}
    LIVE_SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    LIVE_SEEN_FILE.write_text(json.dumps(seen, indent=1), encoding="utf-8")
    return list(seen)


def discover_twitch(sources: List[Dict], now: Optional[datetime] = None,
                    categories: Optional[List[Dict]] = None) -> List[Dict]:
    if not (TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET):
        print("[feed/twitch] TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET not set; skipping Twitch", flush=True)
        return []
    now = now or datetime.now(timezone.utc)
    token = _app_token()
    logins = [str(s["login"]).lower() for s in sources if s.get("login")]
    if categories:
        logins += _top_live(token, categories, now)
    logins = list(dict.fromkeys(logins))
    users = _helix("users", token, [("login", login) for login in logins[:100]])

    candidates: List[Dict] = []
    locked: Dict[str, bool] = {}
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
            vod_url = f"https://www.twitch.tv/videos/{m['vod_id']}"
            if _vod_locked(vod_url, locked):
                url, start, end = m["top_url"], None, None   # the whole clip is the source
            else:
                url, start, end = vod_url, round(max(0.0, m["start"] - WINDOW_PAD_SECONDS)), round(m["end"] + WINDOW_PAD_SECONDS)
            candidates.append({
                # Bucket by 2 minutes so tomorrow's extra clips of the same moment don't re-queue it.
                "key": f"tw:{m['vod_id']}:{int(m['start'] // 120)}",
                "platform": "Twitch",
                "title": m["title"],
                "url": url,
                "channel": m["channel"],
                "published": datetime.fromisoformat(m["created_at"].replace("Z", "+00:00")),
                "views": int(m["views"]),
                "score": round(m["views"] / baseline, 2),
                "start": start,
                "end": end,
            })
        print(f"[feed/twitch] {user['login']}: {len(clips)} clips → {len(moments)} moments", flush=True)
    return candidates
