"""Find the most viral-worthy highlights in a transcript.

Logic ported from ViralVadoo's transcript_analysis/highlight_generator.py:
  - chunking for long videos with overlap
  - virality-criteria prompt
  - score-based dedupe with overlap suppression

Token budget: one LLM call per chunk (the model infers the content type
itself), integer-second timestamps in the transcript, and a bounded number of
short candidates. Returned times are snapped to Whisper segment boundaries, so
the coarse timestamps never cut mid-sentence.

The LLM call is pluggable via the `llm_fn` argument (default: the provider
selected by LLM_PROVIDER).
"""
import json
import re
from typing import Callable, Dict, List, Optional


LLMFn = Callable[[str], str]


VIRALITY_CRITERIA = """
Virality signals to prioritize (ranked by impact):
1. HOOK MOMENTS — statements that create immediate curiosity ("The secret is...", "Nobody talks about...", "I was completely wrong about...")
2. EMOTIONAL PEAKS — genuine surprise, laughter, anger, vulnerability, excitement; raw unscripted reactions
3. OPINION BOMBS — strong, polarizing or counter-intuitive statements that trigger agree/disagree
4. REVELATION MOMENTS — surprising facts, stats, or confessions that reframe how the viewer thinks
5. CONFLICT/TENSION — disagreement, pushback, or a problem being confronted head-on
6. QUOTABLE ONE-LINERS — a sentence that works as a standalone quote card
7. STORY PEAKS — the climax or twist of an anecdote; the payoff moment
8. PRACTICAL VALUE — a concrete tip, hack, or insight the viewer can immediately apply
"""


HIGHLIGHT_SYSTEM_PROMPT = """You are an elite short-form video editor who has studied thousands of viral clips on TikTok, Instagram Reels, and YouTube Shorts. You know exactly what makes viewers stop scrolling, watch to the end, and share.

{virality_criteria}

Your task: identify the most viral-worthy highlights from the transcript below. Infer the content type (podcast, interview, tutorial, vlog, ...) from the transcript and judge highlights accordingly.

Rules:
- Every highlight must open with a strong HOOK — a line that grabs attention within the first 3 seconds
- Duration sweet spot: 45-90 seconds. Go shorter (20-44s) only for a perfect standalone one-liner. Go longer (91-180s) only when a story arc needs full context to land
- Never cut mid-sentence or mid-thought — each clip must feel complete and self-contained
- Clips must not overlap significantly with each other
- Score 0-100 on viral potential (not general quality)
- {num_clips_instruction}
- start_time / end_time are integer seconds read from the [seconds] markers
- "title": max 50 characters. "hook_sentence": the clip's opening line, verbatim. "virality_reason": max 15 words
- "description": 1-2 short lines. "hashtags": 3-5 lowercase hashtags mixing niche (streamer, game, show) and broad tags, no spaces inside a tag
- Write title, description, hashtags and virality_reason in the transcript's language

Tone for title and description — the audience is teenagers scrolling Shorts:
- Talk like a 16-20 year old texting friends about the clip: spoken, casual, direct, short punchy phrasing. Lowercase is fine
- Address the viewer directly (in French: tutoiement, "t'as vu ça", "le mec"); natural slang is welcome (in French e.g. "grave", "chaud", "ptdr", "jsuis mort"), but never forced, and don't reuse the same slang word across clips
- React to the moment or tease it instead of summarising it; never spoil the punchline
- Banned: formal, literary or marketing wording (in French e.g. "un moment culte", "à ne pas manquer", "découvrez", "totalement", "enchaîne", "absurde")
- Title: 0-1 emoji. Description: 0-2 emojis, may end with a question that makes people comment

Respond ONLY with valid JSON (no markdown, no explanation):
{{"highlights":[{{"title":"string","start_time":int,"end_time":int,"score":int,"hook_sentence":"string","virality_reason":"string","description":"string","hashtags":["#tag"]}}]}}"""


CHUNK_SIZE_SECONDS = 1200       # 20-min chunks for long videos
LONG_VIDEO_THRESHOLD = 1800     # chunk videos longer than 30 min
CHUNK_OVERLAP_SECONDS = 60
MAX_HIGHLIGHT_API_ATTEMPTS = 3


