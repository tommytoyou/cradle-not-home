"""Daily entry point wiring the whole pipeline (T7).

Order: library check, theme pick, package, then per video (main, punch) TTS,
timeline, render, upload; afterwards mark theme and clips used, log and notify.
If either video fails before upload, neither is uploaded and nothing is marked
used. ``--dry-run`` does everything except upload, and instead writes
``upload_sheet.txt`` with what a manual upload needs. A format that
``data/runs.jsonl`` already shows uploaded that day is skipped, so re-running a
day never uploads a video twice. Output lands in
``output/YYYY-MM-DD/{main,punch}.mp4``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from pipeline import snippets, themes
from pipeline.assemble import build_timeline, render
from pipeline.config import Config, ConfigError, load_config
from pipeline.runlog import (
    ALREADY_UPLOADED,
    DRY_RUN,
    FAILED,
    UPLOADED,
    RunRecord,
    append_run,
    notify,
    uploaded_ids,
)
from pipeline.script_agent import build_package
from pipeline.tts import concat_voice, synth_blocks
from pipeline.youtube_upload import DEFAULT_PRIVACY, fit_tags, shorts_description, upload

FORMATS = ("main", "punch")
THEMES_NAME = "themes.json"
UPLOAD_SHEET_NAME = "upload_sheet.txt"
SYNTHETIC_REMINDER = 'In YouTube Studio, tick "Altered or synthetic content: Yes".'

log = logging.getLogger(__name__)


class RunError(RuntimeError):
    """Raised for a stop the pipeline makes on purpose, such as a thin library."""


@dataclass
class Built:
    """One rendered video waiting for upload."""

    format: str
    video: dict
    path: Path
    duration_sec: float
    snippet_count: int


def day_dir(cfg: Config, today: date) -> Path:
    return cfg.output_dir / today.isoformat()


def _video(pkg: dict, video_format: str) -> dict:
    for video in pkg["videos"]:
        if video["format"] == video_format:
            return video
    raise RunError(f"package {pkg['id']} has no {video_format} video")


def _build(video: dict, cfg: Config, today: date, used_today: set[str]) -> Built:
    """TTS, timeline and render for one video."""
    video_format = video["format"]
    out_dir = day_dir(cfg, today)
    work = out_dir / "work" / video_format

    blocks = synth_blocks(video, cfg, work)
    voice = work / "voice.wav"
    concat_voice(blocks, voice)
    shots, captions = build_timeline(video, blocks, cfg, today, used_today)
    path = render(shots, captions, voice, video["hook_text"], cfg, out_dir / f"{video_format}.mp4")

    return Built(
        format=video_format,
        video=video,
        path=path,
        duration_sec=round(sum(shot.duration for shot in shots), 2),
        snippet_count=len(shots),
    )


def upload_sheet(built: list[Built]) -> str:
    """Everything needed to upload the day's videos by hand.

    The description and tags go through the same helpers the API upload uses,
    so the sheet matches what an automated upload would have sent.
    """
    rule = "=" * 60
    sections = []
    for item in built:
        video = item.video
        sections.append("\n".join([
            rule,
            f"{item.format.upper()}  ({item.path.name}, {item.duration_sec:.1f}s)",
            rule,
            "",
            "TITLE",
            video["title"],
            "",
            "DESCRIPTION",
            shorts_description(video["description"]),
            "",
            "TAGS",
            ", ".join(fit_tags(video.get("tags", []))),
            "",
            "VISIBILITY",
            DEFAULT_PRIVACY,
            "",
            "REMINDER",
            SYNTHETIC_REMINDER,
            "",
        ]))
    return "\n".join(sections)


def _failed(today: date, package_id: str | None, notes: str) -> list[RunRecord]:
    return [
        RunRecord(
            date=today.isoformat(), package_id=package_id, format=video_format,
            youtube_id=None, visibility=None, duration_sec=None, snippet_count=None,
            status=FAILED, notes=notes,
        )
        for video_format in FORMATS
    ]


def _finish(records: list[RunRecord], cfg: Config) -> list[RunRecord]:
    for record in records:
        append_run(record, cfg)
    notify(records, cfg)
    return records


def run(today: date, cfg: Config, dry_run: bool = False) -> list[RunRecord]:
    """Make, upload and record the day's two videos; one record per video."""
    stamp = today.isoformat()
    themes_path = cfg.data_dir / THEMES_NAME
    package_id = None
    stage = "library check"

    # Everything up to the end of rendering is all-or-nothing.
    try:
        if not snippets.library_ok(cfg):
            raise RunError(f"library has fewer than {cfg.min_library_clips} clips")

        stage = "theme"
        theme = themes.pick_theme(themes.load_themes(themes_path), today)

        stage = "package"
        pkg = build_package(theme, cfg, today)
        package_id = pkg["id"]
        out_dir = day_dir(cfg, today)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "package.json").write_text(
            json.dumps(pkg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        used_today: set[str] = set()
        built = []
        for video_format in FORMATS:
            stage = video_format
            built.append(_build(_video(pkg, video_format), cfg, today, used_today))
    except Exception as exc:
        log.exception("run stopped at %s; nothing uploaded, nothing marked used", stage)
        return _finish(_failed(today, package_id, f"{stage}: {exc}"), cfg)

    records = []
    if dry_run:
        sheet = day_dir(cfg, today) / UPLOAD_SHEET_NAME
        sheet.write_text(upload_sheet(built), encoding="utf-8")
        log.info("dry run: upload sheet at %s", sheet)
        for item in built:
            records.append(RunRecord(
                date=stamp, package_id=package_id, format=item.format,
                youtube_id=None, visibility=None, duration_sec=item.duration_sec,
                snippet_count=item.snippet_count, status=DRY_RUN,
                notes=f"not uploaded; see {UPLOAD_SHEET_NAME}",
            ))
    else:
        # A re-run of a day must never put the same video up twice.
        already = uploaded_ids(stamp, cfg)
        for item in built:
            if item.format in already:
                log.warning(
                    "%s already uploaded on %s as %s; skipping upload",
                    item.format, stamp, already[item.format],
                )
                records.append(RunRecord(
                    date=stamp, package_id=package_id, format=item.format,
                    youtube_id=already[item.format], visibility=None,
                    duration_sec=item.duration_sec, snippet_count=item.snippet_count,
                    status=ALREADY_UPLOADED, notes="upload skipped; already in runs.jsonl",
                ))
                continue
            try:
                youtube_id = upload(item.path, item.video, cfg, privacy=DEFAULT_PRIVACY)
            except Exception as exc:
                log.exception("%s upload failed", item.format)
                records.append(RunRecord(
                    date=stamp, package_id=package_id, format=item.format,
                    youtube_id=None, visibility=None, duration_sec=item.duration_sec,
                    snippet_count=item.snippet_count, status=FAILED,
                    notes=f"upload: {exc}",
                ))
                continue
            records.append(RunRecord(
                date=stamp, package_id=package_id, format=item.format,
                youtube_id=youtube_id, visibility=DEFAULT_PRIVACY,
                duration_sec=item.duration_sec, snippet_count=item.snippet_count,
                status=UPLOADED, notes="",
            ))

    # A day where nothing went out stays unmarked, so a re-run gets the same
    # theme and clips. Once anything is public the material counts as used.
    if any(record.status in (UPLOADED, ALREADY_UPLOADED, DRY_RUN) for record in records):
        try:
            themes.mark_used(themes_path, theme["id"], today)
            snippets.mark_used(sorted(used_today), today, cfg)
        except Exception as exc:
            log.exception("could not mark theme and clips used")
            for record in records:
                record.notes = "; ".join(filter(None, [record.notes, f"mark used: {exc}"]))

    return _finish(records, cfg)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.run_daily")
    parser.add_argument("--dry-run", action="store_true", help="do everything except upload")
    parser.add_argument(
        "--date", type=date.fromisoformat, default=None,
        help="run as if today were YYYY-MM-DD (default: today)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    records = run(args.date or date.today(), cfg, dry_run=args.dry_run)
    return 1 if any(record.status == FAILED for record in records) else 0


if __name__ == "__main__":
    raise SystemExit(main())
