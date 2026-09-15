"""YouTube discovery: channel RSS feeds (no API key, no quota) + yt-dlp details.

Score = the video's views per hour divided by the channel's median views per
hour over its recent uploads, i.e. "how much faster than usual is this taking
off". Only videos past the score threshold get the slower yt-dlp lookup for
duration and live status; videos outside FEED_MIN/MAX_DURATION_MINUTES are skipped.
YouTube Shorts (up to 3 min) are skipped too: they are already short-form.
"""
import statistics
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

from ..config import (
    FEED_MAX_AGE_HOURS,
    FEED_MAX_DURATION_MINUTES,
    FEED_MIN_AGE_HOURS,
    FEED_MIN_DURATION_MINUTES,
    FEED_MIN_SCORE,
)
from .sources import resolve_youtube_channel_id

RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


def fetch_feed(channel_id: str) -> List[Dict]:
    """Latest ~15 uploads with view counts, straight from the public RSS feed."""
    resp = requests.get(RSS_URL.format(channel_id), timeout=30)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    entries = []
    for entry in root.findall("atom:entry", NS):
        video_id = entry.findtext("yt:videoId", namespaces=NS)
        stats = entry.find("media:group/media:community/media:statistics", NS)
        link = entry.find("atom:link", NS)
        entries.append({
            # The feed links Shorts as /shorts/<id> and regular uploads as /watch?v=<id>.
            "is_short": link is not None and "/shorts/" in (link.get("href") or ""),
            "id": video_id,
            "title": entry.findtext("atom:title", default="", namespaces=NS),
            "channel": entry.findtext("atom:author/atom:name", default="", namespaces=NS),
            "published": datetime.fromisoformat(entry.findtext("atom:published", namespaces=NS)),
            "views": int(stats.get("views", 0)) if stats is not None else 0,
            "url": f"https://www.youtube.com/watch?v={video_id}",
        })
    return entries


def _details(url: str) -> Optional[Dict]:
    import yt_dlp  # type: ignore

    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as e:  # members-only, removed, region-locked...
        print(f"[feed/youtube] skip {url}: {e}", flush=True)
        return None


def discover_youtube(sources: List[Dict], now: Optional[datetime] = None) -> List[Dict]:
    now = now or datetime.now(timezone.utc)
    candidates: List[Dict] = []
    for source in sources:
        label = source.get("handle") or source.get("channel_id")
        try:
            entries = fetch_feed(resolve_youtube_channel_id(source))
        except Exception as e:
            print(f"[feed/youtube] {label}: {e}", flush=True)
            continue

        for e in entries:
            e["age_hours"] = (now - e["published"]).total_seconds() / 3600
            e["velocity"] = e["views"] / max(e["age_hours"], 1.0)
        velocities = [e["velocity"] for e in entries if e["views"] > 0]
        baseline = statistics.median(velocities) if velocities else 0.0
        recent = [e for e in entries if FEED_MIN_AGE_HOURS <= e["age_hours"] <= FEED_MAX_AGE_HOURS]
        print(
            f"[feed/youtube] {label}: {len(entries)} uploads, {len(recent)} in the {FEED_MIN_AGE_HOURS:g}-{FEED_MAX_AGE_HOURS:g}h window, "
            f"baseline {baseline:.0f} views/h",
            flush=True,
        )

        for e in recent:
            if e["is_short"]:
                # Already a Short (up to 3 min): nothing to cut. Checked before the slow yt-dlp lookup.
                print(f"[feed/youtube]   skip {e['title'][:60]!r}: already a YouTube Short", flush=True)
                continue
            score = e["velocity"] / baseline if baseline > 0 else 0.0
            if score < FEED_MIN_SCORE:
                print(f"[feed/youtube]   skip {e['title'][:60]!r}: score {score:.2f} < {FEED_MIN_SCORE:g}", flush=True)
                continue
            info = _details(e["url"])
            if not info:
                continue
            minutes = (info.get("duration") or 0) / 60
            if info.get("media_type") == "short":
                reason = "already a YouTube Short"  # backup in case the feed's link format changes
            elif info.get("live_status") not in (None, "not_live", "was_live"):
                reason = f"live status {info.get('live_status')}"
            elif not FEED_MIN_DURATION_MINUTES <= minutes <= FEED_MAX_DURATION_MINUTES:
                reason = f"duration {minutes:.1f} min outside {FEED_MIN_DURATION_MINUTES:g}-{FEED_MAX_DURATION_MINUTES:g} min"
            else:
                reason = None
            if reason:
                print(f"[feed/youtube]   skip {e['title'][:60]!r}: {reason}", flush=True)
                continue
            candidates.append({
                "key": f"yt:{e['id']}",
                "platform": "YouTube",
                "title": e["title"],
                "url": e["url"],
                "channel": e["channel"],
                "published": e["published"],
                "views": int(info.get("view_count") or e["views"]),
                "score": round(score, 2),
                "start": None,
                "end": None,
            })
    return candidates
