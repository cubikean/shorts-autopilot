"""Burned-in word-by-word captions for local clips (TikTok / Shorts style).

Whisper word timestamps → an ASS file showing one uppercase word at a time,
with a quick scale "pop" as each word lands, rendered by ffmpeg/libass.
Lines in another language than SUBTITLE_LANGUAGE arrive already translated
(local/translate.py), without word timestamps; their original words pop in
the same way, at half size, under the translated word.
"""
from typing import Dict, List, Optional

from ..config import SUBTITLE_FONT, SUBTITLE_POSITION

MIN_WORD_SECONDS = 0.12   # never flash a word for less than this
MAX_HOLD_SECONDS = 0.6    # keep a word up this long past its end when the speaker pauses
STRIP_CHARS = ".,!?;:\"«»“”„…()[]{}—–-¿¡"
APOSTROPHES = ("'", "’")


def _ass_time(seconds: float) -> str:
    cs = max(0, int(round(seconds * 100)))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _even_split(segment: Dict) -> List[Dict]:
    """Fallback when a segment has no word timestamps: spread words evenly."""
    tokens = str(segment.get("text", "")).split()
    if not tokens:
        return []
    start, end = float(segment["start"]), float(segment["end"])
    step = (end - start) / len(tokens)
    return [
        {"start": start + i * step, "end": start + (i + 1) * step, "word": tok}
        for i, tok in enumerate(tokens)
    ]


def _join_elisions(words: List[Dict]) -> List[Dict]:
    """Glue Whisper's split elisions back together: " j" + "'étais" → " j'étais".

    Only apostrophe joins are merged — languages written without spaces
    (Chinese, Japanese) also emit space-less tokens that must stay separate.
    """
    joined: List[Dict] = []
    for w in words:
        raw = str(w.get("word", ""))
        prev = joined[-1]["word"] if joined else ""
        if joined and (raw[:1] in APOSTROPHES or prev.rstrip()[-1:] in APOSTROPHES):
            joined[-1] = {**joined[-1], "end": w["end"], "word": prev + raw.lstrip()}
        else:
            joined.append(dict(w))
    return joined


def clip_words(transcript: Dict, start: float, end: float) -> List[Dict]:
    """Words spoken inside [start, end], re-timed relative to the clip start.

    Translated lines also yield their original words, tagged {"kind": "original"},
    on Whisper's real word timings when it gave them.
    """
    words: List[Dict] = []
    for segment in transcript.get("segments", []):
        if float(segment["end"]) <= start or float(segment["start"]) >= end:
            continue
        if segment.get("original"):
            original = segment.get("original_words") or _even_split({**segment, "text": segment["original"]})
            words += _in_clip(original, start, end, kind="original")
        words += _in_clip(segment.get("words") or _even_split(segment), start, end)
    return words


def _in_clip(raw_words: List[Dict], start: float, end: float, kind: Optional[str] = None) -> List[Dict]:
    out = []
    for w in _join_elisions(raw_words):
        w_start, w_end = float(w["start"]), float(w["end"])
        if w_end <= start or w_start >= end:
            continue
        text = str(w.get("word", "")).strip().strip(STRIP_CHARS)
        if not text:
            continue
        item = {"start": max(w_start, start) - start, "end": min(w_end, end) - start, "text": text.upper()}
        if kind:
            item["kind"] = kind
        out.append(item)
    return out


def _word_events(words: List[Dict], style: str, x: int, y: int, base_size: int, width: int,
                 duration: float) -> List[str]:
    """One ASS event per word, popping in at its own position."""
    events = []
    for i, w in enumerate(words):
        next_start = words[i + 1]["start"] if i + 1 < len(words) else duration
        shown_end = min(max(w["end"], w["start"] + MIN_WORD_SECONDS) + MAX_HOLD_SECONDS, next_start)
        shown_end = max(shown_end, w["start"] + 0.05)

        # Shrink long words so they fit in 90% of the frame width (~0.75em per heavy uppercase glyph).
        size = min(base_size, int(width * 0.9 / (len(w["text"]) * 0.75)))
        text = w["text"].replace("\\", "").replace("{", "(").replace("}", ")")
        tags = (
            "{\\pos(" + f"{x},{y}" + ")\\fs" + str(size)
            + "\\fscx75\\fscy75\\t(0,80,\\fscx112\\fscy112)\\t(80,150,\\fscx100\\fscy100)}"
        )
        events.append(f"Dialogue: 0,{_ass_time(w['start'])},{_ass_time(shown_end)},{style},,0,0,0,,{tags}{text}")
    return events


def write_ass(
    words: List[Dict],
    width: int,
    height: int,
    duration: float,
    out_path: str,
    position: Optional[float] = None,
) -> str:
    """Write an ASS subtitle file sized for a width×height video.

    `position` overrides SUBTITLE_POSITION (vertical centre, 0 = top, 1 = bottom).
    """
    base_size = round(height * 0.07)
    outline = max(2, round(height * 0.006))
    shadow = max(1, round(height * 0.004))
    x, y = width // 2, round(height * (SUBTITLE_POSITION if position is None else position))

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Word,{SUBTITLE_FONT},{base_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,"
        f"-1,-1,0,0,100,100,0,0,1,{outline},{shadow},5,0,0,0,1\n"
        f"Style: Original,{SUBTITLE_FONT},{base_size // 2},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,"
        f"-1,-1,0,0,100,100,0,0,1,{max(1, outline // 2)},{max(1, shadow // 2)},5,0,0,0,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    originals = [w for w in words if w.get("kind") == "original"]
    words = [w for w in words if w.get("kind") != "original"]
    # Original words: same pop, half size, on their own timing just under the translated word.
    lines = _word_events(words, "Word", x, y, base_size, width, duration) + _word_events(
        originals, "Original", x, y + round(base_size * 0.8), base_size // 2, width, duration)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(lines) + "\n")
    return out_path
