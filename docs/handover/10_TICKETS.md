# Tickets

Product is **long-form first** (16–22 min). Shorts are phase 2. Do not switch the repo to Shorts-only.

---

## T0 — Repo hygiene (do this now)

Implement only this ticket.

1. Keep `docs/handover/` as the spec. Do not rewrite it.
2. Confirm `.gitignore` ignores `.env`, `sources/`, `library/`, `music/`, `output/`, `*.pickle`, `__pycache__/`, `catalog.json`.
3. Confirm `.env.example` has empty `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `LLM_API_KEY`, `LLM_BASE_URL`, `YOUTUBE_CLIENT_SECRETS`.
4. Add root `requirements.txt` with: requests, pyyaml, moviepy, pillow, google-api-python-client, google-auth-oauthlib, python-dotenv.
5. Create `data/themes.json` with at least 15 stubs: `{ "id", "title_seed", "angles": [], "last_used": null }`.
6. Create `data/snippets.csv` with header only:
   `file,folder,moods,duration_sec,nasa_id,credit,faces,logo_risk,last_used_date`
7. Create empty modules with a one-line docstring each:
   - `pipeline/script_agent.py`
   - `pipeline/tts.py`
   - `pipeline/assemble.py`
   - `pipeline/thumbnail.py`
   - `pipeline/youtube_upload.py`
   - `pipeline/run_daily.py`
8. Do not download NASA video. Do not commit binaries. Do not add Shorts pipelines.

Done when those files exist and `git status` is clean after one commit: `T0: repo hygiene`.

---

## T1 — Script agent (not now)

`pipeline/script_agent.py` reads a theme from `data/themes.json` and writes `output/video_package.json` using `prompts/script_system.md`.

## T2 — TTS (not now)

ElevenLabs (edge-tts stub allowed for offline).

## T3+ — See `09_BACKLOG.md`
