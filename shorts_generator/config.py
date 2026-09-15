import os

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str) -> str:
    """Env value with dotenv's 'KEY=  # comment' quirk treated as unset."""
    value = os.getenv(name, "").strip()
    return default if not value or value.startswith("#") else value


MUAPI_API_KEY = os.getenv("MUAPI_API_KEY", "").strip()
MUAPI_BASE_URL = os.getenv("MUAPI_BASE_URL", "https://api.muapi.ai/api/v1").rstrip("/")

POLL_INTERVAL_SECONDS = float(os.getenv("MUAPI_POLL_INTERVAL", "5"))
POLL_TIMEOUT_SECONDS = float(os.getenv("MUAPI_POLL_TIMEOUT", "600"))

# Local-mode (--mode local) settings — only consulted when running offline.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
# Sonnet: measured ~4.7x cheaper than inheriting an Opus default, and Haiku
# wasn't cheaper here because it wrote far longer answers.
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "").strip()
if not CLAUDE_MODEL or CLAUDE_MODEL.startswith("#"):
    # dotenv parses "CLAUDE_MODEL=   # note" as the value "# note".
    CLAUDE_MODEL = "sonnet"
# Reasoning effort (low / medium / high): thinking tokens are billed as output.
CLAUDE_EFFORT = os.getenv("CLAUDE_EFFORT", "").strip()
if not CLAUDE_EFFORT or CLAUDE_EFFORT.startswith("#"):
    CLAUDE_EFFORT = "low"
CLAUDE_TIMEOUT_SECONDS = float(os.getenv("CLAUDE_TIMEOUT", "600"))
# Extended thinking for the highlight call (off = ~35% fewer tokens, measured).
CLAUDE_THINKING = os.getenv("CLAUDE_THINKING", "off").strip().lower() in ("on", "true", "1", "yes")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").strip().lower()
# Whisper model. "auto" detects the spoken language first and picks from
# LOCAL_WHISPER_MODELS ("lang=model" pairs, "*" = any other language); any
# other value (e.g. "base") forces that model for every language.
# Measured on an RTX 3070 Ti against YouTube captions:
#   French: large-v3-turbo 18.5% WER at 17x realtime (base: 23%, many wrong words;
#           large-v3: 18.2% but 3x slower).
#   English: large-v3 9.5% at 8.5x realtime vs turbo 13.8% (distil-large-v3.5: 14.7%).
# On CPU large-v3 runs below realtime; set LOCAL_WHISPER_MODELS=*=large-v3-turbo there.
LOCAL_WHISPER_MODEL = _env("LOCAL_WHISPER_MODEL", "auto")
LOCAL_WHISPER_MODELS = {
    lang.strip(): model.strip()
    for lang, _, model in (
        pair.partition("=") for pair in _env("LOCAL_WHISPER_MODELS", "en=large-v3,*=large-v3-turbo").split(",")
    )
    if model.strip()
}
LOCAL_WHISPER_DEVICE = _env("LOCAL_WHISPER_DEVICE", "auto")  # auto (GPU if usable) / cpu / cuda
LOCAL_OUTPUT_DIR = os.getenv("LOCAL_OUTPUT_DIR", "output")

# Burned-in word-by-word captions (--mode local). Position is the caption's
# vertical centre as a fraction of the frame height (0 = top, 1 = bottom).
SUBTITLE_FONT = os.getenv("SUBTITLE_FONT", "Arial Black")
SUBTITLE_POSITION = float(os.getenv("SUBTITLE_POSITION", "0.70"))

