"""Whitelist of channels the feed watches (sources.json).

Only list channels you are allowed to clip (clipping programs, your own
channels, explicit permission): reuploading other creators' content without
permission gets Content ID claims and "reused content" demonetisation.
"""
import json
import os
from typing import Dict, List

from ..config import LOCAL_OUTPUT_DIR

_CHANNEL_ID_CACHE = os.path.join(LOCAL_OUTPUT_DIR, "feed_channel_ids.json")


def load_sources(path: str) -> Dict[str, List[Dict]]:
    if not os.path.exists(path):
        raise RuntimeError(
            f"{path} not found. Copy sources.example.json to {path} and list the channels you are allowed to clip."
        )
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {"youtube": data.get("youtube") or [], "twitch": data.get("twitch") or []}


def resolve_youtube_channel_id(source: Dict) -> str:
    """Return the UC… channel id for {"channel_id": ...} or {"handle": "@name"} (cached on disk)."""
    if source.get("channel_id"):
        return source["channel_id"]
    handle = str(source.get("handle") or "").strip()
    if not handle:
        raise RuntimeError(f"YouTube source needs 'handle' or 'channel_id': {source}")
    handle = handle if handle.startswith("@") else "@" + handle

    cache: Dict[str, str] = {}
    if os.path.exists(_CHANNEL_ID_CACHE):
        with open(_CHANNEL_ID_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    if handle.lower() in cache:
        return cache[handle.lower()]

    import yt_dlp  # type: ignore

    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "playlistend": 1}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/{handle}/videos", download=False)
    channel_id = info.get("channel_id") or ""
    if not channel_id.startswith("UC"):
        raise RuntimeError(f"could not resolve YouTube handle {handle}")

    cache[handle.lower()] = channel_id
    os.makedirs(os.path.dirname(_CHANNEL_ID_CACHE) or ".", exist_ok=True)
    with open(_CHANNEL_ID_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)
    return channel_id
