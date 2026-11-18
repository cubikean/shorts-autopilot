"""Copy every rendered short to a folder, for posting by hand on tiktok.com.

TikTok's API only drops drafts in the app inbox, and refuses more while 5 are
pending. The export folder has no such cap: each short lands there as
"<date heure> - <titre>.mp4" next to a .txt holding its caption, ready to upload
from TikTok web. Names start with the render time, so the newest sort last.
"""
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ..config import LOCAL_OUTPUT_DIR, TIKTOK_EXPORT_DIR

# "#shorts" only means something on YouTube.
_YOUTUBE_ONLY_TAGS = {"#shorts"}


def _safe_name(title: str) -> str:
    """Title usable as a Windows file name (emoji are fine on NTFS)."""
    name = re.sub(r"\s+", " ", re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title)).strip().rstrip(". ")
    return name[:80].rstrip(". ") or "short"


def caption(title: str, description: str, hashtags: Iterable[str]) -> str:
    tags = " ".join(t for t in ("#" + str(t).lstrip("#") for t in hashtags if str(t).strip("# "))
                    if t.lower() not in _YOUTUBE_ONLY_TAGS)
    return "\n\n".join(part for part in (title.strip(), description.strip(), tags) if part)[:2200]


def export_short(clip: Path, meta: Dict, rendered_at: Optional[datetime] = None) -> Optional[Path]:
    """Copy one short and write its caption. Returns the new video, None if exporting is off or done already."""
    if not TIKTOK_EXPORT_DIR:
        return None
    folder = Path(TIKTOK_EXPORT_DIR)
    when = rendered_at or datetime.fromtimestamp(clip.stat().st_mtime)
    title = str(meta.get("title") or "").strip()
    suffix = f" - {_safe_name(title)}.mp4"
    folder.mkdir(parents=True, exist_ok=True)
    # Matched on the title alone: a backfill must not copy again what a render already exported.
    if any(f.name.endswith(suffix) for f in folder.iterdir()):
        return None
    video = folder / f"{when:%Y-%m-%d %Hh%M}{suffix}"
    shutil.copy2(clip, video)
    text = caption(title, str(meta.get("description") or ""), meta.get("hashtags") or [])
    video.with_suffix(".txt").write_text(text + "\n", encoding="utf-8")
    print(f"[export] TikTok: {video}", flush=True)
    return video


def export_quietly(clip: Path, meta: Dict) -> None:
    """Best effort: a full disk or a locked folder must not fail the render."""
    try:
        export_short(clip, meta)
    except Exception as e:
        print(f"[export] TikTok copy skipped: {e}", flush=True)


def export_existing(days: Optional[float] = None) -> List[Path]:
    """Export the shorts already rendered in output/shorts (newer than `days`, all when None)."""
    now = datetime.now()
    exported = []
    for meta_path in sorted(Path(LOCAL_OUTPUT_DIR, "shorts").glob("*/short_*.json")):
        clip = meta_path.with_suffix(".mp4")
        if not clip.is_file():
            continue
        rendered_at = datetime.fromtimestamp(clip.stat().st_mtime)
        if days is not None and (now - rendered_at).total_seconds() > days * 86400:
            continue
        video = export_short(clip, json.loads(meta_path.read_text(encoding="utf-8")), rendered_at)
        if video:
            exported.append(video)
    return exported