def _default_llm(prompt: str) -> str:
    from .local.llm import call_local_llm  # lazy: local.llm imports this module

    return call_local_llm(prompt)


def _parse_json_loose(raw: str) -> Dict:
    """LLMs sometimes wrap JSON in markdown fences — strip and parse."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(text[start:end + 1])
        raise


def _coerce_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _sanitize_highlights(raw_highlights: object, min_start: float, max_end: float) -> List[Dict]:
    """Normalize model output into the expected shape; skip invalid entries.

    Times are absolute; they are clamped to [min_start, max_end] (max_end <= 0 means unbounded).
    """
    if not isinstance(raw_highlights, list):
        return []

    max_end = max_end if max_end > 0 else float("inf")
    cleaned: List[Dict] = []
    for item in raw_highlights:
        if not isinstance(item, dict):
            continue

        start = _coerce_float(item.get("start_time"), default=-1.0)
        end = _coerce_float(item.get("end_time"), default=-1.0)
        if start < 0 or end <= start:
            continue

        start = min(max(start, min_start), max_end)
        end = min(end, max_end)
        if end <= start:
            continue

        cleaned.append(
            {
                "title": str(item.get("title") or "Untitled Highlight").strip(),
                "start_time": start,
                "end_time": end,
                "score": max(0, min(100, _coerce_int(item.get("score"), default=0))),
                "hook_sentence": str(item.get("hook_sentence") or "").strip(),
                "virality_reason": str(item.get("virality_reason") or "").strip(),
                "description": str(item.get("description") or "").strip(),
                "hashtags": _clean_hashtags(item.get("hashtags")),
            }
        )

    return cleaned


def _clean_hashtags(value: object) -> List[str]:
    """Normalise to at most 5 unique '#tag' strings, always ending with #shorts."""
    raw = value if isinstance(value, list) else re.split(r"[\s,]+", str(value or ""))
    tags: List[str] = []
    for item in raw:
        tag = re.sub(r"[^\w]", "", str(item))
        if tag and tag.lower() not in {t[1:].lower() for t in tags}:
            tags.append("#" + tag)
    return [t for t in tags if t.lower() != "#shorts"][:4] + ["#shorts"]


def snap_to_segments(highlights: List[Dict], segments: List[Dict]) -> List[Dict]:
    """Move each start/end onto the nearest Whisper segment boundary.

    The prompt only carries integer seconds; snapping restores sentence-accurate cuts.
    """
    if not segments:
        return highlights
    starts = [float(s["start"]) for s in segments]
    ends = [float(s["end"]) for s in segments]
    snapped = []
    for h in highlights:
        start = min(starts, key=lambda t: abs(t - h["start_time"]))
        end = min(ends, key=lambda t: abs(t - h["end_time"]))
        if end <= start:
            start, end = h["start_time"], h["end_time"]
        snapped.append({**h, "start_time": start, "end_time": end})
    return snapped


def build_transcript_text(transcript: Dict) -> str:
    segments = transcript.get("segments", [])
    return "\n".join(f"[{int(s['start'])}] {s['text'].strip()}" for s in segments)


def chunk_transcript(transcript: Dict) -> List[Dict]:
    segments = transcript.get("segments", [])
    duration = transcript.get("duration", segments[-1]["end"] if segments else 0)
    chunks = []
    start = 0
    while start < duration:
        end = min(start + CHUNK_SIZE_SECONDS, duration)
        chunk_segs = [
            s for s in segments
            if s["start"] >= start and s["end"] <= end + CHUNK_OVERLAP_SECONDS
        ]
        if chunk_segs:
            chunk = dict(transcript)
            chunk["segments"] = chunk_segs
            chunk["duration"] = end - start
            chunk["_offset"] = start
            chunks.append(chunk)
        start += CHUNK_SIZE_SECONDS - CHUNK_OVERLAP_SECONDS
    return chunks


