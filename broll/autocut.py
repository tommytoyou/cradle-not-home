"""Cut NASA source videos into review candidates (T8a).

For each video in ``broll/sources/``: detect scene cuts with PySceneDetect,
split every scene into 4-12 s clips, drop clips that are under 1080 px tall or
near black, and write the rest to ``<library>/_inbox/`` as muted H.264 named
``<nasa_id>_t<start in tenths of a second>.mp4``. Near-static clips are kept
but marked ``static`` in the manifest and listed in their own "Flagged static"
section of the page. Then write ``_inbox/review.html`` (thumbnail, looping
preview, keep/reject, folder, tags) for ``broll/apply_review.py``.

``_inbox/candidates.json`` remembers the exact ``nasa_id`` and source of every
clip, and which sources are done, so a rerun only cuts new sources. Thumbnails
and previews live in ``_inbox/_review/``. The scanner skips ``_inbox``, so
nothing here is pickable until it is reviewed.

    python -m broll.autocut              # new sources only
    python -m broll.autocut --force      # recut everything in broll/sources
    python -m broll.autocut --page-only  # just rewrite review.html
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from broll.collect_nasa import FOLDERS, SOURCES
from pipeline.config import ConfigError, load_config
from pipeline.script_agent import MOODS
from pipeline.snippets import (
    FACES_TAG,
    LOGO_TAG,
    MAX_DURATION_SEC,
    MIN_DURATION_SEC,
    MIN_HEIGHT,
    VIDEO_SUFFIXES,
    SnippetError,
    folder_moods,
    probe_clip,
)

log = logging.getLogger(__name__)

INBOX = "_inbox"
REVIEW_DIR = "_review"
MANIFEST_NAME = "candidates.json"
PAGE_NAME = "review.html"
DECISIONS_NAME = "review_decisions.json"
TEMPLATE_PATH = Path(__file__).with_name("review_template.html")

# Folders a clip can be kept into; the inbox itself is not a destination.
TARGET_FOLDERS = [f for f in FOLDERS if f != INBOX]

# Re-encoding lands on frame boundaries, so aim inside the scanner's 4-12 s.
CLIP_MIN_SEC = MIN_DURATION_SEC + 0.25
CLIP_MAX_SEC = MAX_DURATION_SEC - 0.25
# Dropped from both ends of a scene so no clip opens on a dissolve frame.
EDGE_TRIM_SEC = 0.2

# Frame checks run on grey frames shrunk to this size.
SAMPLE_SIZE = (160, 90)
SAMPLES_PER_CLIP = 8
# Near black: the brightest 1% of pixels stays below this (0-255) in most
# samples. A planet or a lit spacecraft on black passes; an empty frame doesn't.
BLACK_P99 = 24
# Near static: mean absolute change between samples, 0-255.
STATIC_DIFF = 1.0

# Same encode as cut_snippet.sh.
CLIP_ENCODE = ["-c:v", "libx264", "-preset", "medium", "-crf", "16",
               "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart"]
PREVIEW_HEIGHT = 270
PREVIEW_SEC = 3.0


class AutocutError(RuntimeError):
    """Raised when a source or the inbox cannot be processed."""


@dataclass(frozen=True)
class Clip:
    name: str
    nasa_id: str
    source: str
    start: float
    end: float
    folder: str  # suggested target, from the collector's file name; may be ""
    static: bool = False  # near static: kept, but shown apart on the page


# --- names -----------------------------------------------------------------


def parse_source(path: Path) -> tuple[str, str]:
    """``(folder, nasa_id)`` from a collector file name like ``05_eva_<id>.mp4``."""
    stem = path.stem
    for folder in sorted(FOLDERS, key=len, reverse=True):
        if stem.startswith(folder + "_") and len(stem) > len(folder) + 1:
            return ("" if folder == INBOX else folder), stem[len(folder) + 1:]
    return "", stem


def safe_id(nasa_id: str) -> str:
    """``nasa_id`` fit for a file name; never holds the ``__`` tag separator."""
    cleaned = re.sub(r"[^\w.-]+", "_", nasa_id)
    return re.sub(r"_{2,}", "_", cleaned).strip("_.") or "clip"


def clip_name(nasa_id: str, start: float) -> str:
    return f"{safe_id(nasa_id)}_t{round(start * 10):05d}.mp4"


# --- scenes ----------------------------------------------------------------


def detect_scenes(path: Path) -> list[tuple[float, float]]:
    """Scene spans in seconds; empty when PySceneDetect finds no cut."""
    from scenedetect import ContentDetector, detect

    scenes = detect(str(path), ContentDetector(), show_progress=False)
    return [(start.seconds, end.seconds) for start, end in scenes]


def split_scene(start: float, end: float) -> list[tuple[float, float]]:
    """Equal pieces of ``CLIP_MIN_SEC``-``CLIP_MAX_SEC``; none if the scene is short."""
    start, end = start + EDGE_TRIM_SEC, end - EDGE_TRIM_SEC
    length = end - start
    if length < CLIP_MIN_SEC:
        return []
    pieces = max(1, math.ceil(length / CLIP_MAX_SEC - 1e-9))
    step = length / pieces
    return [(start + i * step, start + (i + 1) * step) for i in range(pieces)]


# --- frame checks ----------------------------------------------------------

NEAR_BLACK = "near black"
NEAR_STATIC = "near static"


def _grey_samples(capture: cv2.VideoCapture, start: float, end: float) -> list[np.ndarray]:
    frames = []
    for i in range(SAMPLES_PER_CLIP):
        at = start + (end - start) * (i + 0.5) / SAMPLES_PER_CLIP
        capture.set(cv2.CAP_PROP_POS_MSEC, at * 1000)
        ok, frame = capture.read()
        if not ok:
            continue
        grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        frames.append(cv2.resize(grey, SAMPLE_SIZE, interpolation=cv2.INTER_AREA).astype(np.float32))
    return frames


def reject_reason(frames: list[np.ndarray]) -> str | None:
    """Why these sampled frames make a bad clip, or None if they are fine."""
    if len(frames) < 2:
        return "unreadable"
    brightest = [float(np.percentile(frame, 99)) for frame in frames]
    if float(np.median(brightest)) < BLACK_P99:
        return NEAR_BLACK
    motion = float(np.mean([np.mean(np.abs(b - a)) for a, b in zip(frames, frames[1:])]))
    if motion < STATIC_DIFF:
        return NEAR_STATIC
    return None


# --- ffmpeg ----------------------------------------------------------------


def _ffmpeg(args: list[str]) -> None:
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise AutocutError("ffmpeg not found on PATH; install FFmpeg")
    result = subprocess.run([binary, "-y", "-v", "error", *args], capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-3:]
        raise AutocutError("ffmpeg failed: " + " / ".join(tail))


def _atomic_output(dest: Path, write) -> None:
    """Run ``write(tmp)`` and move the result into place only if it worked."""
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=dest.stem + ".", suffix=dest.suffix)
    os.close(fd)
    try:
        write(Path(tmp))
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def cut_clip(source: Path, start: float, end: float, dest: Path) -> None:
    args = ["-ss", f"{start:.3f}", "-i", str(source), "-t", f"{end - start:.3f}",
            "-map", "0:v:0", *CLIP_ENCODE]
    _atomic_output(dest, lambda tmp: _ffmpeg([*args, "-f", "mp4", str(tmp)]))


def make_previews(clip: Path, review_dir: Path) -> None:
    """A thumbnail and a short, small, looping preview from the middle of the clip."""
    duration, _, _ = probe_clip(clip)
    middle = duration / 2
    scale = f"scale=-2:{PREVIEW_HEIGHT}"
    thumb = review_dir / (clip.stem + ".jpg")
    preview = review_dir / (clip.stem + ".mp4")
    _atomic_output(thumb, lambda tmp: _ffmpeg(
        ["-ss", f"{middle:.3f}", "-i", str(clip), "-frames:v", "1", "-vf", scale,
         "-q:v", "4", "-update", "1", "-f", "image2", str(tmp)]))
    _atomic_output(preview, lambda tmp: _ffmpeg(
        ["-ss", f"{max(0.0, middle - PREVIEW_SEC / 2):.3f}", "-i", str(clip),
         "-t", f"{PREVIEW_SEC}", "-vf", scale, "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "30", "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart",
         "-f", "mp4", str(tmp)]))


def remove_previews(inbox: Path, name: str) -> None:
    stem = Path(name).stem
    for suffix in (".jpg", ".mp4"):
        (inbox / REVIEW_DIR / (stem + suffix)).unlink(missing_ok=True)


# --- manifest --------------------------------------------------------------


def manifest_path(inbox: Path) -> Path:
    return inbox / MANIFEST_NAME


def load_manifest(inbox: Path) -> dict:
    path = manifest_path(inbox)
    if not path.is_file():
        return {"sources": {}, "clips": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AutocutError(f"{path} is not valid JSON: {exc}") from exc
    data.setdefault("sources", {})
    data.setdefault("clips", {})
    return data


def save_manifest(inbox: Path, manifest: dict) -> None:
    inbox.mkdir(parents=True, exist_ok=True)
    text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    _atomic_output(manifest_path(inbox), lambda tmp: tmp.write_text(text, encoding="utf-8"))


def pending_clips(inbox: Path, manifest: dict) -> list[Clip]:
    """Clips listed in the manifest that are still in the inbox, in name order."""
    clips = []
    for name, entry in sorted(manifest["clips"].items()):
        if (inbox / name).is_file():
            clips.append(Clip(name=name, **entry))
    return clips


# --- cutting ---------------------------------------------------------------


def cut_source(source: Path, inbox: Path) -> tuple[list[Clip], dict[str, int]]:
    """Write every usable clip of ``source`` into ``inbox``; returns them and drop counts.

    Near-static clips are written too, flagged ``static``; every other reject is
    dropped.
    """
    folder, nasa_id = parse_source(source)
    dropped: dict[str, int] = {}

    def drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    try:
        duration, _, height = probe_clip(source)
    except SnippetError as exc:
        raise AutocutError(f"{source.name}: {exc}") from exc
    if height < MIN_HEIGHT:
        log.warning("skip %s: %d px tall, need at least %d", source.name, height, MIN_HEIGHT)
        return [], {f"under {MIN_HEIGHT} px": 1}

    scenes = detect_scenes(source) or [(0.0, duration)]
    spans = []
    for start, end in scenes:
        pieces = split_scene(start, end)
        if not pieces:
            drop("scene too short")
        spans.extend(pieces)

    (inbox / REVIEW_DIR).mkdir(parents=True, exist_ok=True)
    clips: list[Clip] = []
    capture = cv2.VideoCapture(str(source))
    try:
        for start, end in spans:
            reason = reject_reason(_grey_samples(capture, start, end))
            static = reason == NEAR_STATIC
            if static:
                log.info("flag %s %.1f-%.1f s: %s", source.name, start, end, reason)
            elif reason:
                log.info("drop %s %.1f-%.1f s: %s", source.name, start, end, reason)
                drop(reason)
                continue
            clip = Clip(clip_name(nasa_id, start), nasa_id, source.name,
                        round(start, 3), round(end, 3), folder, static)
            dest = inbox / clip.name
            cut_clip(source, start, end, dest)
            make_previews(dest, inbox / REVIEW_DIR)
            clips.append(clip)
    finally:
        capture.release()
    return clips, dropped


def sources_in(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir()
                  if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES)


def autocut(sources_dir: Path, library: Path, force: bool = False) -> list[Clip]:
    """Cut every new source into ``library/_inbox``, rewrite the page; returns new clips."""
    inbox = library / INBOX
    inbox.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(inbox)
    made: list[Clip] = []

    for source in sources_in(sources_dir):
        if source.name in manifest["sources"] and not force:
            log.info("skip %s: already cut (use --force to recut)", source.name)
            continue
        log.info("cutting %s", source.name)
        try:
            clips, dropped = cut_source(source, inbox)
        except AutocutError as exc:
            log.error("%s", exc)
            continue
        for clip in clips:
            entry = {k: v for k, v in clip.__dict__.items() if k != "name"}
            manifest["clips"][clip.name] = entry
        static = sum(clip.static for clip in clips)
        manifest["sources"][source.name] = {"nasa_id": parse_source(source)[1],
                                            "clips": len(clips), "static": static,
                                            "dropped": dropped}
        # Saved per source so a crash halfway keeps the finished ones.
        save_manifest(inbox, manifest)
        log.info("%s: %d clips (%d flagged static), dropped %s",
                 source.name, len(clips), static, dropped or "none")
        made.extend(clips)

    write_review_page(library)
    return made


# --- review page -----------------------------------------------------------


def write_review_page(library: Path) -> Path:
    """Write ``_inbox/review.html`` listing every clip still waiting for review."""
    inbox = library / INBOX
    inbox.mkdir(parents=True, exist_ok=True)
    clips = pending_clips(inbox, load_manifest(inbox))
    defaults = folder_moods()
    data = {
        "decisionsName": DECISIONS_NAME,
        "reviewDir": REVIEW_DIR,
        "folders": TARGET_FOLDERS,
        "folderMoods": {f: sorted(defaults.get(f, ())) for f in TARGET_FOLDERS},
        "moods": sorted(MOODS),
        "flags": [FACES_TAG, LOGO_TAG],
        "clips": [
            {"name": c.name, "stem": Path(c.name).stem, "nasa_id": c.nasa_id,
             "source": c.source, "start": c.start, "end": c.end, "folder": c.folder,
             "static": c.static}
            for c in clips
        ],
    }
    # "</" would end the <script> early.
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    page = TEMPLATE_PATH.read_text(encoding="utf-8").replace("/*__DATA__*/null", payload)
    path = inbox / PAGE_NAME
    _atomic_output(path, lambda tmp: tmp.write_text(page, encoding="utf-8"))
    return path


# --- CLI -------------------------------------------------------------------


def library_dir() -> Path:
    try:
        return Path(load_config().library_dir)
    except ConfigError as exc:
        raise SystemExit(f"config: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m broll.autocut", description=__doc__.split("\n\n")[0])
    parser.add_argument("--sources", type=Path, default=SOURCES, help="default: broll/sources")
    parser.add_argument("--library", type=Path, help="default: LIBRARY_DIR from .env")
    parser.add_argument("--force", action="store_true", help="recut sources already done")
    parser.add_argument("--page-only", action="store_true", help="only rewrite review.html")
    args = parser.parse_args(argv)

    library = args.library or library_dir()
    if args.page_only:
        page = write_review_page(library)
    else:
        made = autocut(args.sources, library, force=args.force)
        print(f"{len(made)} new clips in {library / INBOX}")
        page = library / INBOX / PAGE_NAME
    print(f"review: {page}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
