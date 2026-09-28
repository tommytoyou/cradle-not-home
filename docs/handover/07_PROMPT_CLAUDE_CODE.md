# Prompt for Claude Code (first session)

Repo: `tommytoyou/cradle-not-home` (private).

You are the coder. Implement only the current ticket. Do not redesign the channel.

## Ticket 0 — repo hygiene

1. Keep `docs/handover/` as source of truth.
2. NASA spike lives in `broll/`.
3. Confirm `.gitignore` excludes `.env`, `sources/`, `library/`, `music/`, `output/`.
4. Confirm `.env.example` exists.
5. Add root `requirements.txt`: requests, pyyaml, moviepy, pillow, google-api-python-client, google-auth-oauthlib, python-dotenv.
6. Add `data/themes.json` with at least 15 theme stubs (id + title_seed).
7. Add `data/snippets.csv` header only.
8. Add `pipeline/` empty modules with docstrings matching `docs/handover/03_ARCHITECTURE.md`.
9. Do not download NASA video in CI. Do not commit binaries.

When Ticket 0 is merged, Ticket 1 is `pipeline/script_agent.py`.
