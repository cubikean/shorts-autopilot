# AGENTS.md

Guide for coding agents working on this repo. The user-facing docs (setup, commands, Notion
workflow) are in [README.md](README.md), in French.

## What this is

A Windows-hosted autopilot that turns fresh videos from whitelisted YouTube / Twitch channels into
9:16 subtitled shorts and publishes them on YouTube and TikTok, with Notion as the queue and dashboard.

```
discover/watch → Notion "Vidéos à traiter" → process → Notion "Shorts" → publish → YouTube / TikTok
                  (feed/)                      (pipeline.py)                (publish/)
```

`process` runs `pipeline.generate_shorts`: yt-dlp download → faster-whisper transcript → audio
markers + comment hot zones (`local/signals.py`) → LLM highlight picking (`highlights.py`) →
ffmpeg/OpenCV crop, reframe and burned subtitles (`local/clipper.py`, `reframe.py`, `subtitles.py`).

| Path | Role |
|---|---|
| `feed.py` | CLI entry point for every scheduled / manual command |
| `main.py` | one-off: shorts from a single URL or local file, no Notion |
| `shorts_generator/config.py` | every setting, read from `.env` |
| `shorts_generator/feed/` | discovery (YouTube, Twitch), Notion client, queue runner, `replay` grading |
| `shorts_generator/local/` | download, transcription, LLM backends, signals, render, cleanup |
| `shorts_generator/publish/` | YouTube / TikTok uploaders, publish runner, daily stats |
| `scripts/schedule_windows.ps1` | registers the `ShortsFeed-*` scheduled tasks |
| `sources.json` | the user's channel whitelist (the user edits it; don't commit their changes unasked) |

## Running things

- Always use the venv: `venv\Scripts\python` (Python 3.10). On Windows set `PYTHONIOENCODING=utf-8`,
  titles are full of emoji and the console is cp1252.
- **Changing how moments are picked** (prompt, signals, model): measure it with `bench.py` before
  shipping. `bench.py build` caches older videos that already have YouTube's most-replayed curve;
  `bench.py run <variants>` grades the picks on it in minutes. Tune on dev, confirm once on `--holdout`.
- There is no test suite. Verify with the dry-run flags (`discover --dry-run`, `publish --dry-run`,
  `replay --dry-run`, `clean --dry-run`) and with scratch scripts that import the module under test.
  Write scratch output outside `output/`, which the pipeline treats as its cache.
- `feed.py` commands take a lock per command (`single_run`), and the scheduled tasks may be running
  when you start one by hand: check `output/logs/` before a manual `process` or `publish`.
- To retry a video in error, set its Notion `Statut` back to `À traiter`; for a short, set
  `Publication` back to `Validé`.

## Rules

- **Public repo.** Never commit `.env`, `*_token.json`, `client_secret*.json`, or anything under
  `output/`. Scan staged diffs for secrets before each commit.
- **Commit and push each verified change** without asking: imperative subject saying what changed
  for the user, a body explaining why, the co-author trailer. Stage files by name, never `git add -A`.
- **Never let several YouTube shorts go public at once**, manual runs included: publishing goes
  through `publish` and its `YOUTUBE_MIN_GAP_MINUTES` spacing. Don't use `watch` or `publish` to
  test, use `--dry-run`.
- **Freshness first.** Our data: clips posted ~2 h after their source took off reached 5k-480k
  TikTok views, those posted 8-48 h later almost never passed 2k. Don't add steps that delay a
  fresh short (queues, batching, long schedules); old sources and unpublished shorts are rejected
  (`FEED_MAX_AGE_HOURS`, `SHORTS_STALE_DAYS`, `YOUTUBE_MAX_WAIT_MINUTES`).
- **TikTok** posts go to the inbox as drafts (sandbox app, not Direct Post); TikTok refuses new
  uploads while 5 drafts are pending (`feed.py drafts` lists them).
- Code, comments, logs and commits are in English. Everything the user reads in Notion, the README,
  notifications and generated titles/descriptions is in French.
- Notion property and option names are French and matched by exact string (`Statut`, `À traiter`,
  `Centile revu`...). Renaming one in code breaks existing databases; add columns idempotently
  (see `Notion.ensure_columns`, `ensure_publish_schema`).
- Match the existing style: small functions, docstrings that explain *why*, comments only where
  the reason isn't obvious, best-effort signals that log and return nothing instead of failing a video.

## Things that bit us

- **LLM = `claude -p`** in bare mode (`local/llm.py`), billed to the user's Claude plan. Unattended
  runs authenticate with `CLAUDE_CODE_OAUTH_TOKEN` in `.env` (from `claude setup-token`); an expired
  interactive login fails every video with "OAuth session expired".
- LLM answers are cached on disk by exact prompt (`output/llm_cache/`), only when they hold usable
  highlights. Any prompt change means fresh calls; signals that feed the prompt must be deterministic
  (comment mentions are cached next to the source for that reason).
- **Twitch VODs over ~26.5 h**: MPEG-TS timestamps wrap, so seeking the full playlist never reaches a
  late moment. `download_section_local` cuts from a trimmed playlist of just the needed segments.
- YouTube's **"most replayed" heatmap** only appears ~7-10 days after release: useless for picking
  moments on fresh videos, used by `feed.py replay` to grade them afterwards (`Centile revu`, 50 = random).
- `.env` values like `KEY=   # note` are read by dotenv as `# note`; `config._env` treats them as unset.
- Whisper hallucinates text on outro music ("Sous-titrage ST' 501") and rarely writes laughter.
