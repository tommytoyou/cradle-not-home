# Tickets for Claude Code

Owner of this file: Claude AI (developer).
Where this file conflicts with `docs/handover/01` through `09`, this file wins.

## Product (stakeholder decisions, 2026-09-28)

Automated daily output, two vertical Shorts, both uploaded unlisted via YouTube Data API v3:
* **main**: 60 to 180 seconds
* **punch**: 30 seconds (hard ceiling 35)

Both come from the same daily theme and have separate original scripts. The punch is not a cut of the main.

Unchanged: Iron Within cadence (not their audio), original scripts only, real NASA public domain footage first, no AI video in v1, fail closed if the library is thin.

Removed from the automated pipeline: all long form (16 to 22 minute) video. Long form is scripted with Claude AI and narrated by Tom by hand. Build nothing for it now, but keep `render()` accepting any voice file so it can be reused later (T11).

Voice: ElevenLabs. Tom plans a Professional Voice Clone of his own voice. Until it exists, use any stock voice ID; switching is an env var change only.

Rules for every ticket:
* Implement only the ticket in front of you.
* No secrets, no media binaries, no `sources/`, `library/`, `music/`, `output/` in git.
* Every module gets `tests/test_<module>.py` that runs offline (mock network; use tiny fixture clips for ffmpeg).
* Config comes from `.env` through `pipeline/config.py`. No hardcoded paths.
* Stop and report if a ticket is marked BLOCKED and you reach the blocked part.

## Numbers that drive the design

* Shot length in Shorts: 2 to 4 seconds. A snippet file (4 to 12 s) can supply one shot per video.
* Worst case per day: 180 s main + 30 s punch at 3 s per shot is about 70 shots. Typical is about 50.
* Clip cooldown default 3 days, minimum library 200 clips. Both are config values.
* Narration: about 400 words max per day, roughly 2,500 characters of TTS.

---

## T0. Repo hygiene (DONE)
As written in `07_PROMPT_CLAUDE_CODE.md`, plus:
* Commit this file as `docs/handover/10_TICKETS.md`. Add one line to root `README.md`: "Current scope: `docs/handover/10_TICKETS.md` supersedes older handover docs where they conflict."
* Empty modules with docstrings in `pipeline/`: `__init__.py`, `config.py`, `themes.py`, `script_agent.py`, `tts.py`, `snippets.py`, `assemble.py`, `youtube_upload.py`, `runlog.py`, `run_daily.py`.
* Add `pytest` and `jsonschema` to root `requirements.txt`.
* `tests/` with one passing smoke test.
* Add `data/runs.jsonl`, `token.json`, `client_secret*.json` to `.gitignore` (the repo is public).
* `data/themes.json`: 15 stubs `{id, title_seed, angles: [], last_used: null}`. Claude AI replaces this with 30 themes before T1b.
* `data/snippets.csv`: header only (columns in T3).

Done when: `pytest` passes, `git status` clean, no binaries tracked.

## T1a. Config and themes (DONE, d441449)
Accepted as built, including same day idempotent `pick_theme` and repo relative paths.

`pipeline/config.py`
```python
@dataclass(frozen=True)
class Config:
    llm_api_key: str; llm_base_url: str; llm_model: str
    elevenlabs_api_key: str; elevenlabs_voice_id: str
    youtube_client_secrets: Path; youtube_token: Path
    library_dir: Path; music_dir: Path; output_dir: Path; data_dir: Path; font_path: Path
    min_library_clips: int = 200
    clip_cooldown_days: int = 3
    notify_webhook_url: str | None = None
def load_config() -> Config: ...   # raises ConfigError listing every missing var
```
Rewrite `.env.example` to match every field.

`pipeline/themes.py`
```python
def load_themes(path: Path) -> list[dict]: ...
def pick_theme(themes: list[dict], today: date) -> dict: ...        # never used first, then oldest last_used
def mark_used(path: Path, theme_id: str, today: date) -> None: ...  # atomic write
```

## T1b. Script agent
Prerequisites from Claude AI (delivered): `prompts/script_system.md`, `prompts/fewshot_01.json`, `prompts/fewshot_02.json`, `data/themes.json` with 30 themes.

Theme contract change: each theme now also carries `facts`, a list of vetted fact strings (may be empty). Extend `load_themes` to accept it (list of strings, default empty) and add a test.

Prompt assembly:
* System message: contents of `prompts/script_system.md`.
* Few-shots as alternating turns: user = the example's `theme` as JSON, assistant = the example's `output` as JSON. Then user = today's theme as JSON (without `last_used`).
* The model returns `{"videos": [...]}`. Code adds `id` (ISO date) and `theme_id`, then appends the credit block to each description.

