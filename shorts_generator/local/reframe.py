"""Plan a stable, face-aware crop path for vertical reframing.

Tracking frame by frame and easing toward every detection makes the crop
flicker: detectors miss frames, fire on background objects and jitter by a
few pixels. Clips are rendered offline, so we plan the whole path first:

  1. analyse — YuNet face detection ~10x/s on a downscaled frame, plus a cheap
     per-frame difference signal to find shot cuts.
  2. plan    — per shot: follow one subject (continuity over size), drop
     outlier detections, fill gaps, then either lock the camera when the
     subject stays within a small span, or follow it with a dead zone and
     zero-phase smoothing. Cuts are hard cuts, never pans.

Falls back to OpenCV's Haar cascade when the YuNet model can't be loaded.
"""
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

YUNET_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_detection_yunet/face_detection_yunet_2023mar.onnx"
)
YUNET_PATH = Path.home() / ".cache" / "ai-shorts-generator" / "face_detection_yunet_2023mar.onnx"

DETECT_WIDTH = 320            # detector input width; faces in shorts sources are large
DETECTIONS_PER_SECOND = 10
MIN_CONFIDENCE = 0.7

CUT_MIN_DIFF = 12.0           # mean abs grey-level change (0-255) on a 64x36 thumbnail
CUT_RATIO = 4.0               # ...and this many times the local median change
MIN_SHOT_SECONDS = 0.5

OUTLIER_FACE_WIDTHS = 1.0     # drop samples this far from the rolling median
LOCK_SPAN = 0.25              # lock camera if the subject moves < 25% of the crop size
DEAD_ZONE = 0.08              # follow mode: subject may drift ±8% of the crop before panning
TARGET_SMOOTH_SECONDS = 0.4
CAMERA_SMOOTH_SECONDS = 0.15

Detection = Tuple[float, float, float, float, float]  # cx, cy, w, h, score (source pixels)


def _yunet_model() -> Optional[Path]:
    if YUNET_PATH.exists():
        return YUNET_PATH
    # Plain ASCII: a Windows console on 'charmap' can't print arrows, and this
    # must stay outside the try so a print error can't masquerade as "offline".
    print(f"[reframe] downloading YuNet face model -> {YUNET_PATH}", flush=True)
    try:
        YUNET_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = YUNET_PATH.with_suffix(".part")
        urllib.request.urlretrieve(YUNET_URL, tmp)
        tmp.replace(YUNET_PATH)
        return YUNET_PATH
    except Exception as e:  # offline, proxy, ... — Haar still works
        print(f"[reframe] YuNet unavailable ({e}); falling back to Haar cascade", flush=True)
        return None


