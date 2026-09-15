"""Phone notifications through ntfy (https://ntfy.sh): free, no account, the topic is the password.

Install the ntfy app and subscribe to NTFY_TOPIC: the pipeline pushes whatever
needs a human step, e.g. the TikTok caption to paste, with a one-tap copy
button (Android and web apps; on iOS copy the text from the notification).
"""
from typing import Optional

import requests

from .config import NTFY_SERVER, NTFY_TOPIC

MAX_MESSAGE_BYTES = 3500  # ntfy turns messages over 4096 bytes into file attachments


def send(
    title: str,
    message: str,
    copy: Optional[str] = None,
    copy_label: str = "Copier",
    open_url: Optional[str] = None,
    open_label: str = "Ouvrir",
) -> bool:
    """Push a notification. Returns False (and logs) instead of raising, so it never breaks a run."""
    if not NTFY_TOPIC:
        return False
    actions = []
    if copy:
        actions.append({"action": "copy", "label": copy_label, "value": copy})
    if open_url:
        actions.append({"action": "view", "label": open_label, "url": open_url})
    body = {
        "topic": NTFY_TOPIC,
        "title": title,
        # JSON publishing keeps emojis and accents intact (headers would need RFC 2047 encoding).
        "message": message.encode("utf-8")[:MAX_MESSAGE_BYTES].decode("utf-8", errors="ignore"),
        "tags": ["clapper"],
        "actions": actions,
    }
    try:
        resp = requests.post(NTFY_SERVER, json=body, timeout=15)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"[notify] ntfy notification failed: {e}", flush=True)
        return False
