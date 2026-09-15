"""Platform-agnostic pieces shared by every publisher."""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional


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


class QuotaExceeded(RuntimeError):
    """The platform refuses more uploads for now: stop the run, leave the queue as is."""


class Publisher:
    """Uploads one short to one platform and returns the public URL of the post."""

    def publish(self, post: ShortPost) -> str:
        raise NotImplementedError
