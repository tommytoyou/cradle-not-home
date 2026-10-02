"""Apply the decisions exported from ``_inbox/review.html`` (T8a).

Kept clips move from ``<library>/_inbox/`` into their folder, with the chosen
tags after a double underscore (``<nasa_id>_t00123__awe_work_faces.mp4``), the
form the T3 scanner reads. Rejected clips are deleted. Both lose their preview
files and manifest entries; undecided clips stay for the next review. Then the
T3 scanner reindexes ``data/snippets.csv``, filling ``nasa_id`` and ``credit``
for the kept clips, and ``review.html`` is rewritten with what is left.

Every decision is checked before any file moves, so a bad export changes nothing.

    python -m broll.apply_review "$HOME\\Downloads\\review_decisions.json"
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from broll.autocut import (
    DECISIONS_NAME,
    INBOX,
    TARGET_FOLDERS,
    load_manifest,
    remove_previews,
    save_manifest,
    write_review_page,
)
from pipeline.config import Config, ConfigError, load_config
from pipeline.script_agent import MOODS
from pipeline.snippets import FACES_TAG, LOGO_TAG, csv_path, scan_library

log = logging.getLogger(__name__)

DETAILS_URL = "https://images.nasa.gov/details/"
DECISIONS = {"keep", "reject"}


class ReviewError(RuntimeError):
    """Raised, before anything changes, when the decisions cannot be applied."""


@dataclass
class Plan:
    moves: list[tuple[str, str]] = field(default_factory=list)  # inbox name -> library path
    rejects: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # decided but gone; applied before


@dataclass(frozen=True)
class Summary:
    kept: list[str]
    rejected: list[str]
    missing: list[str]
    left: int
    indexed: int


def credit_for(nasa_id: str) -> str:
    return f"NASA ({DETAILS_URL}{nasa_id})"


def tagged_name(name: str, moods: list[str], faces: bool, logo: bool) -> str:
    """``name`` with its tags after ``__``: moods sorted, then the flags."""
    path = Path(name)
    tags = sorted(set(moods)) + [FACES_TAG] * faces + [LOGO_TAG] * logo
    return path.stem + ("__" + "_".join(tags) if tags else "") + path.suffix


def load_decisions(path: Path) -> dict[str, dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ReviewError(f"no decisions file at {path}; export it from review.html") from exc
    except json.JSONDecodeError as exc:
        raise ReviewError(f"{path} is not valid JSON: {exc}") from exc
    clips = data.get("clips") if isinstance(data, dict) else None
    if not isinstance(clips, dict):
        raise ReviewError(f"{path} has no 'clips' object; is it a review.html export?")
    return clips


def make_plan(decisions: dict[str, dict], inbox: Path, library: Path, manifest: dict) -> Plan:
    """Check every decision; raise one ReviewError listing all problems."""
    plan = Plan()
    problems: list[str] = []
    targets: set[str] = set()

    for name, choice in sorted(decisions.items()):
        if not isinstance(choice, dict) or choice.get("decision") not in DECISIONS:
            problems.append(f"{name}: decision must be keep or reject")
            continue
        if name not in manifest["clips"]:
            problems.append(f"{name}: not an autocut clip in {INBOX}/")
            continue
        if not (inbox / name).is_file():
            plan.missing.append(name)
            continue
        if choice["decision"] == "reject":
            plan.rejects.append(name)
            continue

        folder = choice.get("folder")
        moods = choice.get("moods") or []
        if folder not in TARGET_FOLDERS:
            problems.append(f"{name}: unknown folder {folder!r}")
            continue
        if not isinstance(moods, list) or not all(isinstance(m, str) for m in moods):
            problems.append(f"{name}: moods must be a list of names")
            continue
        unknown = sorted(set(moods) - MOODS)
        if unknown:
            problems.append(f"{name}: unknown moods {unknown}")
            continue
        target = f"{folder}/{tagged_name(name, moods, bool(choice.get('faces')), bool(choice.get('logo')))}"
        if (library / target).exists() or target in targets:
            problems.append(f"{name}: {target} already exists")
            continue
        targets.add(target)
        plan.moves.append((name, target))

    if problems:
        raise ReviewError("nothing changed; fix these first:\n  " + "\n  ".join(problems))
    return plan


def apply_review(decisions_path: Path, cfg: Config) -> Summary:
    library = Path(cfg.library_dir)
    inbox = library / INBOX
    manifest = load_manifest(inbox)
    plan = make_plan(load_decisions(decisions_path), inbox, library, manifest)

    if plan.moves and shutil.which("ffprobe") is None:
        # The scan below needs it; without it nasa_id and credit would be lost.
        raise ReviewError("ffprobe not found on PATH; install FFmpeg. Nothing changed.")

    provenance: dict[str, tuple[str, str]] = {}
    for name, target in plan.moves:
        dest = library / target
        dest.parent.mkdir(parents=True, exist_ok=True)
        (inbox / name).replace(dest)
        nasa_id = manifest["clips"][name]["nasa_id"]
        provenance[target] = (nasa_id, credit_for(nasa_id))
        log.info("keep %s -> %s", name, target)
    for name in plan.rejects:
        (inbox / name).unlink()
        log.info("reject %s", name)
    for name in plan.missing:
        log.warning("%s is no longer in %s; skipped (already applied?)", name, INBOX)

    for name in [n for n, _ in plan.moves] + plan.rejects + plan.missing:
        remove_previews(inbox, name)
        manifest["clips"].pop(name, None)
    save_manifest(inbox, manifest)

    indexed = scan_library(cfg, provenance)
    write_review_page(library)
    left = sum(1 for name in manifest["clips"] if (inbox / name).is_file())
    return Summary(kept=[t for _, t in plan.moves], rejected=plan.rejects,
                   missing=plan.missing, left=left, indexed=indexed)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m broll.apply_review",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("decisions", type=Path, nargs="?",
                        help=f"exported JSON; default <library>/{INBOX}/{DECISIONS_NAME}")
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return 2
    path = args.decisions or Path(cfg.library_dir) / INBOX / DECISIONS_NAME
    try:
        summary = apply_review(path, cfg)
    except ReviewError as exc:
        print(exc, file=sys.stderr)
        return 1

    print(f"kept {len(summary.kept)}, rejected {len(summary.rejected)}, "
          f"{summary.left} still to review")
    print(f"{summary.indexed} clips indexed in {csv_path(cfg)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
