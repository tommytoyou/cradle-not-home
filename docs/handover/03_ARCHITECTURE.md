# Architecture

Daily job at 02:00 local: pick theme, LLM package, TTS, pick snippets, assemble, thumbnail, upload UNLISTED, log, notify.

Target layout: docs/handover, broll, library (gitignored), sources (gitignored), music (gitignored), data/, pipeline/, prompts/.

v1 stack: LLM API, ElevenLabs, local NASA cuts, FFmpeg/MoviePy, Pillow thumbs, YouTube Data API v3, python run_daily.py then cron.

Fail closed if snippet library has fewer than 80 clips.
Never commit secrets.
