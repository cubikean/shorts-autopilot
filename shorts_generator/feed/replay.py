"""`replay`: grade each short's moment against its source's "most replayed" curve.

YouTube publishes that curve (the heatmap above the progress bar) only ~10 days
after release, too late to pick moments with. Once it exists it is ground truth:
this scores where the chosen [start, end] ranks among every window of the same
length in the source, and writes it to Notion:
  - "Centile revu": 100 = the most replayed window of that length, 50 = median
  - "Pic revu": the same in words, plus where the real peak was
The summary groups shorts by week of creation, to see whether prompt changes help.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Dict, List, Optional

from ..config import NOTION_SHORTS_DB, NOTION_TOKEN
from .notion import Notion

MIN_AGE_DAYS = 10    # the curve shows up 7-10 days after release
GIVE_UP_DAYS = 30    # still no curve by then: the source never got enough views for one
WINDOW_STEP_SECONDS = 2.0
INTRO_SECONDS, INTRO_SHARE = 60.0, 0.05  # opening flattened before grading: min(60 s, 5 % of the video)


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}" if seconds >= 3600 else f"{seconds // 60}:{seconds % 60:02d}"


def fetch_heatmap(url: str) -> List[Dict]:
    import yt_dlp  # type: ignore

    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
        return ydl.extract_info(url, download=False).get("heatmap") or []


def _window_mean(heatmap: List[Dict], start: float, end: float) -> float:
    """Time-weighted mean of the curve over [start, end]."""
    total = covered = 0.0
    for point in heatmap:
        overlap = min(end, point["end_time"]) - max(start, point["start_time"])
        if overlap > 0:
            total += overlap * point["value"]
            covered += overlap
    return total / covered if covered else 0.0


def grade(heatmap: List[Dict], start: float, end: float) -> Dict:
    """Percentile of [start, end] among all same-length windows, and the best such window."""
    duration = heatmap[-1]["end_time"]
    # Everyone watches the opening, so the curve always bumps there: it isn't a replayed moment.
    # Flatten it to the video's median, for the chosen clip and the windows it is compared with alike.
    intro_end = min(INTRO_SECONDS, INTRO_SHARE * duration)
    typical = median(p["value"] for p in heatmap)
    heatmap = [{**p, "value": min(p["value"], typical)} if p["start_time"] < intro_end else p for p in heatmap]
    length = min(end - start, duration)
    start = min(max(0.0, start), duration - length)
    clip = _window_mean(heatmap, start, start + length)
    windows = []
    t = 0.0
    while t + length <= duration:
        windows.append((t, _window_mean(heatmap, t, t + length)))
        t += WINDOW_STEP_SECONDS
    best_start, best_value = max(windows, key=lambda w: w[1])
    beaten = sum(1 for _, value in windows if value <= clip)
    return {
        "percentile": round(100 * beaten / len(windows)),
        "clip_value": clip,
        "best_start": best_start,
        "best_end": best_start + length,
        "best_value": best_value,
    }


def _describe(g: Dict, start: float, end: float) -> str:
    rank = "le passage le plus revu" if g["percentile"] >= 99 else f"centile {g['percentile']}/100"
    overlap = min(end, g["best_end"]) - max(start, g["best_start"])
    peak = "pile sur le pic" if overlap > 0.5 * (end - start) else f"pic à {_clock(g['best_start'])}-{_clock(g['best_end'])}"
    return f"{rank} · {peak} (intensité {g['clip_value']:.2f} vs {g['best_value']:.2f})"


def _age_days(created: str) -> float:
    return (datetime.now(timezone.utc) - datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds() / 86400


def print_summary(notion: Notion) -> None:
    rated = notion.rated_shorts(NOTION_SHORTS_DB)
    if not rated:
        print("[replay] no short rated yet")
        return
    weeks: Dict[str, List[float]] = defaultdict(list)
    for row in rated:
        day = datetime.fromisoformat(row["created"].replace("Z", "+00:00")).date()
        weeks[str(day - timedelta(days=day.weekday()))].append(row["percentile"])
    print("[replay] moment chosen vs the real most replayed one (centile: 100 = best window, 50 = random pick)")
    for week in sorted(weeks):
        values = weeks[week]
        hits = sum(1 for v in values if v >= 90)
        print(f"  week of {week}: {len(values):3} short(s), median centile {median(values):5.1f}, top 10 % for {hits}/{len(values)}")
    every = [row["percentile"] for row in rated]
    print(f"  all: {len(every)} short(s), median centile {median(every):.1f}")


def check_replays(dry_run: bool = False) -> None:
    if not NOTION_SHORTS_DB:
        raise RuntimeError("NOTION_SHORTS_DB is not set.")
    notion = Notion(NOTION_TOKEN)
    notion.ensure_columns(NOTION_SHORTS_DB, {"Pic revu": {"rich_text": {}}, "Centile revu": {"number": {"format": "number"}}})
    rows = notion.shorts_to_rate(NOTION_SHORTS_DB, MIN_AGE_DAYS)
    print(f"[replay] {len(rows)} short(s) old enough to grade")

    by_video: Dict[str, List[Dict]] = defaultdict(list)
    for row in rows:
        by_video[row["video_page_id"]].append(row)

    graded = 0
    for video_page_id, shorts in by_video.items():
        source = notion.video_source(video_page_id)
        verdict: Optional[str] = None
        heatmap: List[Dict] = []
        if source["platform"] != "YouTube" or not source["url"]:
            verdict = f"{source['platform'] or 'source'} : pas de courbe de revisionnage"
        else:
            try:
                heatmap = fetch_heatmap(source["url"])
            except Exception as e:
                print(f"[replay] {source['url']}: {e}", flush=True)
                continue
            if not heatmap:
                if all(_age_days(s["created"]) < GIVE_UP_DAYS for s in shorts):
                    print(f"[replay] {source['url']}: curve not published yet, retrying next run", flush=True)
                    continue
                verdict = "courbe jamais publiée par YouTube"
        for short in shorts:
            if verdict:
                text, percentile = verdict, None
            else:
                g = grade(heatmap, short["start"], short["end"])
                text, percentile = _describe(g, short["start"], short["end"]), g["percentile"]
                graded += 1
            print(f"  {short['title'][:50]:50}  {text}", flush=True)
            if not dry_run:
                notion.set_replay(short["page_id"], text, percentile)
    print(f"[replay] {graded} short(s) graded" + (" (dry run, Notion untouched)" if dry_run else ""))
    print_summary(notion)
