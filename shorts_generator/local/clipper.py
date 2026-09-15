"""Local clipping: ffmpeg subclip + face-aware vertical crop.

Two stages per highlight:
  1. Cut the source video to [start, end] with ffmpeg (re-encoded, audio kept).
  2. Reframe the cut to the target aspect ratio. A stable crop path is planned
     over the whole clip first (YuNet faces, shot cuts, locked or dead-zone
     camera — see reframe.py), then frames are piped into a single x264 encode.
"""
import os
import subprocess
from typing import Dict, List, Optional

import numpy as np

from ..config import LOCAL_OUTPUT_DIR
from .reframe import STACK_TOP_SHARE, analyse, plan_crop_path, plan_layout
from .subtitles import clip_words, write_ass


def _ratio(aspect_ratio: str) -> float:
    """Parse '9:16' → 9/16, '1:1' → 1.0."""
    try:
        w, h = aspect_ratio.split(":")
        return float(w) / float(h)
    except (ValueError, ZeroDivisionError):
        return 9.0 / 16.0


def _cut_subclip(source_path: str, start: float, end: float, out_path: str) -> str:
    """ffmpeg -ss start -t duration → re-encoded mp4 with audio.

    -ss before -i seeks straight to the keyframe instead of decoding the whole
    video up to `start`; re-encoding keeps the cut frame-accurate.
    """
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start:.3f}",
        "-i", source_path,
        "-t", f"{end - start:.3f}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        out_path,
    ]
    subprocess.run(cmd, check=True)
    return out_path


def _reframe_vertical(
    in_path: str,
    out_path: str,
    aspect_ratio: str,
    words: Optional[List[Dict]] = None,
    layout: str = "auto",
) -> str:
    """Crop the cut clip to the target aspect ratio, tracking faces if possible.

    For tall outputs, `layout` "auto" stacks a detected stream webcam on top of
    the content ("stack" forces it for any steady face, "single" disables it).
    When `words` is given, captions are burned in during the final encode.
    """
    try:
        import cv2  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "opencv-python is required. Install it with:\n"
            "    pip install -r requirements.txt"
        ) from e

    target_ratio = _ratio(aspect_ratio)
    cap = cv2.VideoCapture(in_path)
    if not cap.isOpened():
        raise RuntimeError(f"could not open {in_path}")

    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    # Compute the largest crop that fits inside the frame at the target ratio.
    if target_ratio < src_w / src_h:
        crop_h = src_h
        crop_w = int(crop_h * target_ratio)
    else:
        crop_w = src_w
        crop_h = int(crop_w / target_ratio)
    crop_w = max(2, crop_w - (crop_w % 2))
    crop_h = max(2, crop_h - (crop_h % 2))

    cap.release()

    # Pass 1: plan the whole crop path with hindsight (see reframe.py).
    analysis = analyse(cv2, in_path)
    xs, ys, stats = plan_crop_path(analysis, crop_w, crop_h)
    print(
        f"[reframe] {stats['detector']}: faces in {stats['face_rate']:.0%} of samples, "
        f"{stats['shots']} shots ({stats['locked']} locked, {stats['faceless']} without face)",
        flush=True,
    )

    # Tall outputs render at the source height as width (1080x1920 from 1080p):
    # the raw 9:16 crop of a landscape frame (608x1080) looks soft on phones, and
    # the stacked layout needs the room so the small webcam overlay isn't a thumbnail.
    out_w, out_h, top_h = crop_w, crop_h, 0
    segments = [(0, len(xs), None)]
    if target_ratio < 0.8:
        out_w = min(1080, src_h - src_h % 2)
        out_h = round(out_w / target_ratio)
        out_h -= out_h % 2
    if target_ratio < 0.8 and layout != "single":
        panel_h = round(out_h * STACK_TOP_SHARE)
        panel_h -= panel_h % 2
        planned = plan_layout(analysis, top_aspect=out_w / panel_h, mode=layout)
        if any(box for _, _, box in planned):
            top_h, segments = panel_h, planned
            stacked_frames = sum(end - start for start, end, box in planned if box)
            print(
                f"[layout] webcam stacked on top for {stacked_frames / max(1, len(xs)):.0%} of the clip "
                f"({len(planned)} segment{'s' if len(planned) > 1 else ''})",
                flush=True,
            )
    stacked = top_h > 0
    resize = (out_w, out_h) != (crop_w, crop_h)
    bottom_h = out_h - top_h
    content_w = min(src_w, round(src_h * out_w / max(1, bottom_h)))
    content_h = src_h if content_w < src_w else round(src_w * bottom_h / out_w)
    content_x, content_y = (src_w - content_w) // 2, (src_h - content_h) // 2
    segment_of = np.zeros(len(xs), dtype=int)
    for i, (start, end, _) in enumerate(segments):
        segment_of[start:end] = i

    # Pass 2: pipe composed frames straight into one x264 encode that also muxes
    # the audio and burns captions (no lossy mp4v intermediate).
    vf_args: List[str] = []
    ass_path = None
    if words:
        ass_path = os.path.abspath(out_path + ".ass")
        # Stacked: captions ride the seam between webcam and content.
        write_ass(words, out_w, out_h, frame_count / fps, ass_path, position=top_h / out_h if stacked else None)
        # Run ffmpeg from the .ass folder and pass a bare filename: Windows drive
        # colons would otherwise need filter-graph escaping.
        vf_args = ["-vf", f"ass={os.path.basename(ass_path)}"]
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{out_w}x{out_h}", "-r", f"{fps:.6f}",
        "-i", "-",
        "-i", os.path.abspath(in_path),
        "-map", "0:v:0", "-map", "1:a:0?",
        *vf_args,
        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        os.path.abspath(out_path),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, cwd=os.path.dirname(ass_path) if ass_path else None)
    # Always release the capture: on Windows an open handle blocks deleting the
    # cut file, and that error would mask whatever actually went wrong here.
    cap = cv2.VideoCapture(in_path)
    try:
        last = len(xs) - 1
        index = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            i = min(index, last)
            box = segments[segment_of[i]][2]
            if box:
                bx, by, bw, bh = box
                top = cv2.resize(frame[by:by + bh, bx:bx + bw], (out_w, top_h), interpolation=cv2.INTER_CUBIC)
                content = frame[content_y:content_y + content_h, content_x:content_x + content_w]
                bottom = cv2.resize(content, (out_w, bottom_h), interpolation=cv2.INTER_LINEAR)
                composed = np.vstack((top, bottom))
            else:
                x0, y0 = xs[i], ys[i]
                composed = frame[y0:y0 + crop_h, x0:x0 + crop_w]
                if resize:
                    composed = cv2.resize(composed, (out_w, out_h), interpolation=cv2.INTER_CUBIC)
            proc.stdin.write(np.ascontiguousarray(composed).tobytes())
            index += 1
        proc.stdin.close()
        if proc.wait() != 0:
            raise RuntimeError(f"ffmpeg encode failed [{proc.returncode}]")
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    finally:
        cap.release()
        if ass_path and os.path.exists(ass_path):
            os.remove(ass_path)
    return out_path


