"""Delete old downloads and rendered clips.

Source videos are the bulk of the disk usage and are useless once their shorts
are rendered; a clip is useless once it is published. Anything a Notion row
still needs (not published yet) is kept, whatever its age.
"""
import os
import time
from pathlib import Path
from typing import List, Optional, Set, Tuple

from ..config import LOCAL_OUTPUT_DIR, MEDIA_RETENTION_DAYS, NOTION_SHORTS_DB, NOTION_TOKEN

MEDIA_SUFFIXES = {".mp4", ".mkv", ".webm", ".m4a", ".wav"}


def _notion_files() -> Tuple[Set[str], Set[str]]:
    """(clips to keep, clips already published) — empty when Notion isn't configured."""
    if not (NOTION_TOKEN and NOTION_SHORTS_DB):
        return set(), set()
    from ..feed.notion import Notion

    notion = Notion(NOTION_TOKEN)
    resolve = lambda files: {str(Path(f).resolve()) for f in files}  # noqa: E731
    try:
        keep = resolve(notion.files_by_state(NOTION_SHORTS_DB, published=False))
        done = resolve(notion.files_by_state(NOTION_SHORTS_DB, published=True))
    except Exception as e:  # never let housekeeping break the run that called it
        print(f"[clean] skipped: could not read Notion ({e})", flush=True)
        raise
    return keep, done - keep


def purge_media(days: float = MEDIA_RETENTION_DAYS, dry_run: bool = False) -> List[Path]:
    """Delete media files older than `days` under the output directory."""
    root = Path(LOCAL_OUTPUT_DIR)
    if not root.exists():
        return []
    try:
        keep, published = _notion_files()
    except Exception:
        return []  # _notion_files() already explained why; deleting blind could drop a pending short
    cutoff = time.time() - days * 86400
    removed, freed = [], 0
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in MEDIA_SUFFIXES:
            continue
        resolved = str(path.resolve())
        if resolved in keep:
            continue
        # A published clip is dead weight straight away; everything else waits out its retention.
        if resolved not in published and path.stat().st_mtime > cutoff:
            continue
        size = path.stat().st_size
        if not dry_run:
            try:
                os.remove(path)
            except OSError as e:
                print(f"[clean] could not delete {path}: {e}", flush=True)
                continue
        removed.append(path)
        freed += size
    if not dry_run:
        for folder in sorted(root.rglob("*"), key=lambda f: len(f.parts), reverse=True):
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
    if removed:
        verb = "would free" if dry_run else "freed"
        print(f"[clean] {len(removed)} file(s) older than {days:g} day(s), {verb} {freed / 1e9:.2f} GB", flush=True)
    return removed


def purge_quietly(days: Optional[float] = None) -> None:
    """Housekeeping at the end of a run: never fails the command that called it."""
    try:
        purge_media(MEDIA_RETENTION_DAYS if days is None else days)
    except Exception as e:
        print(f"[clean] skipped: {e}", flush=True)
