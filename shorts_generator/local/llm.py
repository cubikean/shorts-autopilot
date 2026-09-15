"""Local LLM backend — OpenAI, Gemini or Claude Code, selected by LLM_PROVIDER."""
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from ..config import (
    CLAUDE_EFFORT,
    CLAUDE_MODEL,
    CLAUDE_THINKING,
    CLAUDE_TIMEOUT_SECONDS,
    GEMINI_MODEL,
    LLM_PROVIDER,
    LOCAL_OUTPUT_DIR,
    OPENAI_MODEL,
    require_gemini_key,
    require_openai_key,
)


def call_openai_llm(prompt: str) -> str:
    """OpenAI Chat Completions backend (LLM_PROVIDER=openai)."""
    try:
        from openai import OpenAI  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "openai is required. Install it with:\n"
            "    pip install -r requirements.txt"
        ) from e

    client = OpenAI(api_key=require_openai_key())
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        temperature=0.7,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content or ""


def call_gemini_llm(prompt: str) -> str:
    """Gemini backend (LLM_PROVIDER=gemini)."""
    try:
        from google import genai  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "google-genai is required for LLM_PROVIDER=gemini. Install it with:\n"
            "    pip install -r requirements.txt"
        ) from e

    client = genai.Client(api_key=require_gemini_key())
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config={
            "temperature": 0.2,
            "response_mime_type": "application/json",
            "max_output_tokens": 8192,
        },
    )
    return response.text or ""


CLAUDE_SYSTEM_PROMPT = "You are a JSON-only API. Follow the user's instructions exactly and output only the requested JSON."


def call_claude_llm(prompt: str) -> str:
    """Claude Code CLI backend (`claude -p`), billed against the user's Claude plan.

    Runs with a bare context: no tools, MCP servers, skills, user/project
    settings (which would also pull in the user's default model) or Claude
    Code's own system prompt — the transcript is the only real input.
    """
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError(
            "LLM_PROVIDER=claude needs the Claude Code CLI on PATH. "
            "Install it from https://claude.com/claude-code and run `claude` once to log in."
        )

    cmd = [
        exe, "-p",
        "--output-format", "json",
        "--model", CLAUDE_MODEL,
        "--effort", CLAUDE_EFFORT,
        # Measured: Sonnet still spent ~2.2k thinking tokens (65% of output) at low
        # effort; turning thinking off cut the call from $0.070 to $0.046.
        "--settings", json.dumps({"alwaysThinkingEnabled": CLAUDE_THINKING}),
        "--system-prompt", CLAUDE_SYSTEM_PROMPT,
        "--tools", "",
        "--setting-sources", "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--strict-mcp-config",
    ]

    # Prompt goes over stdin: long transcripts overflow the Windows command-line limit.
    proc = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=CLAUDE_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p failed [{proc.returncode}]: {proc.stderr.strip() or proc.stdout.strip()}")

    try:
        data = json.loads(proc.stdout)
    except ValueError as e:
        raise RuntimeError(f"claude -p returned non-JSON output: {proc.stdout[:500]}") from e
    if data.get("is_error"):
        raise RuntimeError(f"claude -p error: {data.get('result')}")

    usage = data.get("usage") or {}
    tokens_in = sum(usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    thinking = (usage.get("output_tokens_details") or {}).get("thinking_tokens") or 0
    print(
        f"[llm/claude] {CLAUDE_MODEL}: {tokens_in} in / {usage.get('output_tokens', 0)} out tokens "
        f"({thinking} thinking), "
        f"~${data.get('total_cost_usd') or 0:.3f} API-equivalent, {(data.get('duration_ms') or 0) / 1000:.0f}s",
        flush=True,
    )
    return data.get("result") or ""


def _llm_cache_path(provider: str, prompt: str) -> Path:
    model = {"openai": OPENAI_MODEL, "gemini": GEMINI_MODEL, "claude": CLAUDE_MODEL}.get(provider, "")
    key = hashlib.sha256(f"{provider}\0{model}\0{prompt}".encode("utf-8")).hexdigest()
    return Path(LOCAL_OUTPUT_DIR) / "llm_cache" / f"{key}.txt"


def _is_json_response(raw: str) -> bool:
    from ..highlights import _parse_json_loose

    try:
        return isinstance(_parse_json_loose(raw), dict)
    except ValueError:
        return False


def call_local_llm(prompt: str) -> str:
    """Dispatch to the configured local LLM provider.

    Responses are cached on disk by (provider, model, prompt): re-running the
    same video costs no tokens. Only parseable JSON is cached, so a bad answer
    is never replayed.
    """
    provider = (LLM_PROVIDER or "openai").strip().lower()
    backends = {"openai": call_openai_llm, "gemini": call_gemini_llm, "claude": call_claude_llm}
    if provider not in backends:
        raise RuntimeError(
            f"Unknown LLM_PROVIDER={provider!r}. Use 'openai', 'gemini' or 'claude'."
        )

    cache_path = _llm_cache_path(provider, prompt)
    if cache_path.exists():
        print(f"[llm] reusing cached {provider} response: {cache_path.name[:12]}…", flush=True)
        return cache_path.read_text(encoding="utf-8")

    response = backends[provider](prompt)
    if _is_json_response(response):
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(response, encoding="utf-8")
    return response
