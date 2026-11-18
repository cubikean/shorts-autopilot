"""Subtitles in French whatever language the streamer speaks.

Whisper can only translate into English, so the LLM translates the lines of
the clips that will be rendered (not the whole transcript: one small call per
video). Each translated line keeps its Whisper timing, and its words are
spread evenly over the span the original words covered: the captions follow
the speech line by line, not word by word.
"""
import json
from typing import Callable, Dict, List

from ..config import SUBTITLE_LANGUAGE

LANGUAGE_NAMES = {"fr": "French", "en": "English", "es": "Spanish", "de": "German", "pt": "Portuguese", "it": "Italian"}


def _prompt(lines: List[Dict], target: str, source: str) -> str:
    return (
        f"Translate these subtitle lines of a {source} live stream into {LANGUAGE_NAMES.get(target, target)}, "
        "as a viewer would say them out loud: natural spoken language, slang and swearing kept, "
        "names and in-game terms left as they are, each line about as long as the original so it fits "
        "the same seconds. Translate every line, even if it looks unfinished.\n"
        'Answer with JSON only: {"lines": [{"id": <id>, "text": "<translation>"}]}\n\n'
        + json.dumps({"lines": lines}, ensure_ascii=False)
    )


def _span(segment: Dict) -> Dict:
    """The part of the segment where words are actually spoken (Whisper pads segments with pauses)."""
    words = segment.get("words") or []
    if words:
        return {"start": float(words[0]["start"]), "end": float(words[-1]["end"])}
    return {"start": float(segment["start"]), "end": float(segment["end"])}


def translate_for_subtitles(transcript: Dict, highlights: List[Dict], llm_fn: Callable[[str], str],
                            target: str = SUBTITLE_LANGUAGE) -> Dict:
    """Copy of the transcript whose lines inside the highlights are in `target`.

    Best effort: on any failure the captions stay in the spoken language.
    """
    source = str(transcript.get("language") or "")
    if not target or not source or source == target:
        return transcript
    segments = transcript.get("segments", [])
    picked = [i for i, s in enumerate(segments) if any(
        float(s["end"]) > float(h["start_time"]) and float(s["start"]) < float(h["end_time"]) for h in highlights
    )]
    lines = [{"id": i, "text": str(segments[i].get("text", "")).strip()} for i in picked]
    lines = [line for line in lines if line["text"]]
    if not lines:
        return transcript
    try:
        from ..highlights import _parse_json_loose

        answer = _parse_json_loose(llm_fn(_prompt(lines, target, LANGUAGE_NAMES.get(source, source))))
        translated = {int(line["id"]): str(line["text"]).strip() for line in answer.get("lines", [])
                      if str(line.get("text", "")).strip()}
    except Exception as e:
        print(f"[translate] subtitles left in {source}: {e}", flush=True)
        return transcript
    out = []
    for i, segment in enumerate(segments):
        if i in translated:
            # No word timestamps: subtitles.clip_words spreads the words over the span.
            segment = {**segment, **_span(segment), "text": translated[i], "words": None}
        out.append(segment)
    print(f"[translate] {len(translated)}/{len(lines)} subtitle line(s) {source} → {target}", flush=True)
    return {**transcript, "segments": out}