def crop_clip_local(
    source_path: str,
    start_time: float,
    end_time: float,
    aspect_ratio: str,
    out_path: str,
    transcript: Optional[Dict] = None,
    layout: str = "auto",
) -> str:
    """Cut + reframe one highlight, returning the local mp4 path.

    Pass the source `transcript` to burn in word-by-word captions.
    """
    cut_path = out_path + ".cut.mp4"
    words = clip_words(transcript, start_time, end_time) if transcript else None
    try:
        _cut_subclip(source_path, start_time, end_time, cut_path)
        _reframe_vertical(cut_path, out_path, aspect_ratio, words=words, layout=layout)
    finally:
        if os.path.exists(cut_path):
            os.remove(cut_path)
    return out_path


def crop_highlights_local(
    source_path: str,
    highlights: List[Dict],
    aspect_ratio: str = "9:16",
    out_dir: Optional[str] = None,
    transcript: Optional[Dict] = None,
    layout: str = "auto",
) -> List[Dict]:
    out_dir = out_dir or LOCAL_OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    results: List[Dict] = []
    for i, h in enumerate(highlights, 1):
        out_path = os.path.join(out_dir, f"short_{i:02d}.mp4")
        print(f"[clip/local] {i}/{len(highlights)}: {h.get('title', '(untitled)')}", flush=True)
        try:
            crop_clip_local(
                source_path,
                float(h["start_time"]),
                float(h["end_time"]),
                aspect_ratio,
                out_path,
                transcript=transcript,
                layout=layout,
            )
            results.append({**h, "clip_url": out_path})
        except Exception as e:
            print(f"[clip/local] {i} failed: {e}", flush=True)
            results.append({**h, "clip_url": None, "error": str(e)})
    return results
