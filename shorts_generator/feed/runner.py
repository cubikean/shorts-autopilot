"""`discover` (fill the Notion queue) and `process` (render shorts from it)."""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from ..config import (
    FEED_CLIPS_PER_VIDEO,
    FEED_MAX_PER_RUN,
    FEED_SOURCES_FILE,
    LOCAL_OUTPUT_DIR,
    NOTION_SHORTS_DB,
    NOTION_TOKEN,
    NOTION_VIDEOS_DB,
)
from .notion import STATUS_ERROR, STATUS_READY, STATUS_RUNNING, Notion
from .sources import load_sources
from .twitch import discover_twitch
from .youtube import discover_youtube


def _require_db(value: str, name: str) -> str:
    if not value:
        raise RuntimeError(f"{name} is not set. Run `python feed.py setup-notion <page>` and add the ids to .env.")
    return value


def discover(dry_run: bool = False) -> List[Dict]:
    sources = load_sources(FEED_SOURCES_FILE)
    now = datetime.now(timezone.utc)
    candidates = discover_youtube(sources["youtube"], now) + discover_twitch(sources["twitch"], now)
    candidates.sort(key=lambda c: -c["score"])

    print(f"\n{len(candidates)} candidate(s):")
    for c in candidates:
        window = f" [{c['start']}-{c['end']}s]" if c["start"] is not None else ""
        print(f"  {c['score']:>6.2f}  {c['platform']:<7} {c['channel'][:20]:<20} {c['views']:>9} views  {c['title'][:60]}{window}")

    if dry_run:
        return candidates
    notion = Notion(NOTION_TOKEN)
    database = _require_db(NOTION_VIDEOS_DB, "NOTION_VIDEOS_DB")
    added = 0
    for c in candidates:
        if notion.has_key(database, c["key"]):
            continue
        notion.add_video(database, c)
        added += 1
    print(f"\n[feed] {added} new video(s) queued in Notion, {len(candidates) - added} already known")
    return candidates


def process(limit: int = FEED_MAX_PER_RUN) -> None:
    from ..local.downloader import download_section_local
    from ..pipeline import generate_shorts

    notion = Notion(NOTION_TOKEN)
    videos_db = _require_db(NOTION_VIDEOS_DB, "NOTION_VIDEOS_DB")
    shorts_db = _require_db(NOTION_SHORTS_DB, "NOTION_SHORTS_DB")
    # The scheduled task never overlaps itself, so anything still "En cours" is from a run that died.
    requeued = notion.requeue_running(videos_db)
    if requeued:
        print(f"[feed] {requeued} video(s) left \"En cours\" by an interrupted run put back in the queue")
    rows = notion.todo(videos_db, limit)
    print(f"[feed] {len(rows)} video(s) to process")

    for row in rows:
        print(f"\n[feed] ▶ {row['platform']} · {row['channel']} · {row['title']}", flush=True)
        notion.set_status(row["page_id"], STATUS_RUNNING)
        safe_key = re.sub(r"[^\w.-]", "_", row["key"] or row["page_id"])
        try:
            source, num_clips = row["url"], FEED_CLIPS_PER_VIDEO
            if row["start"] is not None and row["end"] is not None:
                # A clipped moment inside a long VOD: fetch just that window, one short from it.
                source = download_section_local(row["url"], row["start"], row["end"], name=safe_key)
                num_clips = 1
            result = generate_shorts(
                source,
                num_clips=num_clips,
                out_dir=os.path.join(LOCAL_OUTPUT_DIR, "shorts", safe_key),
            )
            rendered = [s for s in result["shorts"] if s.get("clip_url")]
            if not rendered:
                errors = "; ".join(str(s.get("error")) for s in result["shorts"])
                raise RuntimeError(f"no clip rendered ({errors})")

            credit = f"🎥 Source : {row['channel']} — {row['url']}"
            for short in rendered:
                if row["start"] is not None:
                    # Times are relative to the downloaded window; report them in VOD time.
                    short["start_time"] = float(short["start_time"]) + row["start"]
                    short["end_time"] = float(short["end_time"]) + row["start"]
                description = f"{short.get('description', '')}\n\n{credit}".strip()
                notion.add_short(shorts_db, row["page_id"], short, description)
                meta_path = Path(short["clip_url"]).with_suffix(".json")
                if meta_path.exists():
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    meta.update(
                        description=description,
                        source=row["url"],
                        start_time=short["start_time"],
                        end_time=short["end_time"],
                    )
                    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            notion.set_status(row["page_id"], STATUS_READY)
            print(f"[feed] ✔ {len(rendered)} short(s) ready", flush=True)
        except Exception as e:
            print(f"[feed] ✘ {e}", flush=True)
            notion.set_status(row["page_id"], STATUS_ERROR, str(e)[:1900])
