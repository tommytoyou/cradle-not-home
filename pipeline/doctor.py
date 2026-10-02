"""Health check for the daily machine (T12): ``python -m pipeline.doctor``.

Checks FFmpeg and ffprobe on PATH, every required ``.env`` value, at least one
music track, enough usable clips for ``MIN_LIBRARY_CLIPS``, the font, the
YouTube token and the Task Scheduler job. Prints one line per check with the
fix when it fails, and exits 0 only when every required check passes. The
token is a warning only while uploads are manual.

Each check reads ``.env`` on its own, so one missing value (say the voice id)
does not hide the state of everything else.
"""

from __future__ import annotations

import argparse
import csv
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from pipeline.assemble import MUSIC_SUFFIXES
from pipeline.config import (
    OPTIONAL_INTS,
    REQUIRED_PATHS,
    REQUIRED_TEXT,
    Config,
    read_env,
    resolve_path,
)
from pipeline.snippets import CSV_NAME

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"

# Must match the default -TaskName in scripts/install_task.ps1.
TASK_NAME = "Cradle Not Home daily"
INSTALL_TASK = r"powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_task.ps1"
DEFAULT_MIN_CLIPS = Config.__dataclass_fields__["min_library_clips"].default
INBOX = "_inbox"


@dataclass(frozen=True)
class Result:
    name: str
    status: str
    detail: str
    fix: str = ""

    def line(self) -> str:
        text = f"[{self.status}] {self.name}: {self.detail}"
        return f"{text} | fix: {self.fix}" if self.fix and self.status != PASS else text


def _env_path(env: Mapping[str, str], key: str) -> Path | None:
    value = (env.get(key) or "").strip()
    return resolve_path(value) if value else None


def _not_set(name: str, key: str) -> Result:
    return Result(name, FAIL, f"{key} is not set", f"set {key} in .env (see .env.example)")


# --- checks ----------------------------------------------------------------


def check_binary(name: str) -> Result:
    found = shutil.which(name)
    if found:
        return Result(name, PASS, found)
    return Result(name, FAIL, "not on PATH",
                  "winget install Gyan.FFmpeg, then open a new PowerShell window")


def check_env(env: Mapping[str, str]) -> Result:
    name = ".env values"
    missing = [key for key in [*REQUIRED_TEXT, *REQUIRED_PATHS] if not (env.get(key) or "").strip()]
    invalid = []
    for key in OPTIONAL_INTS:
        value = (env.get(key) or "").strip()
        if value and not value.lstrip("-").isdigit():
            invalid.append(f"{key}={value!r}")
    if not missing and not invalid:
        return Result(name, PASS, f"all {len(REQUIRED_TEXT) + len(REQUIRED_PATHS)} required values set")
    problems = []
    if missing:
        problems.append("missing " + ", ".join(sorted(missing)))
    if invalid:
        problems.append("not whole numbers: " + ", ".join(invalid))
    return Result(name, FAIL, "; ".join(problems), "fill them in .env (Copy-Item .env.example .env to start)")


def check_music(env: Mapping[str, str]) -> Result:
    name = "music"
    folder = _env_path(env, "MUSIC_DIR")
    if folder is None:
        return _not_set(name, "MUSIC_DIR")
    suffixes = ", ".join(sorted(MUSIC_SUFFIXES))
    tracks = [p for p in folder.iterdir()
              if p.is_file() and p.suffix.lower() in MUSIC_SUFFIXES] if folder.is_dir() else []
    if tracks:
        return Result(name, PASS, f"{len(tracks)} track(s) in {folder}")
    where = "no tracks in" if folder.is_dir() else "folder missing:"
    return Result(name, FAIL, f"{where} {folder}", f"copy at least one {suffixes} file into {folder}")


