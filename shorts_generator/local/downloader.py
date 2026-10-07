"""Local YouTube download via yt-dlp.

Returns a local mp4 path so the rest of the local pipeline can read it
directly off disk.
"""
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from typing import Optional

from ..config import LOCAL_OUTPUT_DIR


def _import_ytdlp():
    try:
        import yt_dlp  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "yt-dlp is required. Install it with:\n"
            "    pip install -r requirements.txt"
        ) from e
    return yt_dlp


def _format_for(fmt: str) -> str:
    """Map our '720' / '1080' shorthand to a yt-dlp format selector."""
    try:
        height = int(fmt)
    except ValueError:
        height = 720
    return (
        f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
        f"best[height<={height}][ext=mp4]/best"
    )


def _extract_youtube_video_id(source: str) -> Optional[str]:
    """Best-effort extraction of a YouTube video id from a URL."""
    parsed = urlparse(source)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]

    if host in ("youtu.be", "www.youtu.be"):
        video_id = parsed.path.lstrip("/").split("/", 1)[0]
        return video_id or None

    if "youtube.com" in host:
        if parsed.path.startswith("/watch"):
            qs = parse_qs(parsed.query)
            video_id = qs.get("v", [""])[0]
            return video_id or None
        match = re.search(r"/(?:shorts|embed|live)/([^/?#&]+)", parsed.path)
        if match:
            return match.group(1)

    return None


def _resolve_local_path(source: str) -> Optional[str]:
    """Return a local filesystem path if the input already points at one."""
    parsed = urlparse(source)
    if parsed.scheme == "file":
        raw_path = unquote(parsed.path)
        if parsed.netloc and parsed.netloc not in ("", "localhost"):
            raw_path = f"//{parsed.netloc}{raw_path}"
        candidate = Path(raw_path).expanduser()
        if candidate.exists() and candidate.is_file():
            return str(candidate.resolve())
        raise RuntimeError(f"Local file URL does not exist: {source}")

    if parsed.scheme in ("http", "https"):
        return None

    candidate = Path(source).expanduser()
    if candidate.exists() and candidate.is_file():
        return str(candidate.resolve())

    if any(sep in source for sep in (os.sep, "/")) or source.startswith("~") or source.startswith("."):
        raise RuntimeError(f"Local file path does not exist: {source}")

    return None


def _usable_media(path: str) -> bool:
    """True when the file has both a video and an audio stream.

    yt-dlp can leave a ~1 KB mp4 behind without raising (e.g. a Twitch VOD part it
    couldn't fetch); transcription then fails with an obscure "tuple index out of range".
    """
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return os.path.getsize(path) > 100_000  # no ffprobe: at least reject near-empty files
    streams = probe.stdout.split()
    return "video" in streams and "audio" in streams


def _cached(stem: str) -> Optional[str]:
    """A previous download of this stem, if usable; broken leftovers are deleted."""
    for ext in (".mp4", ".mkv", ".webm"):
        candidate = stem + ext
        if os.path.exists(candidate):
            if _usable_media(candidate):
                return candidate
            print(f"[download/local] discarding unusable cached file: {candidate}", flush=True)
            os.remove(candidate)
    return None


def _existing_download(out_dir: str, video_id: str) -> Optional[str]:
    """Return a cached download path if we already have this YouTube id."""
    return _cached(os.path.join(out_dir, f"source_{video_id}"))


HLS_SECTION_TIMEOUT_SECONDS = 600