def _make_detector(cv2, width: int, height: int) -> Tuple[str, Callable[[np.ndarray], List[Detection]]]:
    scale = DETECT_WIDTH / width
    size = (DETECT_WIDTH, max(1, round(height * scale)))

    model = _yunet_model() if hasattr(cv2, "FaceDetectorYN") else None
    if model:
        yunet = cv2.FaceDetectorYN.create(str(model), "", size, MIN_CONFIDENCE, 0.3, 50)

        def detect_yunet(frame: np.ndarray) -> List[Detection]:
            _, faces = yunet.detect(cv2.resize(frame, size, interpolation=cv2.INTER_AREA))
            if faces is None:
                return []
            return [
                ((f[0] + f[2] / 2) / scale, (f[1] + f[3] / 2) / scale, f[2] / scale, f[3] / scale, float(f[14]))
                for f in faces
            ]

        return "yunet", detect_yunet

    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

    def detect_haar(frame: np.ndarray) -> List[Detection]:
        gray = cv2.cvtColor(cv2.resize(frame, size, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=8, minSize=(24, 24))
        return [((x + w / 2) / scale, (y + h / 2) / scale, w / scale, h / scale, 1.0) for x, y, w, h in faces]

    return "haar", detect_haar


def _smooth(values: np.ndarray, sigma: float) -> np.ndarray:
    """Zero-phase Gaussian smoothing (no lag), edges padded by repetition."""
    if sigma < 0.5 or len(values) < 3:
        return values.astype(float)
    radius = int(3 * sigma)
    kernel = np.exp(-0.5 * (np.arange(-radius, radius + 1) / sigma) ** 2)
    kernel /= kernel.sum()
    padded = np.pad(values.astype(float), radius, mode="edge")
    return np.convolve(padded, kernel, mode="valid")


def _rolling_median(values: np.ndarray, window: int) -> np.ndarray:
    half = window // 2
    return np.array([np.median(values[max(0, i - half): i + half + 1]) for i in range(len(values))])


def analyse(cv2, video_path: str) -> Dict:
    """Pass 1: face detections on sampled frames + per-frame cut signal."""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"could not open {video_path}")
    try:
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        detector_name, detect = _make_detector(cv2, width, height)
        step = max(1, round(fps / DETECTIONS_PER_SECOND))

        samples: Dict[int, List[Detection]] = {}
        diffs: List[float] = []
        prev = None
        index = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            thumb = cv2.cvtColor(cv2.resize(frame, (64, 36), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
            diffs.append(0.0 if prev is None else float(np.mean(cv2.absdiff(thumb, prev))))
            prev = thumb
            if index % step == 0:
                samples[index] = detect(frame)
            index += 1
    finally:
        cap.release()

    return {
        "width": width, "height": height, "fps": fps, "frames": index,
        "detector": detector_name, "samples": samples, "diffs": np.array(diffs),
    }


def find_cuts(diffs: np.ndarray, fps: float) -> List[int]:
    """Frame indices where a new shot starts (always includes 0)."""
    if len(diffs) == 0:
        return [0]
    local = _rolling_median(diffs, max(3, int(fps)))
    is_cut = (diffs > CUT_MIN_DIFF) & (diffs > CUT_RATIO * np.maximum(local, 1.0))
    cuts = [0]
    for i in np.flatnonzero(is_cut):
        if i - cuts[-1] >= MIN_SHOT_SECONDS * fps:
            cuts.append(int(i))
    return cuts


def _subject_centres(frames: List[int], samples: Dict[int, List[Detection]]) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """One subject per sample: biggest confident face first, then the face closest to it."""
    xs, ys, sizes = [], [], []
    prev: Optional[Detection] = None
    for f in frames:
        dets = samples.get(f) or []
        if not dets:
            xs.append(np.nan); ys.append(np.nan); sizes.append(np.nan)
            continue
        if prev is None:
            pick = max(dets, key=lambda d: d[4] * d[2] * d[3])
        else:
            pick = max(dets, key=lambda d: d[4] * d[2] * d[3] / (1.0 + ((d[0] - prev[0]) / max(prev[2], 1.0)) ** 2))
        prev = pick
        xs.append(pick[0]); ys.append(pick[1]); sizes.append(pick[2])
    return np.array(xs), np.array(ys), np.array(sizes)


def _plan_axis(centres: np.ndarray, crop: int, src: int, fps: float) -> Tuple[np.ndarray, bool]:
    """Per-frame crop offset along one axis for a single shot. Returns (offsets, locked)."""
    room = src - crop
    if room <= 0:
        return np.zeros(len(centres)), True
    lo, hi = np.percentile(centres, [5, 95])
    if hi - lo <= LOCK_SPAN * crop:
        offset = np.clip(np.median(centres) - crop / 2, 0, room)
        return np.full(len(centres), offset), True

    target = _smooth(centres, TARGET_SMOOTH_SECONDS * fps)
    dead = DEAD_ZONE * crop
    camera = np.empty_like(target)
    c = target[0]
    for i, t in enumerate(target):
        if t > c + dead:
            c = t - dead
        elif t < c - dead:
            c = t + dead
        camera[i] = c
    camera = _smooth(camera, CAMERA_SMOOTH_SECONDS * fps)
    return np.clip(camera - crop / 2, 0, room), False


def plan_crop_path(analysis: Dict, crop_w: int, crop_h: int) -> Tuple[np.ndarray, np.ndarray, Dict]:
    """Pass 2 planning: integer (x0, y0) per frame, plus stats for logging."""
    width, height, fps, total = analysis["width"], analysis["height"], analysis["fps"], analysis["frames"]
    samples = analysis["samples"]
    cuts = find_cuts(analysis["diffs"], fps) + [total]

    x0 = np.zeros(total)
    y0 = np.zeros(total)
    locked = faceless = 0
    for start, end in zip(cuts[:-1], cuts[1:]):
        frames = [f for f in sorted(samples) if start <= f < end]
        xs, ys, sizes = _subject_centres(frames, samples)
        valid = ~np.isnan(xs)
        if valid.sum() >= 3:
            # Reject detections that jump away from their neighbours (false positives).
            med_x = _rolling_median(xs[valid], 5)
            med_y = _rolling_median(ys[valid], 5)
            tol = OUTLIER_FACE_WIDTHS * np.nanmedian(sizes[valid])
            keep = (np.abs(xs[valid] - med_x) <= tol) & (np.abs(ys[valid] - med_y) <= tol)
            idx = np.flatnonzero(valid)
            valid[idx[~keep]] = False

        shot_frames = np.arange(start, end)
        if valid.any():
            sample_frames = np.array(frames)[valid]
            cx = np.interp(shot_frames, sample_frames, xs[valid])
            cy = np.interp(shot_frames, sample_frames, ys[valid])
        else:
            faceless += 1
            cx = np.full(len(shot_frames), width / 2)
            cy = np.full(len(shot_frames), height / 2)

        x0[start:end], lock_x = _plan_axis(cx, crop_w, width, fps)
        y0[start:end], lock_y = _plan_axis(cy, crop_h, height, fps)
        locked += int(lock_x and lock_y)

    detected = sum(1 for d in samples.values() if d)
    stats = {
        "detector": analysis["detector"],
        "shots": len(cuts) - 1,
        "locked": locked,
        "faceless": faceless,
        "face_rate": detected / max(1, len(samples)),
    }
    return np.round(x0).astype(int), np.round(y0).astype(int), stats


# --- Stacked layout: streamer webcam on top, content below -------------------
#
# Stream webcams are either a bordered rectangle or a chroma-keyed cut-out with
# no border at all, so the overlay is found from the face itself: a small face
# that stays put for seconds, next to a frame edge. The top panel frames head
# and shoulders around it; the bottom panel is a centre crop of the content.

STACK_TOP_SHARE = 0.40        # webcam panel height as a share of the output height
CAM_BOX_FACE_WIDTHS = 4.0     # webcam panel spans this many face widths (head + shoulders)
CAM_MAX_FACE_SHARE = 0.12     # overlay faces are small: width <= 12% of the frame width
CAM_MIN_FACE_SHARE = 0.035    # ...but not chat avatars / emotes pinned to the edge
CAM_EDGE_MARGIN = 0.12        # ...and the webcam box sits within 12% of a frame edge
CAM_MIN_SPAN_SECONDS = 8.0   # stream webcams stay put; a few seconds of small face is just a wide shot
CAM_MIN_DENSITY = 0.6         # detected in >= 60% of the samples across its span
CAM_MAX_JITTER = 0.5          # position std <= half a face width: overlays don't wander
CAM_MAX_GAP_SECONDS = 2.0     # keep the stacked layout through detection gaps this short
MIN_LAYOUT_SECONDS = 2.0      # never switch layout for less than this


def _cluster_faces(samples: Dict[int, List[Detection]]) -> List[Dict]:
    """Group detections that sit at the same place with the same size."""
    clusters: List[Dict] = []
    for f in sorted(samples):
        for cx, cy, w, _h, _score in samples[f]:
            for c in clusters:
                if (abs(cx - c["cx"]) <= 0.75 * c["w"] and abs(cy - c["cy"]) <= 0.75 * c["w"]
                        and 0.65 <= w / c["w"] <= 1.5):
                    break
            else:
                c = {"xs": [], "ys": [], "ws": [], "frames": []}
                clusters.append(c)
            c["xs"].append(cx)
            c["ys"].append(cy)
            c["ws"].append(w)
            if not c["frames"] or c["frames"][-1] != f:
                c["frames"].append(f)
            c["cx"], c["cy"], c["w"] = float(np.mean(c["xs"])), float(np.mean(c["ys"])), float(np.mean(c["ws"]))
    return clusters


def _cam_box(cx: float, cy: float, face_w: float, width: int, height: int, aspect: float) -> Tuple[int, int, int, int]:
    """Head-and-shoulders box (x, y, w, h) at the panel aspect, shifted inside the frame."""
    box_w = min(float(width), CAM_BOX_FACE_WIDTHS * face_w)
    box_h = box_w / aspect
    if box_h > height:
        box_h = float(height)
        box_w = box_h * aspect
    x = float(np.clip(cx - box_w / 2, 0, width - box_w))
    # Face sits slightly above centre so shoulders stay in shot.
    y = float(np.clip(cy + 0.15 * box_h - box_h / 2, 0, height - box_h))
    return int(x), int(y), int(box_w) - int(box_w) % 2, int(box_h) - int(box_h) % 2


def _webcam_candidates(analysis: Dict, aspect: float, force: bool) -> List[Dict]:
    width, height, fps = analysis["width"], analysis["height"], analysis["fps"]
    samples = analysis["samples"]
    sample_frames = np.array(sorted(samples))
    candidates = []
    for c in _cluster_faces(samples):
        frames = c["frames"]
        if (frames[-1] - frames[0]) / fps < CAM_MIN_SPAN_SECONDS:
            continue
        in_span = np.count_nonzero((sample_frames >= frames[0]) & (sample_frames <= frames[-1]))
        if len(frames) / max(1, in_span) < CAM_MIN_DENSITY:
            continue
        face_w = float(np.median(c["ws"]))
        if max(np.std(c["xs"]), np.std(c["ys"])) > CAM_MAX_JITTER * face_w:
            continue
        box = _cam_box(float(np.median(c["xs"])), float(np.median(c["ys"])), face_w, width, height, aspect)
        if not force:
            x, y, bw, bh = box
            mx, my = CAM_EDGE_MARGIN * width, CAM_EDGE_MARGIN * height
            near_edge = x <= mx or x + bw >= width - mx or y <= my or y + bh >= height - my
            if not CAM_MIN_FACE_SHARE * width <= face_w <= CAM_MAX_FACE_SHARE * width or not near_edge:
                continue
        candidates.append({"frames": set(frames), "count": len(frames), "box": box})
    return sorted(candidates, key=lambda c: -c["count"])


def plan_layout(analysis: Dict, top_aspect: float, mode: str = "auto") -> List[Tuple[int, int, Optional[Tuple[int, int, int, int]]]]:
    """Split the clip into (start, end, webcam_box) segments; box None = regular face crop.

    mode: "auto" (webcam overlays only), "stack" (any steady face) or "single".
    """
    total, fps = analysis["frames"], analysis["fps"]
    if mode == "single" or total == 0:
        return [(0, total, None)]
    cams = _webcam_candidates(analysis, top_aspect, force=(mode == "stack"))
    if not cams:
        return [(0, total, None)]

    sample_frames = sorted(analysis["samples"])
    labels: List[Optional[int]] = [
        next((i for i, c in enumerate(cams) if f in c["frames"]), None) for f in sample_frames
    ]
    # Bridge short misses (blink, hand over face) with the webcam seen just before.
    last_label, last_frame = None, None
    for k, f in enumerate(sample_frames):
        if labels[k] is not None:
            last_label, last_frame = labels[k], f
        elif last_label is not None and f - last_frame <= CAM_MAX_GAP_SECONDS * fps:
            labels[k] = last_label

    runs: List[List] = []  # [label, start_frame]
    for label, f in zip(labels, sample_frames):
        if not runs or runs[-1][0] != label:
            runs.append([label, f])
    runs[0][1] = 0
    min_frames = MIN_LAYOUT_SECONDS * fps
    while len(runs) > 1 and runs[1][1] - runs[0][1] < min_frames:
        runs.pop(0)
        runs[0][1] = 0
    kept: List[List] = []
    for i, (label, start) in enumerate(runs):
        end = runs[i + 1][1] if i + 1 < len(runs) else total
        if kept and (end - start < min_frames or kept[-1][0] == label):
            continue  # too short, or same layout as before: previous segment extends over it
        kept.append([label, start])

    # Layout switches happen on scene changes: snap each switch to a nearby cut.
    cuts = find_cuts(analysis["diffs"], fps)
    for seg in kept[1:]:
        near = [c for c in cuts if abs(c - seg[1]) <= fps]
        if near:
            seg[1] = min(near, key=lambda c: abs(c - seg[1]))

    segments = []
    for i, (label, start) in enumerate(kept):
        end = kept[i + 1][1] if i + 1 < len(kept) else total
        if end > start:
            segments.append((int(start), int(end), None if label is None else cams[label]["box"]))
    return segments
