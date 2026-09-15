"""Daily viral-video feed: whitelisted channels → Notion queue → rendered shorts.

Usage:
    python feed.py setup-notion "<Notion page URL shared with your integration>"
    python feed.py discover [--dry-run]
    python feed.py process [--limit 3]
    python feed.py setup-publish          # add publishing columns to an existing Shorts database
    python feed.py auth-youtube           # one-time OAuth consent for the upload channel
    python feed.py auth-tiktok            # one-time OAuth consent for TikTok drafts
    python feed.py notify-test            # test phone notification (ntfy)
    python feed.py publish [--limit 1] [--platform youtube] [--dry-run]
"""
import argparse
import os
import sys
from contextlib import contextmanager
from pathlib import Path

# Windows consoles default to 'charmap'; keep Unicode titles printable.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from shorts_generator.config import (  # noqa: E402
    FEED_MAX_PER_RUN,
    LOCAL_OUTPUT_DIR,
    NOTION_SHORTS_DB,
    NOTION_TOKEN,
    PUBLISH_MAX_PER_RUN,
)

LOCK_DIR = Path(LOCAL_OUTPUT_DIR) / "locks"


class AlreadyRunning(Exception):
    pass


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        # os.kill(pid, 0) would terminate the process on Windows: query it instead.
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@contextmanager
def single_run(command: str):
    """One `feed.py <command>` at a time: a manual run and the scheduled task must not overlap
    (two publish runs could upload the same short; process would requeue a video mid-render)."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock = LOCK_DIR / f"{command}.lock"
    try:
        pid = int(lock.read_text().strip())
    except (OSError, ValueError):
        pid = 0
    if pid and pid != os.getpid() and _pid_alive(pid):
        raise AlreadyRunning(f"`feed.py {command}` is already running (pid {pid}); skipping this run")
    lock.write_text(str(os.getpid()))
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily viral-video feed for the shorts generator")
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup-notion", help="create the Notion databases under a page")
    setup.add_argument("page", help="URL or id of a Notion page shared with the integration")

    disc = sub.add_parser("discover", help="find fresh viral videos and queue them in Notion")
    disc.add_argument("--dry-run", action="store_true", help="print candidates without writing to Notion")

    proc = sub.add_parser("process", help="render shorts for the top queued videos")
    proc.add_argument("--limit", type=int, default=FEED_MAX_PER_RUN, help=f"videos per run (default {FEED_MAX_PER_RUN})")

    sub.add_parser("setup-publish", help="add the publishing columns/options to the Notion Shorts database")
    sub.add_parser("auth-youtube", help="authorise uploads to your YouTube channel (opens a browser once)")
    sub.add_parser("auth-tiktok", help="authorise draft uploads to your TikTok account (opens a browser once)")
    sub.add_parser("notify-test", help="send a test notification to your phone (ntfy)")

    pub = sub.add_parser("publish", help="upload the shorts marked Validé in Notion")
    pub.add_argument("--limit", type=int, default=PUBLISH_MAX_PER_RUN, help=f"shorts per run (default {PUBLISH_MAX_PER_RUN})")
    pub.add_argument("--platform", action="append", help="only this platform (repeatable; default PUBLISH_PLATFORMS)")
    pub.add_argument("--dry-run", action="store_true", help="list what would be published, upload nothing")

    args = parser.parse_args()
    try:
        if args.command == "setup-notion":
            from shorts_generator.feed.notion import Notion, page_id_from
            from shorts_generator.publish.runner import link_columns

            notion = Notion(NOTION_TOKEN)
            videos_id, shorts_id = notion.create_databases(page_id_from(args.page))
            notion.ensure_publish_schema(shorts_id, link_columns())
            print("Databases created. Add these lines to .env:")
            print(f"NOTION_VIDEOS_DB={videos_id}")
            print(f"NOTION_SHORTS_DB={shorts_id}")
        elif args.command == "setup-publish":
            from shorts_generator.feed.notion import Notion
            from shorts_generator.publish.runner import link_columns

            if not NOTION_SHORTS_DB:
                raise RuntimeError("NOTION_SHORTS_DB is not set.")
            changes = Notion(NOTION_TOKEN).ensure_publish_schema(NOTION_SHORTS_DB, link_columns())
            print("\n".join(f"  {c}" for c in changes) if changes else "Shorts database already up to date.")
        elif args.command == "auth-youtube":
            from shorts_generator.publish.youtube import authorize

            authorize()
        elif args.command == "auth-tiktok":
            from shorts_generator.publish.tiktok import authorize

            authorize()
        elif args.command == "notify-test":
            from shorts_generator import notify
            from shorts_generator.config import NTFY_TOPIC

            if not NTFY_TOPIC:
                raise RuntimeError("NTFY_TOPIC is not set in .env.")
            sample = "titre du short 🔥\n\ndescription du short, avec des accents é à ç\n\n#hashtag #shorts"
            if not notify.send("Test Shorts Autopilot", sample, copy=sample, copy_label="Copier la légende",
                               open_url="https://www.tiktok.com/", open_label="Ouvrir TikTok"):
                raise RuntimeError("notification not sent (see above)")
            print(f"Test notification sent to ntfy topic {NTFY_TOPIC}")
        elif args.command == "publish":
            from shorts_generator.publish.runner import publish

            if args.dry_run:
                publish(limit=args.limit, platforms=args.platform, dry_run=True)
            else:
                with single_run("publish"):
                    publish(limit=args.limit, platforms=args.platform)
        elif args.command == "discover":
            from shorts_generator.feed.runner import discover

            if args.dry_run:
                discover(dry_run=True)
            else:
                with single_run("discover"):
                    discover()
        elif args.command == "process":
            from shorts_generator.feed.runner import process

            with single_run("process"):
                process(limit=args.limit)
    except AlreadyRunning as e:
        print(f"[feed] {e}")
        return 0
    except Exception as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