Package schema (replaces `video_package.json` in `05_DATA_CONTRACTS.md`), stored at `pipeline/schemas/daily_package.json`:
```json
{
  "id": "2026-09-28",
  "theme_id": "string",
  "videos": [
    {
      "format": "main | punch",
      "title": "string, max 90 chars",
      "description": "string",
      "tags": ["string"],
      "duration_target_sec": 120,
      "hook_text": "max 6 words, shown on the first frame",
      "script_blocks": [{"text": "string", "moods": ["string"], "emphasis": ["word"]}]
    }
  ]
}
```
`pipeline/script_agent.py`
```python
MOODS: frozenset[str]   # closed list, from broll/queries.yaml moods
def build_package(theme: dict, cfg: Config, today: date) -> dict: ...   # one LLM call returns both videos
def validate_package(pkg: dict) -> list[str]: ...                      # empty list means valid
def estimate_duration_sec(text: str, wpm: int = 140) -> float: ...
```
* Exactly one `main` and one `punch`.
* Estimated duration: main 60 to 180 s, punch 22 to 32 s. Otherwise retry up to 3 times, then raise.
* Every mood must be in `MOODS`; 1 to 3 per block.
* Each block 5 to 30 words. Every `emphasis` word must appear in that block's text.
* Fact guard: any digit sequence in any script block (for example `2006`, `400`, `11.2`) must appear in that theme's `facts`. Otherwise reject and retry. This stops invented statistics in an unattended pipeline.
* `hook_text` max 6 words, `title` max 90 characters.
* Append the credit block from `02_CHANNEL_FORMAT.md` to every description in code. Never trust the LLM with it.
* Keep the LLM client in one function so the vendor can change.

## T2. TTS
`pipeline/tts.py`
```python
@dataclass
class BlockAudio: index: int; path: Path; duration_sec: float
def synth_blocks(video: dict, cfg: Config, workdir: Path) -> list[BlockAudio]: ...
def concat_voice(blocks: list[BlockAudio], out: Path, gap_sec: float = 0.35) -> float: ...
```
* One request per block so cuts align with lines.
* Cache by sha256 of `voice_id + text`.
* Duration via `ffprobe`.
* Voice ID from config only. Test with a mocked client.

## T3. Snippet index and picker
`data/snippets.csv`: `path,folder,moods,duration_sec,width,height,nasa_id,credit,faces,logo_risk,last_used` (update the T0 header)

* `nasa_id`, `credit`: left blank by the scanner, filled by hand in the CSV when known. A rescan must preserve hand edited values.
* `faces`, `logo_risk`: 0 or 1, set from reserved filename tags `faces` and `logo` (these are flags, not moods).
* Picker never selects `logo_risk=1`. `faces=1` is allowed, but never as the first shot of a video.

`pipeline/snippets.py`
```python
def scan_library(cfg: Config) -> int: ...
def library_ok(cfg: Config) -> bool: ...
def pick(moods: list[str], seconds: float, used_today: set[str], cfg: Config, today: date) -> list[Snippet]: ...
def mark_used(paths: list[str], today: date, cfg: Config) -> None: ...
```
* Moods: folder defaults from `broll/queries.yaml` plus tags after a double underscore in the filename (`launch_sls_01__pressure_ignition_faces.mp4`). Unknown tags are logged and ignored.
* Reject under 1080 px tall or outside 4 to 12 s; log rejects.
* Order: mood match, then least recently used. Never in `used_today` (shared across both videos of the day), never inside cooldown.
* Mood pool empty: fall back to any mood, then fail closed.
* CLI: `python -m pipeline.snippets scan`.

## T4a. Timeline
`pipeline/assemble.py`
```python
@dataclass
class Shot: snippet: Path; src_start: float; duration: float; zoom: bool
@dataclass
class Caption: start: float; end: float; text: str; emphasis: list[str]
def build_timeline(video: dict, blocks: list[BlockAudio], cfg: Config, today: date, used_today: set[str]) -> tuple[list[Shot], list[Caption]]: ...
```
* Fill each block's audio with 2 to 4 s shots; hard cut on block boundaries.
* `src_start`: skip the first second of each snippet, pick from the middle.
* Captions on every line, split into chunks of 3 to 5 words timed across the block.

