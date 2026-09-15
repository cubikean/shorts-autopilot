"""`publish`: upload the shorts validated in Notion to every enabled platform.

Notion workflow on the Shorts database:
  Publication "À publier" → you set "Validé" → each platform's URL column gets
  filled → "Publié". A failure sets "Erreur" with the reason in "Erreur publication";
  set "Validé" again to retry. Platforms already published are never redone.
"""
import re
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ..config import NOTION_SHORTS_DB, NOTION_TOKEN, PUBLISH_MAX_PER_RUN, PUBLISH_PLATFORMS
from ..feed.notion import PUB_ERROR, PUB_PUBLISHED, Notion
from .base import Publisher, QuotaExceeded, ShortPost

REPO_ROOT = Path(__file__).resolve().parents[2]


def _youtube() -> Publisher:
    from .youtube import YouTubePublisher

    return YouTubePublisher()


# Platform key → (Notion URL column holding the post link, publisher factory).
# A new platform (TikTok next) = one module with a Publisher + one entry here.
PLATFORMS: Dict[str, Tuple[str, Callable[[], Publisher]]] = {
    "youtube": ("YouTube", _youtube),
}


def link_columns(names: Optional[List[str]] = None) -> List[str]:
    return [PLATFORMS[name][0] for name in (names or list(PLATFORMS))]


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.astimezone()  # date-only / naive = local time


def _post(row: Dict) -> ShortPost:
    file = Path(row["file"])
    return ShortPost(
        page_id=row["page_id"],
        title=row["title"],
        hook=row["hook"],
        description=row["description"],
        hashtags=["#" + tag.lstrip("#") for tag in re.split(r"[\s,]+", row["hashtags"]) if tag.lstrip("#")],
        file=file if file.is_absolute() else REPO_ROOT / file,
        publish_at=_parse_date(row["publish_at"]),
    )


def publish(limit: int = PUBLISH_MAX_PER_RUN, platforms: Optional[List[str]] = None, dry_run: bool = False) -> None:
    names = platforms or PUBLISH_PLATFORMS
    unknown = [name for name in names if name not in PLATFORMS]
    if unknown:
        raise RuntimeError(f"unknown platform(s) {unknown}; supported: {', '.join(PLATFORMS)}")
    if not NOTION_SHORTS_DB:
        raise RuntimeError("NOTION_SHORTS_DB is not set. Run `python feed.py setup-notion <page>` and add the ids to .env.")

    notion = Notion(NOTION_TOKEN)
    rows = notion.validated_shorts(NOTION_SHORTS_DB, link_columns(names), limit)
    print(f"[publish] {len(rows)} validated short(s) to publish on {', '.join(names)}")
    if not rows:
        return
    clients = {} if dry_run else {name: PLATFORMS[name][1]() for name in names}

    for row in rows:
        post = _post(row)
        pending = [name for name in names if not row["links"].get(PLATFORMS[name][0])]
        when = f" · programmé {post.publish_at:%Y-%m-%d %H:%M}" if post.publish_at else ""
        print(f"\n[publish] ▶ {post.title} → {', '.join(pending)}{when}", flush=True)
        if dry_run:
            print(f"[publish]   file {post.file} ({'ok' if post.file.exists() else 'MISSING'}), hashtags {' '.join(post.hashtags)}")
            continue
        if not post.file.exists():
            print(f"[publish] ✘ file not found: {post.file}", flush=True)
            notion.set_publication(post.page_id, PUB_ERROR, f"Fichier introuvable : {post.file}")
            continue
        try:
            for name in pending:
                url = clients[name].publish(post)
                # Saved per platform right away, so a later failure never re-uploads this one.
                notion.set_link(post.page_id, PLATFORMS[name][0], url)
                print(f"[publish]   {name}: {url}", flush=True)
        except QuotaExceeded as e:
            print(f"[publish] ■ {e} — stopping, the remaining shorts wait for the next run", flush=True)
            return
        except Exception as e:
            print(f"[publish] ✘ {e}", flush=True)
            notion.set_publication(post.page_id, PUB_ERROR, str(e)[:1900])
            continue
        notion.set_publication(post.page_id, PUB_PUBLISHED)
        print("[publish] ✔ published", flush=True)
