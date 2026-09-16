"""`publish`: upload the shorts validated in Notion to every enabled platform.

Notion workflow on the Shorts database:
  Publication "À publier" → you set "Validé" → each platform's URL column gets
  filled → "Publié". A failure sets "Erreur" with the reason in "Erreur publication";
  set "Validé" again to retry. Platforms already published are never redone.
A platform that can't start (missing token) or runs out of quota is skipped for
the rest of the run; the others carry on and the short stays "Validé" until all are done.
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


def _tiktok() -> Publisher:
    from .tiktok import TikTokPublisher

    return TikTokPublisher()


# Platform key → (Notion URL column holding the post link, publisher factory).
# A new platform = one module with a Publisher + one entry here.
PLATFORMS: Dict[str, Tuple[str, Callable[[], Publisher]]] = {
    "youtube": ("YouTube", _youtube),
    "tiktok": ("TikTok", _tiktok),
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
    # A short is Publié only once every enabled platform has it, even when --platform narrows the run.
    required = list(dict.fromkeys([*PUBLISH_PLATFORMS, *names]))
    unknown = [name for name in required if name not in PLATFORMS]
    if unknown:
        raise RuntimeError(f"unknown platform(s) {unknown}; supported: {', '.join(PLATFORMS)}")
    if not NOTION_SHORTS_DB:
        raise RuntimeError("NOTION_SHORTS_DB is not set. Run `python feed.py setup-notion <page>` and add the ids to .env.")

    notion = Notion(NOTION_TOKEN)
    # Fetch past the limit: a short blocked on one platform (quota) mustn't starve the others.
    rows = notion.validated_shorts(NOTION_SHORTS_DB, link_columns(required), 100)
    print(f"[publish] {len(rows)} validated short(s) to publish on {', '.join(names)} (up to {limit} this run)")
    if not rows:
        return

    clients: Dict[str, Publisher] = {}
    skipped: Dict[str, str] = {}  # platform → why it sits out the rest of this run
    if not dry_run:
        for name in names:
            try:
                clients[name] = PLATFORMS[name][1]()
            except Exception as e:
                skipped[name] = str(e)
                print(f"[publish] {name} unavailable this run: {e}", flush=True)
        if not clients:
            raise RuntimeError("no platform available: " + "; ".join(f"{n}: {why}" for n, why in skipped.items()))

    handled = 0  # shorts that had an upload attempted; only these count toward the limit
    for row in rows:
        if handled >= limit:
            break
        post = _post(row)
        done = {name for name in required if row["links"].get(PLATFORMS[name][0])}
        pending = [name for name in names if name not in done]
        if not pending:
            continue
        when = f" · programmé {post.publish_at:%Y-%m-%d %H:%M}" if post.publish_at else ""
        print(f"\n[publish] ▶ {post.title} → {', '.join(pending)}{when}", flush=True)
        if dry_run:
            print(f"[publish]   file {post.file} ({'ok' if post.file.exists() else 'MISSING'}), hashtags {' '.join(post.hashtags)}")
            handled += 1
            continue
        runnable = [name for name in pending if name not in skipped]
        if not runnable:
            print(f"[publish]   waiting: {', '.join(pending)} unavailable this run", flush=True)
            if len(skipped) == len(names):
                return
            continue
        if not post.file.exists():
            print(f"[publish] ✘ file not found: {post.file}", flush=True)
            notion.set_publication(post.page_id, PUB_ERROR, f"Fichier introuvable : {post.file}")
            continue

        failure = None
        attempted = False
        for name in runnable:
            try:
                url = clients[name].publish(post)
            except QuotaExceeded as e:
                skipped[name] = str(e)
                print(f"[publish] ■ {name}: {e} — skipped for the rest of this run", flush=True)
                continue
            except Exception as e:
                failure = f"{name}: {e}"
                print(f"[publish] ✘ {failure}", flush=True)
                attempted = True
                break
            attempted = True
            # Saved per platform right away, so a later failure never re-uploads this one.
            notion.set_link(post.page_id, PLATFORMS[name][0], url)
            done.add(name)
            print(f"[publish]   {name}: {url}", flush=True)
        handled += attempted

        if failure:
            notion.set_publication(post.page_id, PUB_ERROR, failure[:1900])
        elif all(name in done for name in required):
            notion.set_publication(post.page_id, PUB_PUBLISHED)
            print("[publish] ✔ published", flush=True)
        else:
            print(f"[publish]   still waiting for: {', '.join(n for n in required if n not in done)}", flush=True)
