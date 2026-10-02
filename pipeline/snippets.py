"""NASA clip index and picker over ``data/snippets.csv`` (T3).

Scans the gitignored library into the CSV, deriving moods from folder defaults
in ``broll/queries.yaml`` plus filename tags after a double underscore, and
rejecting clips under 1080 px tall or outside 4-12 s. Picking orders by mood
match then least recently used, skipping anything already used today (shared
across both videos) or inside the cooldown, and fails closed when the library is
thin. Also the ``python -m pipeline.snippets scan`` entry point.

CSV notes: ``path`` is library-relative with forward slashes and is the key used
by ``used_today`` and :func:`mark_used`. ``moods`` is pipe-separated. ``nasa_id``
and ``credit`` are left blank by the scanner for hand editing, and a rescan
preserves them along with ``last_used``; ``broll/apply_review.py`` fills them
for reviewed clips through ``provenance``. ``faces`` and ``logo_risk`` are flags
(0/1) owned by the scanner, set from the reserved ``faces`` and ``logo``
filename tags. Top-level folders starting with ``_`` (the review inbox) are
never indexed.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml

from pipeline.config import ROOT, Config, load_config
from pipeline.script_agent import MOODS

log = logging.getLogger(__name__)

QUERIES_PATH = ROOT / "broll" / "queries.yaml"
CSV_NAME = "snippets.csv"
FIELDNAMES = [
    "path", "folder", "moods", "duration_sec", "width", "height",
    "nasa_id", "credit", "faces", "logo_risk", "last_used",
]

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".mkv"}
MIN_HEIGHT = 1080
MIN_DURATION_SEC = 4.0
MAX_DURATION_SEC = 12.0

# The architect's capacity math (10_TICKETS.md "Numbers that drive the design")
# budgets ~70 shots for a 210 s day, i.e. 3 s per shot. T4a cuts 2-4 s shots.
AVERAGE_SHOT_SEC = 3.0

FACES_TAG = "faces"
LOGO_TAG = "logo"


class SnippetError(RuntimeError):
    """Raised when the library or index cannot satisfy the request."""


@dataclass(frozen=True)
class Snippet:
    path: str
    folder: str
    moods: tuple[str, ...]
    duration_sec: float
    width: int
    height: int
    nasa_id: str
    credit: str
    faces: bool
    logo_risk: bool
    last_used: date | None
    full_path: Path


# --- ffprobe ---------------------------------------------------------------
# pipeline/tts.py has sibling helpers; both should move to a shared media
# module when a ticket allows, rather than one importing the other's privates.


def _binary(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise SnippetError(f"{name} not found on PATH; install FFmpeg")
    return found


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-3:]
        raise SnippetError(f"{Path(cmd[0]).name} failed: " + " / ".join(tail))
    return result.stdout


def probe_clip(path: Path) -> tuple[float, int, int]:
    """Duration, width and height of the first video stream, in one ffprobe call."""
    output = _run(
        [
            _binary("ffprobe"),
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "json",
            str(path),
        ]
    )
    try:
        data = json.loads(output)
        stream = data["streams"][0]
        return float(data["format"]["duration"]), int(stream["width"]), int(stream["height"])
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SnippetError(f"ffprobe gave no usable video stream for {path}") from exc


# --- moods and tags --------------------------------------------------------


@lru_cache(maxsize=1)
def folder_moods() -> dict[str, frozenset[str]]:
    """Default moods per library folder, from the b-roll query catalogue."""
    data = yaml.safe_load(QUERIES_PATH.read_text(encoding="utf-8"))
    defaults: dict[str, set[str]] = {}
    for entry in data["queries"]:
        defaults.setdefault(entry["folder"], set()).update(entry.get("moods", []))
    return {folder: frozenset(moods) for folder, moods in defaults.items()}


def parse_tags(stem: str) -> tuple[frozenset[str], bool, bool]:
    """Moods and the faces/logo flags from the part after a double underscore."""
    if "__" not in stem:
        return frozenset(), False, False

    moods: set[str] = set()
    faces = logo = False
    for tag in stem.split("__", 1)[1].split("_"):
        if not tag:
            continue
        if tag == FACES_TAG:
            faces = True
        elif tag == LOGO_TAG:
            logo = True
        elif tag in MOODS:
            moods.add(tag)
        else:
            log.warning("ignoring unknown tag %r in %s", tag, stem)
    return frozenset(moods), faces, logo


# --- the CSV ---------------------------------------------------------------


def csv_path(cfg: Config) -> Path:
    return Path(cfg.data_dir) / CSV_NAME


def _read_rows(cfg: Config) -> list[dict]:
    path = csv_path(cfg)
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _write_rows(cfg: Config, rows: list[dict]) -> None:
    path = csv_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in FIELDNAMES})
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _to_snippet(row: dict, cfg: Config) -> Snippet:
    stamp = (row.get("last_used") or "").strip()
    path = row["path"]
    return Snippet(
        path=path,
        folder=row.get("folder", ""),
        moods=tuple(m for m in (row.get("moods") or "").split("|") if m),
        duration_sec=float(row["duration_sec"]),
        width=int(row["width"]),
        height=int(row["height"]),
        nasa_id=row.get("nasa_id", ""),
        credit=row.get("credit", ""),
        faces=row.get("faces") == "1",
        logo_risk=row.get("logo_risk") == "1",
        last_used=date.fromisoformat(stamp) if stamp else None,
        full_path=Path(cfg.library_dir) / path,
    )


def load_snippets(cfg: Config) -> list[Snippet]:
    """Every indexed clip, in CSV order."""
    try:
        return [_to_snippet(row, cfg) for row in _read_rows(cfg)]
    except (KeyError, ValueError) as exc:
        raise SnippetError(f"{csv_path(cfg)} is malformed: {exc}") from exc


# --- scanning --------------------------------------------------------------


def scan_library(cfg: Config, provenance: Mapping[str, tuple[str, str]] | None = None) -> int:
    """Index the library into the CSV; returns the number of accepted clips.

    Top-level folders starting with ``_`` (the review ``_inbox``) are skipped.
    ``provenance`` maps a library path to ``(nasa_id, credit)`` and fills those
    columns where they are still blank; values already in the CSV win.
    """
    library = Path(cfg.library_dir)
    if not library.is_dir():
        raise SnippetError(f"library directory not found: {library}")

    preserved = {row["path"]: row for row in _read_rows(cfg)}
    defaults = folder_moods()
    rows: list[dict] = []
    rejected = 0

    for file in sorted(library.rglob("*")):
        if not file.is_file() or file.suffix.lower() not in VIDEO_SUFFIXES:
            continue

        key = file.relative_to(library).as_posix()
        if key.startswith("_"):
            continue
        try:
            duration, width, height = probe_clip(file)
        except SnippetError as exc:
            log.warning("reject %s: %s", key, exc)
            rejected += 1
            continue

        if height < MIN_HEIGHT:
            log.warning("reject %s: %d px tall, need at least %d", key, height, MIN_HEIGHT)
            rejected += 1
            continue
        if not MIN_DURATION_SEC <= duration <= MAX_DURATION_SEC:
            log.warning(
                "reject %s: %.2f s, need %g-%g s",
                key, duration, MIN_DURATION_SEC, MAX_DURATION_SEC,
            )
            rejected += 1
            continue

        relative_parent = file.parent.relative_to(library).as_posix()
        folder = "" if relative_parent == "." else relative_parent
        tag_moods, faces, logo = parse_tags(file.stem)
        moods = sorted(defaults.get(folder, frozenset()) | tag_moods)
        if not moods:
            log.warning("%s has no moods from its folder or filename", key)

        prior = preserved.get(key, {})
        nasa_id, credit = (provenance or {}).get(key, ("", ""))
        rows.append(
            {
                "path": key,
                "folder": folder,
                "moods": "|".join(moods),
                "duration_sec": f"{duration:.3f}",
                "width": str(width),
                "height": str(height),
                # Hand-edited columns survive a rescan.
                "nasa_id": prior.get("nasa_id") or nasa_id,
                "credit": prior.get("credit") or credit,
                "faces": "1" if faces else "0",
                "logo_risk": "1" if logo else "0",
                "last_used": prior.get("last_used", ""),
            }
        )

    _write_rows(cfg, rows)
    log.info("indexed %d clips, rejected %d", len(rows), rejected)
    return len(rows)


def library_ok(cfg: Config) -> bool:
    """True when the index holds at least ``cfg.min_library_clips`` clips."""
    return len(_read_rows(cfg)) >= cfg.min_library_clips


# --- picking ---------------------------------------------------------------


def _is_eligible(snippet: Snippet, used_today: set[str], cfg: Config, today: date) -> bool:
    if snippet.logo_risk:
        return False
    if snippet.path in used_today:
        return False
    if snippet.last_used is not None:
        if (today - snippet.last_used).days < cfg.clip_cooldown_days:
            return False
    return True


def _order_key(snippet: Snippet, wanted: set[str]) -> tuple:
    """Mood match first, then least recently used, then a stable tiebreak."""
    matches = len(set(snippet.moods) & wanted)
    return (
        -matches,
        snippet.last_used is not None,          # never used sorts first
        snippet.last_used or date.min,
        snippet.path,
    )


def _face_last(chosen: list[Snippet]) -> list[Snippet]:
    """Keep a face off the front; T4a takes the first shot from the front."""
    if not chosen or not chosen[0].faces:
        return chosen
    for index, snippet in enumerate(chosen):
        if not snippet.faces:
            return [snippet] + chosen[:index] + chosen[index + 1:]
    log.warning("every picked clip is flagged faces; first shot will show a face")
    return chosen


def pick(
    moods: list[str],
    seconds: float,
    used_today: set[str],
    cfg: Config,
    today: date,
) -> list[Snippet]:
    """Pick enough clips to cover ``seconds`` of footage, best mood match first."""
    wanted = set(moods)
    needed = max(1, math.ceil(seconds / AVERAGE_SHOT_SEC))

    eligible = [s for s in load_snippets(cfg) if _is_eligible(s, used_today, cfg, today)]

    if not any(set(s.moods) & wanted for s in eligible):
        log.warning("no eligible clip matches moods %s; falling back to any mood",
                    sorted(wanted))

    if len(eligible) < needed:
        raise SnippetError(
            f"need {needed} clips for {seconds:.1f}s but only {len(eligible)} are "
            f"eligible (cooldown {cfg.clip_cooldown_days}d, {len(used_today)} used today)"
        )

    chosen = sorted(eligible, key=lambda s: _order_key(s, wanted))[:needed]
    return _face_last(chosen)


def mark_used(paths: list[str], today: date, cfg: Config) -> None:
    """Stamp ``today`` on every named clip; atomic write."""
    rows = _read_rows(cfg)
    wanted = set(paths)
    stamp = today.isoformat()

    seen = set()
    for row in rows:
        if row["path"] in wanted:
            row["last_used"] = stamp
            seen.add(row["path"])

    unknown = wanted - seen
    if unknown:
        raise SnippetError(f"not in the index: {sorted(unknown)}")

    _write_rows(cfg, rows)


# --- CLI -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = list(sys.argv[1:] if argv is None else argv)

    if args != ["scan"]:
        print("usage: python -m pipeline.snippets scan", file=sys.stderr)
        return 2

    cfg = load_config()
    count = scan_library(cfg)
    print(f"{count} clips indexed in {csv_path(cfg)}")
    if library_ok(cfg):
        print("library ok")
    else:
        print(f"LIBRARY THIN: {count} of {cfg.min_library_clips} clips")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
