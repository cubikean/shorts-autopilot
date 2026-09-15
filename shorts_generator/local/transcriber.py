"""Local transcription via faster-whisper, adapted to the spoken language.

Reads a local media file and returns the same shape the highlight generator
expects: {duration, language, model, segments[start, end, text, words[start, end, word]]}.
Word timestamps drive the burned-in captions.

Language handling: the language is detected on the first ~90 s, the Whisper
model is picked for that language (LOCAL_WHISPER_MODELS), and transcription
runs with the language forced so Whisper never drifts into another language
mid-video. Runs on the NVIDIA GPU when available (no torch needed), else CPU.
"""
import glob
import json
import os
import sysconfig
from pathlib import Path
from typing import Dict, Optional, Tuple

from ..config import (
    LOCAL_OUTPUT_DIR,
    LOCAL_WHISPER_DEVICE,
    LOCAL_WHISPER_MODEL,
    LOCAL_WHISPER_MODELS,
)

DETECTION_SEGMENTS = 3  # 3 × 30 s windows: skips past a music intro before deciding
_MODELS: Dict[Tuple[str, str], object] = {}  # loaded once per process (feed runs many videos)


def _transcript_cache_path(media_path: str, suffix: str = ".srt") -> Path:
    """Return the cache path for a media file (.json is the real cache, .srt is for humans)."""
    cache_dir = Path(LOCAL_OUTPUT_DIR)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / (Path(media_path).stem + suffix)


def _format_srt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _write_srt_cache(media_path: str, transcript: Dict) -> Path:
    cache_path = _transcript_cache_path(media_path)
    lines = []
    for idx, segment in enumerate(transcript.get("segments", []), start=1):
        start = _format_srt_timestamp(float(segment["start"]))
        end = _format_srt_timestamp(float(segment["end"]))
        text = str(segment.get("text", "")).strip().replace("\r", "").replace("\n", " ")
        lines.append(str(idx))
        lines.append(f"{start} --> {end}")
        lines.append(text)
        lines.append("")

    cache_path.write_text("\n".join(lines), encoding="utf-8")
    return cache_path


def _load_json_cache(cache_path: Path) -> Optional[Dict]:
    """Return the cached transcript, or None if it is unreadable or empty."""
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not cached.get("segments") or float(cached.get("duration") or 0.0) <= 0.0:
        return None
    return cached


def model_for_language(language: Optional[str]) -> str:
    """Whisper model for a language: a forced LOCAL_WHISPER_MODEL, else the per-language table."""
    if LOCAL_WHISPER_MODEL != "auto":
        return LOCAL_WHISPER_MODEL
    return LOCAL_WHISPER_MODELS.get(language or "") or LOCAL_WHISPER_MODELS.get("*") or "large-v3-turbo"


def _cuda_ready() -> bool:
    """True when an NVIDIA GPU and the cuBLAS/cuDNN runtime DLLs are usable.

    The DLLs come from the nvidia-cublas-cu12 / nvidia-cudnn-cu12 wheels; they
    are registered explicitly because Windows doesn't search site-packages.
    Checked up front: a missing cuDNN aborts the process mid-transcription
    instead of raising.
    """
    try:
        import ctranslate2  # type: ignore

        if ctranslate2.get_cuda_device_count() == 0:
            return False
    except Exception:
        return False
    if os.name != "nt":
        return True

    import ctypes

    for bin_dir in glob.glob(os.path.join(sysconfig.get_paths()["purelib"], "nvidia", "*", "bin")):
        os.add_dll_directory(bin_dir)
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
    try:
        ctypes.WinDLL("cublas64_12.dll")
        ctypes.WinDLL("cudnn64_9.dll")
        return True
    except OSError:
        print(
            "[transcribe/local] NVIDIA GPU found but cuBLAS/cuDNN are missing; using CPU. "
            "Install them with: pip install nvidia-cublas-cu12 \"nvidia-cudnn-cu12==9.*\"",
            flush=True,
        )
        return False


