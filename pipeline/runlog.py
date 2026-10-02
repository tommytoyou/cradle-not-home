"""Run records and notification (T7).

Appends one JSON line per video to ``data/runs.jsonl`` (gitignored) with date,
package id, format, youtube id, visibility, duration, snippet count, status and
notes, and reports the day's records to stdout plus an optional webhook.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import requests

from pipeline.config import Config

RUNS_NAME = "runs.jsonl"
WEBHOOK_TIMEOUT_SEC = 15

UPLOADED = "uploaded"
DRY_RUN = "dry_run"
FAILED = "failed"
# Not uploaded again because an earlier run that day already did it.
ALREADY_UPLOADED = "already_uploaded"

log = logging.getLogger(__name__)


@dataclass
class RunRecord:
    date: str
    package_id: str | None
    format: str
    youtube_id: str | None
    visibility: str | None
    duration_sec: float | None
    snippet_count: int | None
    status: str
    notes: str


def runs_path(cfg: Config) -> Path:
    return cfg.data_dir / RUNS_NAME


def append_run(record: RunRecord, cfg: Config) -> None:
    """One JSON object per line, appended so earlier days are never rewritten."""
    path = runs_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")


def uploaded_ids(day: str, cfg: Config) -> dict[str, str]:
    """``{format: youtube_id}`` for every video already uploaded on ``day``.

    Unreadable lines are skipped with a warning rather than stopping the run;
    the guard only ever prevents uploads, never causes one.
    """
    path = runs_path(cfg)
    if not path.is_file():
        return {}
    found: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            log.warning("%s line %d is not JSON; ignored", path.name, number)
            continue
        if isinstance(entry, dict) and entry.get("date") == day and entry.get("youtube_id"):
            found.setdefault(entry.get("format"), entry["youtube_id"])
    return found


def summary(records: list[RunRecord]) -> str:
    lines = []
    for record in records:
        line = f"{record.date} {record.format}: {record.status}"
        if record.youtube_id:
            line += f" https://youtu.be/{record.youtube_id} ({record.visibility})"
        if record.duration_sec is not None:
            line += f", {record.duration_sec:.1f}s, {record.snippet_count} clips"
        if record.notes:
            line += f" | {record.notes}"
        lines.append(line)
    return "\n".join(lines)


def notify(records: list[RunRecord], cfg: Config) -> None:
    """Print and log the day; POST it to the webhook when one is configured.

    A webhook failure is logged and swallowed: the videos are already out, and
    a broken notifier must not turn a good run into a failed one.
    """
    text = summary(records)
    print(text)
    failed = any(record.status == FAILED for record in records)
    log.log(logging.ERROR if failed else logging.INFO, "run finished:\n%s", text)

    if not cfg.notify_webhook_url:
        return
    payload = {"text": text, "records": [asdict(record) for record in records]}
    try:
        response = requests.post(cfg.notify_webhook_url, json=payload, timeout=WEBHOOK_TIMEOUT_SEC)
        response.raise_for_status()
    except requests.RequestException as exc:
        log.warning("notify webhook failed: %s", exc)
