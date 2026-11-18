"""`chatlog`: record the live chat of the watched Twitch channels.

The data a chat-spike detector would be tuned on: which emotes and message
rates come just before the moments viewers clip. Big French channels keep
their VODs (and so their chat replay) behind a subscription, so the chat has
to be recorded while it happens.

Anonymous IRC (no account, no token), one JSON line per message under
output/chat/<channel>/<date>.jsonl: send time (ms), a short hash of the
chatter (enough to count distinct people, without storing who they are),
the text, and Twitch's emote tag. BTTV / 7TV / FFZ emotes are plain words
in the text.
"""
import hashlib
import json
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

from ..config import FEED_SOURCES_FILE, LOCAL_OUTPUT_DIR
from .sources import load_sources

CHAT_DIR = Path(LOCAL_OUTPUT_DIR) / "chat"
HOST, PORT = "irc.chat.twitch.tv", 6667
REFRESH_SECONDS = 300       # how often the list of channels to follow is recomputed
JOIN_PER_BATCH = 15         # Twitch allows 20 JOINs per 10 s for anonymous users


def _parse(line: str) -> Optional[Dict]:
    """One PRIVMSG with tags → {channel, t, u, m, e}; anything else → None."""
    if not line.startswith("@") or " PRIVMSG #" not in line:
        return None
    raw_tags, _, rest = line[1:].partition(" ")
    tags = dict(tag.partition("=")[::2] for tag in raw_tags.split(";"))
    prefix, _, rest = rest.partition(" PRIVMSG #")
    channel, _, text = rest.partition(" :")
    login = prefix.lstrip(":").partition("!")[0]
    return {
        "channel": channel,
        "t": int(tags.get("tmi-sent-ts") or time.time() * 1000),
        "u": hashlib.sha1(login.encode()).hexdigest()[:10],
        "m": text,
        "e": tags.get("emotes") or "",
    }


def _channels() -> List[str]:
    from .twitch import _app_token, _top_live

    sources = load_sources(FEED_SOURCES_FILE)
    logins = [str(s["login"]).lower() for s in sources["twitch"] if s.get("login")]
    if sources["twitch_categories"]:
        logins += _top_live(_app_token(), sources["twitch_categories"], datetime.now(timezone.utc))
    return list(dict.fromkeys(logins))


def _append(message: Dict) -> None:
    day = datetime.fromtimestamp(message["t"] / 1000).strftime("%Y-%m-%d")
    path = CHAT_DIR / message.pop("channel") / f"{day}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(message, ensure_ascii=False) + "\n")


def _session(joined: Set[str]) -> None:
    """One IRC connection; returns (or raises) when it drops, so the caller reconnects."""
    sock = socket.create_connection((HOST, PORT), timeout=30)
    sock.settimeout(30)
    send = lambda text: sock.sendall((text + "\r\n").encode("utf-8"))
    send("CAP REQ :twitch.tv/tags")
    send("PASS SCHMOOPIIE")
    send(f"NICK justinfan{int(time.time()) % 100000}")
    joined.clear()
    next_refresh, buffer, counts = 0.0, b"", 0
    while True:
        if time.time() >= next_refresh:
            try:
                wanted = _channels()
            except Exception as e:
                print(f"[chatlog] channel list not refreshed: {e}", flush=True)
                wanted = sorted(joined)
            new = [c for c in wanted if c not in joined]
            for i in range(0, len(new), JOIN_PER_BATCH):
                send("JOIN " + ",".join("#" + c for c in new[i:i + JOIN_PER_BATCH]))
                joined.update(new[i:i + JOIN_PER_BATCH])
                if i + JOIN_PER_BATCH < len(new):
                    time.sleep(11)
            print(f"[chatlog] {datetime.now():%H:%M} following {len(joined)} channel(s): {', '.join(sorted(joined))}"
                  + (f" — {counts} message(s) since last refresh" if counts else ""), flush=True)
            next_refresh, counts = time.time() + REFRESH_SECONDS, 0
        try:
            data = sock.recv(65536)
        except socket.timeout:
            continue
        if not data:
            raise ConnectionError("connection closed by Twitch")
        buffer += data
        *lines, buffer = buffer.split(b"\r\n")
        for raw in lines:
            line = raw.decode("utf-8", errors="replace")
            if line.startswith("PING"):
                send("PONG" + line[4:])
            elif " RECONNECT" in line:
                raise ConnectionError("Twitch asked to reconnect")
            else:
                message = _parse(line)
                if message:
                    _append(message)
                    counts += 1


def run() -> None:
    """Record until interrupted, reconnecting after any network error."""
    joined: Set[str] = set()
    delay = 5
    while True:
        started = time.time()
        try:
            _session(joined)
        except KeyboardInterrupt:
            return
        except Exception as e:
            print(f"[chatlog] disconnected: {e}; reconnecting in {delay}s", flush=True)
        delay = 5 if time.time() - started > 300 else min(delay * 2, 300)
        time.sleep(delay)