# Daily feed (feed.py): whitelisted channels → Notion queue → local rendering.
NOTION_TOKEN = _env("NOTION_TOKEN", "")
NOTION_VIDEOS_DB = _env("NOTION_VIDEOS_DB", "")
NOTION_SHORTS_DB = _env("NOTION_SHORTS_DB", "")
TWITCH_CLIENT_ID = _env("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = _env("TWITCH_CLIENT_SECRET", "")
FEED_SOURCES_FILE = _env("FEED_SOURCES_FILE", "sources.json")
FEED_MIN_AGE_HOURS = float(_env("FEED_MIN_AGE_HOURS", "2"))       # view velocity is noise before this
FEED_MAX_AGE_HOURS = float(_env("FEED_MAX_AGE_HOURS", "72"))
# Source length limits, YouTube videos and Twitch VODs alike.
FEED_MIN_DURATION_MINUTES = float(_env("FEED_MIN_DURATION_MINUTES", "1"))
FEED_MAX_DURATION_MINUTES = float(_env("FEED_MAX_DURATION_MINUTES", "120"))
FEED_MIN_SCORE = float(_env("FEED_MIN_SCORE", "1.5"))             # YouTube: x times the channel's usual velocity
FEED_TWITCH_MOMENTS_PER_CHANNEL = int(_env("FEED_TWITCH_MOMENTS_PER_CHANNEL", "3"))
FEED_TWITCH_MIN_VIEWS = int(_env("FEED_TWITCH_MIN_VIEWS", "50"))
FEED_MAX_PER_RUN = int(_env("FEED_MAX_PER_RUN", "3"))              # videos rendered per `feed.py process`
FEED_CLIPS_PER_VIDEO = int(_env("FEED_CLIPS_PER_VIDEO", "3"))

# Publishing (feed.py publish): shorts marked "Validé" in Notion → each platform.
PUBLISH_PLATFORMS = [p.strip().lower() for p in _env("PUBLISH_PLATFORMS", "youtube").split(",") if p.strip()]
# YouTube's default quota allows ~6 uploads a day; 1 per run spreads them over the day.
PUBLISH_MAX_PER_RUN = int(_env("PUBLISH_MAX_PER_RUN", "1"))
YOUTUBE_CLIENT_SECRETS = _env("YOUTUBE_CLIENT_SECRETS", "client_secret.json")
YOUTUBE_TOKEN_FILE = _env("YOUTUBE_TOKEN_FILE", "youtube_token.json")
YOUTUBE_PRIVACY = _env("YOUTUBE_PRIVACY", "public")         # public / unlisted / private
YOUTUBE_CATEGORY_ID = _env("YOUTUBE_CATEGORY_ID", "24")     # 24 Entertainment, 20 Gaming, 23 Comedy


# VAD (Voice Activity Detection) settings for faster-whisper
# Default threshold is 0.5; lower = more sensitive, higher = less sensitive
# Default min_speech_duration_ms is 250ms; increase to avoid tiny false positives
# Default min_silence_duration_ms is 2000ms; increase to avoid splitting mid-sentence
# DISABLED by default because VAD is too aggressive on mixed speech/music content
LOCAL_WHISPER_VAD_FILTER = os.getenv("LOCAL_WHISPER_VAD_FILTER", "false").strip().lower() == "true"
_vad_params_env = os.getenv("LOCAL_WHISPER_VAD_PARAMETERS", "")
if _vad_params_env:
    import json
    LOCAL_WHISPER_VAD_PARAMETERS = json.loads(_vad_params_env)
else:
    # Match faster-whisper defaults when VAD is enabled
    LOCAL_WHISPER_VAD_PARAMETERS = {
        "threshold": 0.5,
        "min_speech_duration_ms": 250,
        "max_speech_duration_s": float("inf"),
        "min_silence_duration_ms": 2000,
        "speech_pad_ms": 400,
    }


def require_api_key() -> str:
    if not MUAPI_API_KEY:
        raise RuntimeError(
            "MUAPI_API_KEY is not set. Add it to your .env file or export it as an env var."
        )
    return MUAPI_API_KEY


def require_openai_key() -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Local mode needs an OpenAI key for highlight ranking. "
            "Add it to your .env or export it, or switch back to --mode api."
        )
    return OPENAI_API_KEY


def require_gemini_key() -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Local mode needs a Gemini key when LLM_PROVIDER=gemini. "
            "Add it to your .env or export it, or switch LLM_PROVIDER back to openai."
        )
    return GEMINI_API_KEY
