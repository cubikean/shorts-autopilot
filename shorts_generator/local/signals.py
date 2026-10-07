"""Cheap signals that point the highlight picker at what the audience reacted to.

- Comment hot zones: timestamps viewers cite in the top YouTube comments
  ("8:50 il a oublié de payer le monteur 😂"). Available within hours of
  release, unlike YouTube's "most replayed" curve (published after ~10 days).
- Audio markers: loudness spikes (shouting, laughter, hype) and laughter written
  out in the transcript — what a text-only transcript otherwise hides.

Both are best effort: any failure returns no signal rather than failing the video.
"""
import json
import math
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

from .downloader import _extract_youtube_video_id

MAX_COMMENTS = 400
HOTSPOT_WINDOW_SECONDS = 30
MAX_HOTSPOTS = 12

# Matches 8:50 or 1:02:03, but not part of a longer number, a ratio or a date.
TIMESTAMP_RE = re.compile(r"(?<![\d:/.,])(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?![\d:])")

AUDIO_SAMPLE_RATE = 8000
AUDIO_FRAME_SECONDS = 0.25
LOUD_TOP_SHARE, LOUD_MIN_DB = 0.06, 5.0             # top 6% of segments, 5 dB above the median
VERY_LOUD_TOP_SHARE, VERY_LOUD_MIN_DB = 0.015, 8.0
LAUGH_RE = re.compile(r"\b(?:ha|ah|hi|hé){3,}h?\b|[(\[*](?:rires?|laughs?|laughter)[)\]*]", re.IGNORECASE)


# --- comments ------------------------------------------------------------------


def _fetch_mentions(video_url: str, duration: float) -> List[Dict]:
    import yt_dlp  # type: ignore

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "getcomments": True,
        "extractor_args": {"youtube": {"max_comments": [str(MAX_COMMENTS), "all", "0"], "comment_sort": ["top"]}},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video_url, download=False)
    mentions = []
    for comment in info.get("comments") or []:
        text = str(comment.get("text") or "")
        seconds = []
        for h, m, s in TIMESTAMP_RE.findall(text):
            t = int(h or 0) * 3600 + int(m) * 60 + int(s)
            if int(s) < 60 and t < duration:
                seconds.append(t)
        if not seconds:
            continue
        quote = re.sub(r"\s+", " ", TIMESTAMP_RE.sub("", text)).strip(" -:,")
        for t in seconds:
            # A comment listing many timestamps (a recap) counts as one vote spread across them.
            mentions.append({"t": t, "likes": int(comment.get("like_count") or 0), "share": 1 / len(seconds), "quote": quote[:100]})
    return mentions


def comment_hotspots(video_url: str, source_path: str, duration: float) -> List[Dict]:
    """Windows of the video that viewers cite most in the top comments, in time order.

    Mentions are cached next to the source, so a re-run sends the LLM the same prompt (and hits its cache).
    """
    if not _extract_youtube_video_id(video_url):
        return []
    cache = Path(source_path).with_suffix(".comments.json")
    try:
        if cache.exists():
            mentions = json.loads(cache.read_text(encoding="utf-8"))
        else:
            mentions = _fetch_mentions(video_url, duration)
            cache.write_text(json.dumps(mentions, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[signals] comments unavailable: {e}", flush=True)
        return []

    windows: Dict[int, List[Dict]] = defaultdict(list)
    for m in mentions:
        windows[int(m["t"]) // HOTSPOT_WINDOW_SECONDS * HOTSPOT_WINDOW_SECONDS].append(m)
    hotspots = []
    for start, items in windows.items():
        # Likes count, with diminishing returns: one 500-like comment shouldn't drown ten others.
        weight = sum(m["share"] * (1 + math.log1p(m["likes"])) for m in items)
        quotes = [m["quote"] for m in sorted(items, key=lambda m: -m["likes"]) if m["quote"]][:2]
        hotspots.append({"start": start, "end": start + HOTSPOT_WINDOW_SECONDS,
                         "mentions": len(items), "weight": round(weight, 1), "quotes": quotes})
    hotspots = sorted(hotspots, key=lambda h: -h["weight"])[:MAX_HOTSPOTS]
    print(f"[signals] {len(mentions)} timestamp mention(s) in comments → {len(hotspots)} hot zone(s)", flush=True)
    return sorted(hotspots, key=lambda h: h["start"])


# --- audio ---------------------------------------------------------------------


def _frame_db(media_path: str) -> List[float]:
    import numpy as np

    pcm = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", media_path, "-vn", "-ac", "1", "-ar", str(AUDIO_SAMPLE_RATE), "-f", "s16le", "-"],
        capture_output=True, check=True, timeout=900,
    ).stdout
    samples = np.frombuffer(pcm, np.int16).astype(np.float32)
    frame = int(AUDIO_SAMPLE_RATE * AUDIO_FRAME_SECONDS)
    usable = len(samples) // frame * frame
    rms = np.sqrt(np.mean(samples[:usable].reshape(-1, frame) ** 2, axis=1))
    return (20 * np.log10(rms + 1.0)).tolist()


def annotate_audio(media_path: str, transcript: Dict) -> Dict:
    """Return a copy of the transcript whose segments carry "markers" (LOUD, VERY LOUD, LAUGH).

    Loudness is relative to the video itself: each segment's loudest second is
    compared with the median segment, so a quiet podcast and a screaming
    gaming session both get their own peaks flagged.
    """
    segments = transcript.get("segments", [])
    if not segments:
        return transcript
    try:
        import numpy as np

        db = np.array(_frame_db(media_path))
        per_second = max(1, int(round(1 / AUDIO_FRAME_SECONDS)))
        smooth = np.convolve(db, np.ones(per_second) / per_second, mode="same") if len(db) else db
        peaks = []
        for s in segments:
            a, b = int(s["start"] / AUDIO_FRAME_SECONDS), int(math.ceil(s["end"] / AUDIO_FRAME_SECONDS))
            window = smooth[a:max(b, a + 1)]
            peaks.append(float(window.max()) if len(window) else float("-inf"))
        peaks_arr = np.array(peaks)
        median = float(np.median(peaks_arr[np.isfinite(peaks_arr)]))
        loud = max(float(np.quantile(peaks_arr, 1 - LOUD_TOP_SHARE)), median + LOUD_MIN_DB)
        very_loud = max(float(np.quantile(peaks_arr, 1 - VERY_LOUD_TOP_SHARE)), median + VERY_LOUD_MIN_DB)
    except Exception as e:
        print(f"[signals] audio analysis skipped: {e}", flush=True)
        peaks, loud, very_loud = [float("-inf")] * len(segments), float("inf"), float("inf")

    annotated, counts = [], defaultdict(int)
    for s, peak in zip(segments, peaks):
        markers = []
        if peak >= very_loud:
            markers.append("VERY LOUD")
        elif peak >= loud:
            markers.append("LOUD")
        if LAUGH_RE.search(s.get("text", "")):
            markers.append("LAUGH")
        for m in markers:
            counts[m] += 1
        annotated.append({**s, "markers": markers})
    print(f"[signals] audio markers: {dict(counts) or 'none'}", flush=True)
    return {**transcript, "segments": annotated}