def check_library(env: Mapping[str, str]) -> Result:
    """Usable = indexed, file still present, and not logo-flagged (never picked)."""
    name = "library"
    library, data = _env_path(env, "LIBRARY_DIR"), _env_path(env, "DATA_DIR")
    if library is None:
        return _not_set(name, "LIBRARY_DIR")
    if data is None:
        return _not_set(name, "DATA_DIR")
    raw = (env.get("MIN_LIBRARY_CLIPS") or "").strip()
    minimum = int(raw) if raw.isdigit() else DEFAULT_MIN_CLIPS

    waiting = len(list((library / INBOX).glob("*.mp4"))) if (library / INBOX).is_dir() else 0
    later = f"; {waiting} waiting in {INBOX} review" if waiting else ""
    index = data / CSV_NAME
    if not index.is_file():
        return Result(name, FAIL, f"no index at {index}, need {minimum} clips{later}",
                      "python -m pipeline.snippets scan")

    with index.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    usable = sum(1 for row in rows
                 if row.get("logo_risk") != "1" and (library / row.get("path", "")).is_file())
    detail = f"{usable} usable of {len(rows)} indexed, need {minimum}{later}"
    if usable >= minimum:
        return Result(name, PASS, detail)
    fix = ("review the inbox, then python -m broll.apply_review <decisions.json>" if waiting
           else "cut more clips: python -m broll.autocut, review, python -m broll.apply_review")
    return Result(name, FAIL, detail, fix)


def check_font(env: Mapping[str, str]) -> Result:
    name = "font"
    font = _env_path(env, "FONT_PATH")
    if font is None:
        return _not_set(name, "FONT_PATH")
    if font.is_file():
        return Result(name, PASS, str(font))
    return Result(name, FAIL, f"missing: {font}",
                  "point FONT_PATH at a .ttf/.otf (assets/fonts/Anton-Regular.ttf ships with the repo)")


def check_token(env: Mapping[str, str]) -> Result:
    """Only a warning: uploads are manual until the YouTube API audit passes."""
    name = "YouTube token"
    token = _env_path(env, "YOUTUBE_TOKEN")
    if token is not None and token.is_file():
        return Result(name, PASS, str(token))
    where = str(token) if token else "YOUTUBE_TOKEN not set"
    return Result(name, WARN, f"missing ({where}); fine while uploads are manual",
                  "before going -Live: python -m pipeline.youtube_upload auth")


def scheduled_task(task_name: str) -> tuple[str, str] | None:
    """``(state, action arguments)`` of a Task Scheduler job, or None if absent."""
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        return None
    quoted = task_name.replace("'", "''")
    script = (
        f"try {{ $t = Get-ScheduledTask -TaskName '{quoted}' -ErrorAction Stop }} catch {{ exit 3 }}; "
        "$a = ($t.Actions | ForEach-Object { $_.Arguments }) -join ' '; "
        "Write-Output ('{0}|{1}' -f $t.State, $a)"
    )
    result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True)
    if result.returncode != 0 or "|" not in result.stdout:
        return None
    state, arguments = result.stdout.strip().split("|", 1)
    return state, arguments


def check_task(task_name: str) -> Result:
    name = "scheduled task"
    if sys.platform != "win32":
        return Result(name, FAIL, "Task Scheduler needs Windows", "run on the Windows machine")
    found = scheduled_task(task_name)
    if found is None:
        return Result(name, FAIL, f"'{task_name}' is not registered",
                      f"from an Administrator PowerShell: {INSTALL_TASK}")
    state, arguments = found
    mode = "LIVE uploads" if "-Live" in arguments.split() else "dry run"
    if state == "Disabled":
        return Result(name, FAIL, f"'{task_name}' is disabled ({mode})",
                      f"Enable-ScheduledTask -TaskName '{task_name}'")
    return Result(name, PASS, f"'{task_name}' {state.lower()}, {mode}")


# --- running ---------------------------------------------------------------


def run_checks(env: Mapping[str, str], task_name: str = TASK_NAME) -> list[tuple[Result, bool]]:
    """Every check paired with whether it is required."""
    checks: list[tuple[Callable[[], Result], bool]] = [
        (lambda: check_binary("ffmpeg"), True),
        (lambda: check_binary("ffprobe"), True),
        (lambda: check_env(env), True),
        (lambda: check_music(env), True),
        (lambda: check_library(env), True),
        (lambda: check_font(env), True),
        (lambda: check_token(env), False),
        (lambda: check_task(task_name), True),
    ]
    return [(check(), required) for check, required in checks]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.doctor", description=__doc__.split("\n\n")[0])
    parser.add_argument("--task-name", default=TASK_NAME, help=f"default: {TASK_NAME}")
    args = parser.parse_args(argv)

    results = run_checks(read_env(), args.task_name)
    for result, _ in results:
        print(result.line())

    failed = [r for r, required in results if required and r.status != PASS]
    warned = [r for r, _ in results if r.status == WARN]
    passed = len(results) - len(failed) - len(warned)
    print(f"doctor: {passed} passed, {len(failed)} failed, {len(warned)} warning(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
