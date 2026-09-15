"""End-to-end orchestrator: yt-dlp → faster-whisper → LLM highlights → ffmpeg/OpenCV render."""
import json
import os
from pathlib import Path
from typing import Dict, List, Optional

from .config import LOCAL_OUTPUT_DIR
from .highlights import get_highlights


def generate_shorts(
    youtube_url: str,
    num_clips: int = 3,
    aspect_ratio: str = "9:16",
    download_format: str = "720",
    language: Optional[str] = None,
    subtitles: bool = True,
    layout: str = "auto",
    out_dir: Optional[str] = None,
) -> Dict:
    """Run the full pipeline and return a structured result.

    Args:
        youtube_url: remote URL (anything yt-dlp reads), file:// URL or local path.
        num_clips: how many shorts to render.
        aspect_ratio: e.g. "9:16", "1:1".
        download_format: source resolution ("360" / "480" / "720" / "1080").
        language: ISO-639-1 to force Whisper language detection.
        subtitles: burn word-by-word captions, in the spoken language, into each clip.
        layout: "auto" stacks a detected stream webcam above the content,
            "stack" forces it for any steady face, "single" always uses a
            face-tracked crop.
        out_dir: where clips + metadata JSON go (default LOCAL_OUTPUT_DIR/<source id>/).

    Returns:
        {
          "source_video_url": str,   # local path of the downloaded source
          "transcript": {...},
          "highlights": [...],       # all candidates ranked
          "shorts": [...],           # top `num_clips` with clip_url = local mp4 path
        }
    """
    from .local.clipper import crop_highlights_local
    from .local.downloader import download_youtube_local
    from .local.llm import call_local_llm
    from .local.transcriber import transcribe_local

    source_path = download_youtube_local(youtube_url, fmt=download_format)

    transcript = transcribe_local(source_path, language=language)
    if not transcript["segments"]:
        raise RuntimeError(
            "Whisper produced no segments. The video may have no detectable speech."
        )

    highlights_result = get_highlights(transcript, num_clips=num_clips, llm_fn=call_local_llm)
    all_highlights: List[Dict] = highlights_result.get("highlights", [])
    if not all_highlights:
        raise RuntimeError("Highlight generator returned zero clips.")

    top = sorted(all_highlights, key=lambda h: int(h.get("score", 0)), reverse=True)[:num_clips]
    print(f"[pipeline] cropping {len(top)} of {len(all_highlights)} candidates", flush=True)

    # One folder per source so runs never overwrite each other's short_XX.mp4.
    out_dir = out_dir or os.path.join(LOCAL_OUTPUT_DIR, Path(source_path).stem.removeprefix("source_"))
    shorts = crop_highlights_local(
        source_path,
        top,
        aspect_ratio=aspect_ratio,
        out_dir=out_dir,
        transcript=transcript if subtitles else None,
        layout=layout,
    )

    # Publishing metadata next to each mp4 (short_01.mp4 → short_01.json).
    for short in shorts:
        if short.get("clip_url"):
            meta = {key: short.get(key) for key in (
                "title", "description", "hashtags", "hook_sentence", "virality_reason",
                "score", "start_time", "end_time",
            )}
            meta["source"] = youtube_url
            Path(short["clip_url"]).with_suffix(".json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    return {
        "source_video_url": source_path,
        "transcript": transcript,
        "highlights": all_highlights,
        "shorts": shorts,
    }
