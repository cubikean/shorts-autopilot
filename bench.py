"""Offline benchmark for the highlight picker, graded on YouTube's "most replayed" curve.

The curve only exists ~10 days after release, so live grading (`feed.py replay`)
is slow. Older videos from the whitelisted channels already have it: picking
moments on them and grading the picks right away tells in minutes whether a
prompt, signal or model change helps.

Usage:
    python bench.py build [--per-channel 2] [--max-videos 40]   # once: transcripts, curves, signals
    python bench.py run full baseline [--holdout]              # grade variants, print the table
    python bench.py report [--holdout]                          # table of every variant already run

Variants: baseline (prompt before the comment/audio signals), bare (current prompt,
no signals), audio, comments, full (what the pipeline runs). LLM answers are cached
by prompt, so re-running an unchanged variant is free.

Caveat: comments on a weeks-old video are more numerous than the ones the
pipeline sees a few hours after release, so `comments` / `full` overstate that
signal. Compare against `audio` / `bare`, and confirm with `feed.py replay`.
About a third of the videos (by id) are held out: tune on the rest, check on
--holdout only once a variant is chosen, so the prompt isn't fitted to them.
"""
import argparse
import hashlib
import json
import subprocess
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Callable, Dict, List, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from shorts_generator.config import FEED_CLIPS_PER_VIDEO, FEED_SOURCES_FILE, LOCAL_OUTPUT_DIR

BENCH_DIR = Path(LOCAL_OUTPUT_DIR) / "bench"
RESULTS_DIR = BENCH_DIR / "results"
MIN_AGE_DAYS, MAX_AGE_DAYS = 12, 60
MIN_MINUTES, MAX_MINUTES = 3, 45          # longer videos cost Whisper time for little extra signal
BASELINE_COMMIT = "76a2d60"               # highlights.py before comment hot zones and audio markers
VARIANTS = ["baseline", "bare", "audio", "comments", "full"]


def _is_holdout(video_id: str) -> bool:
    return int(hashlib.sha1(video_id.encode()).hexdigest(), 16) % 3 == 0


# --- build ---------------------------------------------------------------------


def _candidates(handle: str, per_channel: int, now: datetime) -> List[Dict]:
    import yt_dlp  # type: ignore

    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True, "playlistend": 25}) as ydl:
        entries = ydl.extract_info(f"https://www.youtube.com/{handle}/videos", download=False).get("entries") or []
    picked = []
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
        for entry in entries:
            if len(picked) >= per_channel:
                break
            in_range = lambda d: MIN_MINUTES * 60 <= d <= MAX_MINUTES * 60
            if entry.get("duration") and not in_range(entry["duration"]):
                continue
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={entry['id']}", download=False)
            age = (now.timestamp() - (info.get("timestamp") or now.timestamp())) / 86400
            if age > MAX_AGE_DAYS:
                break  # uploads are newest first
            if age < MIN_AGE_DAYS or not in_range(info.get("duration") or 0) or not info.get("heatmap"):
                continue
            picked.append({
                "id": info["id"], "url": info["webpage_url"], "title": info.get("title"),
                "channel": info.get("channel"), "duration": info.get("duration"),
                "age_days": round(age, 1), "heatmap": info["heatmap"],
            })
    return picked


def _prepare(video: Dict) -> None:
    """Audio → transcript with audio markers + comment hot zones, cached in output/bench/<id>/."""
    import yt_dlp  # type: ignore

    from shorts_generator.local.signals import annotate_audio, comment_hotspots
    from shorts_generator.local.transcriber import transcribe_local

    folder = BENCH_DIR / video["id"]
    folder.mkdir(parents=True, exist_ok=True)
    stem = folder / f"bench_{video['id']}"
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "format": "bestaudio[ext=m4a]/bestaudio", "outtmpl": str(stem) + ".%(ext)s"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video["url"], download=True)
    audio = Path(ydl.prepare_filename(info))
    try:
        transcript = annotate_audio(str(audio), transcribe_local(str(audio)))
        hotspots = comment_hotspots(video["url"], str(audio), float(transcript["duration"]))
    finally:
        audio.unlink(missing_ok=True)  # the transcript and markers are all the benchmark needs
    (folder / "transcript.json").write_text(json.dumps(transcript, ensure_ascii=False), encoding="utf-8")
    (folder / "hotspots.json").write_text(json.dumps(hotspots, ensure_ascii=False), encoding="utf-8")
    (folder / "meta.json").write_text(json.dumps(video, ensure_ascii=False), encoding="utf-8")


def build(per_channel: int, max_videos: int) -> None:
    from shorts_generator.feed.sources import load_sources

    now = datetime.now(timezone.utc)
    have = {p.parent.name for p in BENCH_DIR.glob("*/meta.json")}
    added = 0
    for source in load_sources(FEED_SOURCES_FILE)["youtube"]:
        if len(have) >= max_videos:
            break
        handle = source.get("handle") or source.get("channel_id")
        handle = handle if str(handle).startswith(("@", "channel/")) else f"channel/{handle}"
        try:
            videos = _candidates(handle, per_channel, now)
        except Exception as e:
            print(f"[bench] {handle}: {e}", flush=True)
            continue
        for video in videos:
            if video["id"] in have or len(have) >= max_videos:
                continue
            print(f"[bench] + {handle} · {video['title'][:60]} ({video['duration'] // 60} min, {video['age_days']} d)", flush=True)
            try:
                _prepare(video)
            except Exception as e:
                print(f"[bench]   skipped: {e}", flush=True)
                continue
            have.add(video["id"])
            added += 1
    held = sum(1 for v in have if _is_holdout(v))
    print(f"[bench] {added} video(s) added; {len(have)} in the benchmark ({len(have) - held} dev, {held} holdout)")


