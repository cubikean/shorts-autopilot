"""Platform-agnostic pieces shared by every publisher."""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class ShortPost:
    """One validated short, as read from its Notion row."""
    page_id: str
    title: str
    hook: str
    description: str
    hashtags: List[str]
    file: Path
    publish_at: Optional[datetime] = None  # future date = scheduled release


@dataclass
class PostStats:
    """Engagement numbers of one post (None = not reported by the platform)."""
    views: Optional[int] = None
    likes: Optional[int] = None
    comments: Optional[int] = None
    shares: Optional[int] = None
    url: Optional[str] = None  # better link than the stored one (e.g. the TikTok video found by caption)


class QuotaExceeded(RuntimeError):
    """The platform refuses more uploads for now: stop the run, leave the queue as is."""


class Publisher:
    """Uploads one short to one platform and returns the public URL of the post."""

    def publish(self, post: ShortPost) -> str:
        raise NotImplementedError

    def fetch_stats(self, items: List[Tuple[str, str, ShortPost]]) -> Dict[str, PostStats]:
        """(Notion page id, stored link, post) → stats for every post this platform can find."""
        return {}
