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

## T0. Repo hygiene
As written in `07_PROMPT_CLAUDE_CODE.md`, plus:
* Commit this file as `docs/handover/10_TICKETS.md`. Add one line to root `README.md`: "Current scope: `docs/handover/10_TICKETS.md` supersedes older handover docs where they conflict."
* Empty modules with docstrings in `pipeline/`: `__init__.py`, `config.py`, `themes.py`, `script_agent.py`, `tts.py`, `snippets.py`, `assemble.py`, `youtube_upload.py`, `runlog.py`, `run_daily.py`.
* Add `pytest` and `jsonschema` to root `requirements.txt`.
* `tests/` with one passing smoke test.
* Add `data/runs.jsonl`, `token.json`, `client_secret*.json` to `.gitignore` (the repo is public).
* `data/themes.json`: 15 stubs `{id, title_seed, angles: [], last_used: null}`. Claude AI replaces this with 30 themes before T1b.
* `data/snippets.csv`: header only (columns in T3).

Done when: `pytest` passes, `git status` clean, no binaries tracked.

## T1a. Config and themes
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
Prerequisite from Claude AI: `prompts/script_system.md`, `prompts/fewshot_01.json`, `prompts/fewshot_02.json`, 30 themes.

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
* Every mood must be in `MOODS`.
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
`data/snippets.csv`: `path,folder,moods,duration_sec,width,height,last_used`

`pipeline/snippets.py`
```python
def scan_library(cfg: Config) -> int: ...
def library_ok(cfg: Config) -> bool: ...
def pick(moods: list[str], seconds: float, used_today: set[str], cfg: Config, today: date) -> list[Snippet]: ...
def mark_used(paths: list[str], today: date, cfg: Config) -> None: ...
```
* Moods: folder defaults from `broll/queries.yaml` plus tags after a double underscore in the filename (`launch_sls_01__pressure_ignition.mp4`).
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

## T9. Dry run
Three days of `--dry-run`, then three days of real unlisted uploads. Tom checks: first 2 seconds grab, voice consistent, captions readable on a phone, no NASA endorsement implied.

## T10. Cron
Crontab for 02:00 Asia/Phnom_Penh: `cd` into the repo, activate the venv, run, log to `output/cron.log`. Machine must be on and online at 02:00.

## T11. Long form manual mode (later)
Reuse `render()` at 1920x1080 with Tom's recorded narration as the voice file. Not ticketed yet.