def call_highlight_api(
    transcript: Dict,
    num_clips: int,
    is_chunk: bool = False,
    llm_fn: LLMFn = _default_llm,
) -> Dict:
    duration = float(transcript.get("duration", 0))
    offset = float(transcript.get("_offset", 0))
    segments = transcript.get("segments", [])
    # A couple of spares above num_clips give dedupe headroom without paying
    # for a long tail of candidates that never get rendered.
    natural_max = max(2 if is_chunk else 3, int(duration / 90))
    wanted = max(1, min(num_clips + 2, natural_max, 8))
    system = HIGHLIGHT_SYSTEM_PROMPT.format(
        virality_criteria=VIRALITY_CRITERIA,
        num_clips_instruction=f"Return at most {wanted} highlights, best first",
    )
    base_prompt = f"{system}\n\nTranscript:\n{build_transcript_text(transcript)}"
    prompt = base_prompt
    last_error = "unknown"
    # Segment timestamps are absolute, so chunk bounds are offset..offset+duration (+ overlap).
    max_end = offset + duration + (CHUNK_OVERLAP_SECONDS if is_chunk else 0)

    for attempt in range(1, MAX_HIGHLIGHT_API_ATTEMPTS + 1):
        raw = llm_fn(prompt)
        try:
            parsed = _parse_json_loose(raw)
            highlights = _sanitize_highlights(parsed.get("highlights"), min_start=offset, max_end=max_end)
            if highlights:
                return {"highlights": snap_to_segments(highlights, segments)}
            last_error = "no valid highlights in response"
        except Exception as e:
            last_error = str(e)

        if attempt < MAX_HIGHLIGHT_API_ATTEMPTS:
            print(
                f"[highlights] invalid model output on attempt {attempt}/{MAX_HIGHLIGHT_API_ATTEMPTS}; retrying",
                flush=True,
            )
            prompt = (
                base_prompt
                + "\n\nIMPORTANT: Return ONLY valid JSON with a top-level 'highlights' array."
                + " Each item must include: title, start_time, end_time, score, hook_sentence, virality_reason,"
                + " description, hashtags."
                + " No markdown fences, no commentary."
            )

    raise RuntimeError(
        f"Highlight generator produced invalid output after {MAX_HIGHLIGHT_API_ATTEMPTS} attempts: {last_error}"
    )


def dedupe_highlights(highlights: List[Dict]) -> List[Dict]:
    """Drop a highlight if it overlaps >50% with a higher-scoring one already kept."""
    highlights = sorted(highlights, key=lambda x: int(x.get("score", 0)), reverse=True)
    kept: List[Dict] = []
    for h in highlights:
        h_start = float(h["start_time"])
        h_end = float(h["end_time"])
        h_dur = h_end - h_start
        overlapping = False
        for k in kept:
            latest_start = max(h_start, float(k["start_time"]))
            earliest_end = min(h_end, float(k["end_time"]))
            overlap = earliest_end - latest_start
            if overlap > 0 and overlap > 0.5 * h_dur:
                overlapping = True
                break
        if not overlapping:
            kept.append(h)
    return kept


def get_highlights(
    transcript: Dict,
    num_clips: int = 3,
    llm_fn: Optional[LLMFn] = None,
) -> Dict:
    """Main entry point — returns {highlights: [...]} sorted by score.

    `llm_fn` swaps the underlying LLM. Defaults to the LLM_PROVIDER backend.
    """
    llm_fn = llm_fn or _default_llm
    duration = transcript.get("duration", 0)
    print(f"[highlights] duration={duration:.0f}s", flush=True)

    if duration >= LONG_VIDEO_THRESHOLD:
        chunks = chunk_transcript(transcript)
        print(f"[highlights] long video — splitting into {len(chunks)} chunks", flush=True)
        all_highlights: List[Dict] = []
        for i, chunk in enumerate(chunks):
            print(f"[highlights] chunk {i + 1}/{len(chunks)} (offset {chunk['_offset']:.0f}s)", flush=True)
            result = call_highlight_api(chunk, num_clips=num_clips, is_chunk=True, llm_fn=llm_fn)
            all_highlights.extend(result.get("highlights", []))
        highlights = dedupe_highlights(all_highlights)
    else:
        result = call_highlight_api(transcript, num_clips=num_clips, llm_fn=llm_fn)
        highlights = dedupe_highlights(result.get("highlights", []))

    return {"highlights": highlights}
