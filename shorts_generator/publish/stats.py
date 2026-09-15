"""`stats`: refresh the "Stats" column of every posted short, once a day.

Each platform's Publisher.fetch_stats reads the numbers; the column gets one
line per platform plus the refresh time. A platform that fails this run (e.g.
missing read permission) keeps its previous line instead of being wiped.
"""
from datetime import datetime
from typing import Dict, List, Optional

from ..config import NOTION_SHORTS_DB, NOTION_TOKEN, PUBLISH_PLATFORMS
from ..feed.notion import Notion
from .base import PostStats
from .runner import PLATFORMS, _post, link_columns


def _number(value: Optional[int]) -> str:
    return f"{value:,}".replace(",", " ")


def format_line(column: str, stats: PostStats) -> str:
    parts = [f"{_number(stats.views)} vues"] if stats.views is not None else []
    for value, label in ((stats.likes, "j'aime"), (stats.comments, "com."), (stats.shares, "partages")):
        if value is not None:
            parts.append(f"{_number(value)} {label}")
    return f"{column} : " + (" · ".join(parts) or "pas de stats")


def update_stats(platforms: Optional[List[str]] = None) -> None:
    names = platforms or PUBLISH_PLATFORMS
    if not NOTION_SHORTS_DB:
        raise RuntimeError("NOTION_SHORTS_DB is not set.")
    notion = Notion(NOTION_TOKEN)
    rows = notion.shorts_with_links(NOTION_SHORTS_DB, link_columns(names))
    print(f"[stats] {len(rows)} posted short(s)")
    if not rows:
        return

    found: Dict[str, Dict[str, PostStats]] = {}  # platform → page id → stats
    for name in names:
        column = PLATFORMS[name][0]
        items = [(row["page_id"], row["links"][column], _post(row)) for row in rows if row["links"].get(column)]
        if not items:
            continue
        try:
            found[name] = PLATFORMS[name][1]().fetch_stats(items)
            print(f"[stats] {name}: numbers for {len(found[name])}/{len(items)} post(s)", flush=True)
        except Exception as e:
            print(f"[stats] {name} skipped this run: {e}", flush=True)

    stamp = f"Mis à jour le {datetime.now():%d/%m %H:%M}"
    updated = 0
    for row in rows:
        previous = {line.split(" : ", 1)[0]: line for line in row["stats"].splitlines() if " : " in line}
        lines, links = [], {}
        for name in names:
            column = PLATFORMS[name][0]
            stats = found.get(name, {}).get(row["page_id"])
            if stats:
                lines.append(format_line(column, stats))
                if stats.url and stats.url != row["links"].get(column):
                    links[column] = stats.url
            elif column in previous:
                lines.append(previous[column])  # platform failed or post not found: keep the last numbers
        if lines:
            notion.set_stats(row["page_id"], "\n".join(lines + [stamp]), links)
            updated += 1
    print(f"[stats] ✔ {updated} short(s) updated in Notion", flush=True)