def _resolve_device() -> str:
    if LOCAL_WHISPER_DEVICE != "auto":
        return LOCAL_WHISPER_DEVICE
    return "cuda" if _cuda_ready() else "cpu"


def _load_model(name: str, device: str):
    key = (name, device)
    if key not in _MODELS:
        from faster_whisper import WhisperModel  # type: ignore

        compute_type = "float16" if device == "cuda" else "int8"
        print(f"[transcribe/local] loading faster-whisper {name} on {device} ({compute_type})", flush=True)
        _MODELS[key] = WhisperModel(name, device=device, compute_type=compute_type)
    return _MODELS[key]


def transcribe_local(media_path: str, language: Optional[str] = None) -> Dict:
    """Run faster-whisper (with word timestamps) on a local file, caching the result as .json."""
    cache_path = _transcript_cache_path(media_path, ".json")
    if cache_path.exists() and cache_path.stat().st_mtime >= os.path.getmtime(media_path):
        cached = _load_json_cache(cache_path)
        # Stale if the forced language differs, or if it was made with another model than
        # the one we'd pick now for its language (e.g. an old "base" transcript).
        if (
            cached
            and (not language or cached.get("language") == language)
            and cached.get("model") == model_for_language(cached.get("language"))
        ):
            print(
                f"[transcribe/local] reusing cached transcript: {cache_path} "
                f"({len(cached['segments'])} segments, {cached['duration']:.0f}s, "
                f"lang={cached.get('language')}, model={cached.get('model')})",
                flush=True,
            )
            return cached
        print(f"[transcribe/local] cache is stale or invalid, re-transcribing: {cache_path}", flush=True)

    try:
        from faster_whisper import decode_audio  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "faster-whisper is required. Install it with:\n"
            "    pip install -r requirements.txt"
        ) from e

    from ..config import LOCAL_WHISPER_VAD_FILTER, LOCAL_WHISPER_VAD_PARAMETERS

    device = _resolve_device()
    audio = decode_audio(media_path)

    if language:
        print(f"[transcribe/local] language forced: {language}", flush=True)
    else:
        # Detect with the catch-all multilingual model; it's also the one used for most languages.
        detector = _load_model(model_for_language("*"), device)
        language, probability, _ = detector.detect_language(audio, language_detection_segments=DETECTION_SEGMENTS)
        print(f"[transcribe/local] detected language: {language} ({probability:.2f})", flush=True)

    model_name = model_for_language(language)
    model = _load_model(model_name, device)

    transcribe_kwargs = {
        "audio": audio,
        "language": language,
        "beam_size": 5,
        "condition_on_previous_text": False,
        "word_timestamps": True,
        "vad_filter": LOCAL_WHISPER_VAD_FILTER,
    }
    if LOCAL_WHISPER_VAD_FILTER:
        transcribe_kwargs["vad_parameters"] = LOCAL_WHISPER_VAD_PARAMETERS

    segments_iter, info = model.transcribe(**transcribe_kwargs)

    segments = []
    for s in segments_iter:
        segments.append({
            "start": float(s.start),
            "end": float(s.end),
            "text": (s.text or "").strip(),
            "words": [
                {"start": float(w.start), "end": float(w.end), "word": w.word}
                for w in (s.words or [])
            ],
        })

    duration = float(getattr(info, "duration", 0.0)) or (segments[-1]["end"] if segments else 0.0)
    print(
        f"[transcribe/local] {len(segments)} segments, {duration:.0f}s of audio, lang={language}, model={model_name}",
        flush=True,
    )
    transcript = {"duration": duration, "language": language, "model": model_name, "segments": segments}
    if segments:
        cache_path.write_text(json.dumps(transcript, ensure_ascii=False), encoding="utf-8")
        _write_srt_cache(media_path, transcript)
        print(f"[transcribe/local] wrote cache: {cache_path}", flush=True)
    return transcript