def _hls_section(info: dict, start: float, end: float, out_path: str) -> bool:
    """Cut [start, end] out of an HLS stream by handing ffmpeg only the segments that cover it.

    Seeking the full playlist breaks on Twitch VODs longer than ~26.5 h: MPEG-TS
    timestamps wrap at 2^33 ticks, so ffmpeg never reaches a later moment and reads
    the whole VOD while writing nothing. A trimmed playlist starts at the wanted
    segment, so the wrap never matters and only a few segments are fetched.
    Returns False when the selected format isn't a single HLS stream.
    """
    import requests

    if info.get("requested_formats") or not str(info.get("protocol", "")).startswith("m3u8"):
        return False
    response = requests.get(info["url"], headers=info.get("http_headers"), timeout=30)
    response.raise_for_status()

    segments, position, duration = [], 0.0, None
    for line in response.text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            duration = float(line[len("#EXTINF:"):].split(",", 1)[0])
        elif line and not line.startswith("#") and duration is not None:
            if position + duration > start and position < end:
                segments.append((position, duration, urljoin(info["url"], line)))
            position += duration
            duration = None
    if not segments:
        return False

    playlist = Path(out_path).with_suffix(".m3u8")
    playlist.write_text(
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-PLAYLIST-TYPE:VOD\n"
        f"#EXT-X-TARGETDURATION:{int(max(d for _, d, _ in segments)) + 1}\n"
        + "".join(f"#EXTINF:{d:.3f},\n{url}\n" for _, d, url in segments)
        + "#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
                "-i", str(playlist),
                "-ss", f"{start - segments[0][0]:.3f}", "-t", f"{end - start:.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
                out_path,
            ],
            check=True, timeout=HLS_SECTION_TIMEOUT_SECONDS,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"[download/local] HLS section cut failed: {e}", flush=True)
    finally:
        playlist.unlink(missing_ok=True)
    return True


def download_section_local(video_url: str, start: float, end: float, name: str, fmt: str = "1080") -> str:
    """Download only [start, end] seconds of a video (e.g. a moment in a long Twitch VOD)."""
    yt_dlp = _import_ytdlp()
    from yt_dlp.utils import download_range_func  # type: ignore

    out_dir = LOCAL_OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.join(out_dir, f"source_{name}")
    cached = _cached(stem)
    if cached:
        print(f"[download/local] reusing cached section: {cached}", flush=True)
        return cached

    print(f"[download/local] {video_url} [{start:.0f}s-{end:.0f}s] @ {fmt}p", flush=True)
    with yt_dlp.YoutubeDL({"format": _format_for(fmt), "quiet": True, "no_warnings": True}) as ydl:
        info = ydl.extract_info(video_url, download=False)
    if _hls_section(info, start, end, stem + ".mp4"):
        ready = _cached(stem)
        if ready:
            print(f"[download/local] ready: {ready}", flush=True)
            return ready
        raise RuntimeError(
            f"section download produced no usable video for {video_url} [{start:.0f}s-{end:.0f}s] "
            "(VOD part unavailable, muted or still processing)"
        )

    ydl_opts = {
        "format": _format_for(fmt),
        "outtmpl": stem + ".%(ext)s",
        "merge_output_format": "mp4",
        "download_ranges": download_range_func(None, [(start, end)]),
        "force_keyframes_at_cuts": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([video_url])
    ready = _cached(stem)
    if ready:
        print(f"[download/local] ready: {ready}", flush=True)
        return ready
    raise RuntimeError(
        f"section download produced no usable video for {video_url} [{start:.0f}s-{end:.0f}s] "
        "(VOD part unavailable, muted or still processing)"
    )


def download_youtube_local(video_url: str, fmt: str = "1080", out_dir: Optional[str] = None) -> str:
    """Download a remote URL or return a local file path unchanged."""
    local_path = _resolve_local_path(video_url)
    if local_path:
        print(f"[download/local] using local file: {local_path}", flush=True)
        return local_path

    yt_dlp = _import_ytdlp()
    out_dir = out_dir or LOCAL_OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)

    video_id = _extract_youtube_video_id(video_url)
    if video_id:
        cached = _existing_download(out_dir, video_id)
        if cached:
            print(f"[download/local] reusing cached download: {cached}", flush=True)
            return cached

    print(f"[download/local] {video_url} @ {fmt}p → {out_dir}/", flush=True)
    ydl_opts = {
        "format": _format_for(fmt),
        "outtmpl": os.path.join(out_dir, "source_%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=True)
        path = ydl.prepare_filename(info)
        # merge_output_format may rename the extension after merge
        if not os.path.exists(path):
            stem, _ = os.path.splitext(path)
            for ext in (".mp4", ".mkv", ".webm"):
                if os.path.exists(stem + ext):
                    path = stem + ext
                    break

    print(f"[download/local] ready: {path}", flush=True)
    return path