## T4b. Render
```python
def render(shots: list[Shot], captions: list[Caption], voice: Path, hook_text: str, cfg: Config, out: Path) -> Path: ...
```
* Output 1080x1920, 30 fps, H.264 + AAC. FFmpeg filter graphs, not MoviePy frame loops.
* Per shot: center crop 16:9 to 9:16, slow zoom when `zoom`, dark grade (contrast up, crushed blacks, slight desaturation, grain), source audio dropped.
* Font: default Anton (SIL Open Font License). Commit the `.ttf` and its `OFL.txt` under `assets/fonts/` and point `FONT_PATH` at it.
* Captions: generated `.ass` file, large bold font centered in the lower third area safe from the Shorts UI (keep the bottom 20% and right 15% clear); `emphasis` words in an accent color.
* First frame: `hook_text` burned in over the first shot for the first 1.5 s.
* Music: random track from `music/`, sidechain ducked under the voice, whole mix at about -14 LUFS.
* Test with fixture clips and a generated tone. BLOCKED only for real runs, on Tom's `music/` folder.

Done when: a 30 s render from fixture clips is 1080x1920, plays, has no source audio, and captions stay inside the safe area.

## T5. (removed)
Thumbnails are dropped. Custom thumbnails for Shorts are not reliably settable through the API, so the first frame (T4b) does the job.

## T6. YouTube upload
`pipeline/youtube_upload.py`
```python
def get_credentials(cfg: Config) -> Credentials: ...   # installed app OAuth, token cached at cfg.youtube_token
def upload(video_path: Path, video: dict, cfg: Config, privacy: str = "unlisted") -> str: ...
```
* Resumable upload, retry on 5xx.
* `privacy` other than `unlisted` requires an explicit CLI flag.
* Set `status.containsSyntheticMedia = true` on every upload (synthetic voice).
* `#Shorts` at the end of the description.
* BLOCKED until Tom creates the OAuth client and the channel. Test with a mocked service.

## T7. Daily runner
`pipeline/run_daily.py`, `pipeline/runlog.py`
```python
def run(today: date, cfg: Config, dry_run: bool = False) -> list[RunRecord]: ...
def append_run(record: RunRecord, cfg: Config) -> None: ...   # data/runs.jsonl, one line per video
def notify(records: list[RunRecord], cfg: Config) -> None: ...  # print + log; POST to webhook if set
```
Order: library check, theme, package, then for main and punch: TTS, timeline, render, upload. Then mark theme and clips used, log, notify.
* Run record: `date, package_id, format, youtube_id, visibility, duration_sec, snippet_count, status, notes`.
* If either video fails before upload, upload neither and mark nothing used.
* `--dry-run` does everything except upload.
* Files go in `output/YYYY-MM-DD/{main,punch}.mp4`.

## T8. Tom cuts clips
Not a Code ticket. Cut from `broll/SOURCE_LIST.md` with `cut_snippet.sh`, keeping the subject in the middle third of the frame (it gets center cropped to vertical). Then run `python -m pipeline.snippets scan`. Target 200 clips before T9.

## T8a. Autocut and review page (DONE)
`broll/autocut.py`, `broll/review_template.html`, `broll/apply_review.py`
* `python -m broll.autocut`: for each new video in `broll/sources/`, PySceneDetect finds the cuts, scenes split into 4-12 s clips, and clips under 1080 px tall or near black are dropped. Near-static clips are kept, marked `static` in `candidates.json`, and shown undecided in a "Flagged static" section at the bottom of the page. The rest go to `library/_inbox/<nasa_id>_t<start>.mp4`, with a thumbnail and a 3 s preview in `_inbox/_review/`. `_inbox/candidates.json` keeps the exact `nasa_id` and which sources are done.
* `library/_inbox/review.html`: keep/reject, target folder (`FOLDERS` minus `_inbox`), mood and `faces`/`logo` checkboxes, vertical crop guides. Exports `review_decisions.json`.
* `python -m broll.apply_review <decisions.json>`: checks everything first, then moves kept clips to `<folder>/<name>__<tags>.mp4`, deletes rejects, and rescans with `nasa_id` and `credit` filled in. Undecided clips stay for the next review.
* The scanner never indexes `_inbox`, so unreviewed clips cannot be picked.

## T9. Dry run
Three days of `--dry-run`, then three days of real unlisted uploads. Tom checks: first 2 seconds grab, voice consistent, captions readable on a phone, no NASA endorsement implied.

## T10. Scheduler (Windows)
The machine is Windows with PowerShell. Use Task Scheduler, not cron:
* `scripts/run_daily.ps1`: `Set-Location` to the repo, activate the venv, run `python -m pipeline.run_daily`, append output to `output/scheduler.log`.
* `scripts/install_task.ps1`: registers a daily task at 02:00 local time (Asia/Phnom_Penh) with "run whether user is logged on or not" and "wake the computer to run".
* The machine must be powered and online at 02:00.

## T11. Long form manual mode (later)
Reuse `render()` at 1920x1080 with Tom's recorded narration as the voice file. Not ticketed yet.