# --- run -----------------------------------------------------------------------


def _baseline_module() -> types.ModuleType:
    source = subprocess.run(
        ["git", "show", f"{BASELINE_COMMIT}:shorts_generator/highlights.py"],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout
    module = types.ModuleType("bench_baseline_highlights")
    exec(compile(source, f"{BASELINE_COMMIT}/highlights.py", "exec"), module.__dict__)
    return module


def _without_markers(transcript: Dict) -> Dict:
    return {**transcript, "segments": [{k: v for k, v in s.items() if k != "markers"} for s in transcript["segments"]]}


def _picker(variant: str) -> Callable[[Dict, List[Dict], Callable], List[Dict]]:
    from shorts_generator.highlights import get_highlights

    if variant == "baseline":
        old = _baseline_module()
        return lambda tr, hs, llm: old.get_highlights(_without_markers(tr), num_clips=FEED_CLIPS_PER_VIDEO, llm_fn=llm)["highlights"]
    audio, comments = {"bare": (False, False), "audio": (True, False), "comments": (False, True), "full": (True, True)}[variant]
    return lambda tr, hs, llm: get_highlights(
        tr if audio else _without_markers(tr), num_clips=FEED_CLIPS_PER_VIDEO, llm_fn=llm, hotspots=hs if comments else None,
    )["highlights"]


def _videos(holdout: bool) -> List[Path]:
    return sorted(p.parent for p in BENCH_DIR.glob("*/meta.json") if _is_holdout(p.parent.name) == holdout)


def run(variants: List[str], holdout: bool) -> None:
    from shorts_generator.feed.replay import grade
    from shorts_generator.local.llm import call_local_llm

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    folders = _videos(holdout)
    split = "holdout" if holdout else "dev"
    for variant in variants:
        pick = _picker(variant)
        results = {}
        for i, folder in enumerate(folders, 1):
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
            transcript = json.loads((folder / "transcript.json").read_text(encoding="utf-8"))
            hotspots = json.loads((folder / "hotspots.json").read_text(encoding="utf-8"))
            print(f"[bench] {variant} {i}/{len(folders)} · {meta['title'][:60]}", flush=True)
            try:
                highlights = sorted(pick(transcript, hotspots, call_local_llm), key=lambda h: -int(h.get("score", 0)))
            except Exception as e:
                print(f"[bench]   failed: {e}", flush=True)
                continue
            picks = []
            for h in highlights[:max(1, FEED_CLIPS_PER_VIDEO)]:
                g = grade(meta["heatmap"], float(h["start_time"]), float(h["end_time"]))
                picks.append({"start": h["start_time"], "end": h["end_time"], "title": h.get("title"), "percentile": g["percentile"]})
            results[meta["id"]] = picks
            print(f"[bench]   centile(s): {', '.join(str(p['percentile']) for p in picks)}", flush=True)
        (RESULTS_DIR / f"{variant}.{split}.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    report(holdout)


def report(holdout: bool) -> None:
    split = "holdout" if holdout else "dev"
    runs = {p.name.split(".")[0]: json.loads(p.read_text(encoding="utf-8")) for p in sorted(RESULTS_DIR.glob(f"*.{split}.json"))}
    if not runs:
        print(f"[bench] no {split} results yet: python bench.py run full baseline" + (" --holdout" if holdout else ""))
        return
    # Compare variants on the videos they all graded, or a failed call would skew one of them.
    common = set.intersection(*(set(r) for r in runs.values()))
    print(f"\n{split}: {len(common)} video(s) graded by every variant — centile: 100 = most replayed window, 50 = random\n")
    print(f"{'variant':10} {'#1 median':>10} {'#1 mean':>8} {'#1 top 10 %':>12} {'#1 below 30':>12} {'all picks mean':>15}")
    for variant in sorted(runs, key=lambda v: VARIANTS.index(v) if v in VARIANTS else 99):
        firsts = [runs[variant][vid][0]["percentile"] for vid in common if runs[variant][vid]]
        every = [p["percentile"] for vid in common for p in runs[variant][vid]]
        if not firsts:
            continue
        top = sum(1 for v in firsts if v >= 90)
        low = sum(1 for v in firsts if v < 30)
        print(f"{variant:10} {median(firsts):10.1f} {mean(firsts):8.1f} {f'{top}/{len(firsts)}':>12} {f'{low}/{len(firsts)}':>12} {mean(every):15.1f}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark the highlight picker on YouTube's most-replayed curve")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="collect videos with a curve, transcribe them, cache the signals")
    b.add_argument("--per-channel", type=int, default=2)
    b.add_argument("--max-videos", type=int, default=40)
    r = sub.add_parser("run", help="pick moments with each variant and grade them")
    r.add_argument("variants", nargs="+", choices=VARIANTS)
    r.add_argument("--holdout", action="store_true", help="use the held-out videos (only to confirm a chosen variant)")
    rep = sub.add_parser("report", help="table of the variants already run")
    rep.add_argument("--holdout", action="store_true")
    args = parser.parse_args()
    if args.command == "build":
        build(args.per_channel, args.max_videos)
    elif args.command == "run":
        run(args.variants, args.holdout)
    else:
        report(args.holdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
