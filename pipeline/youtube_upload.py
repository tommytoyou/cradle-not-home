"""Unlisted YouTube uploads via Data API v3 (T6).

Installed-app OAuth with the token cached at ``cfg.youtube_token``, resumable
upload retrying on 5xx. Uploads default to unlisted and anything else needs an
explicit CLI flag; every upload sets ``status.containsSyntheticMedia = true`` for
the synthetic voice and ends the description with ``#Shorts``.

The browser consent step only runs from ``python -m pipeline.youtube_upload auth``.
The unattended daily run refreshes a cached token or fails; it never blocks at
02:00 waiting for someone to click through a consent screen.

BLOCKED until Tom creates the OAuth client and the channel; tests mock the
service.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import time
from pathlib import Path

import httplib2
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from pipeline.config import Config, load_config

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
PRIVACY_STATUSES = frozenset({"unlisted", "private", "public"})
DEFAULT_PRIVACY = "unlisted"

# People & Blogs; the channel has no better-fitting category.
CATEGORY_ID = "22"
SHORTS_TAG = "#Shorts"

# YouTube counts the whole tag list against 500 characters, with a tag that
# contains a space costing two extra for its quotes, plus a comma between tags.
MAX_TAGS_CHARS = 500

CHUNK_BYTES = 8 * 1024 * 1024
RETRYABLE_STATUS = frozenset({500, 502, 503, 504})
MAX_RETRIES = 6
MAX_BACKOFF_SEC = 64

log = logging.getLogger(__name__)

# Indirection so tests can skip the waits.
_sleep = time.sleep


class UploadError(RuntimeError):
    """Raised for missing OAuth files, a bad privacy value, or a failed upload."""


# --- credentials -----------------------------------------------------------


def _save_token(creds: Credentials, cfg: Config) -> None:
    cfg.youtube_token.parent.mkdir(parents=True, exist_ok=True)
    cfg.youtube_token.write_text(creds.to_json(), encoding="utf-8")


def get_credentials(cfg: Config, *, interactive: bool = False) -> Credentials:
    """OAuth credentials for uploads, refreshing and re-caching the token.

    With ``interactive`` a missing or revoked token opens the browser consent
    flow; without it (the daily run) that case raises :class:`UploadError`.
    """
    creds = None
    if cfg.youtube_token.is_file():
        creds = Credentials.from_authorized_user_file(str(cfg.youtube_token), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            if not interactive:
                raise UploadError(
                    "YouTube token could not be refreshed; run "
                    "`python -m pipeline.youtube_upload auth`"
                ) from exc
            log.warning("cached YouTube token refused; asking for consent again")
        else:
            _save_token(creds, cfg)
            return creds

    if not interactive:
        raise UploadError(
            f"no usable YouTube token at {cfg.youtube_token}; run "
            "`python -m pipeline.youtube_upload auth`"
        )
    if not cfg.youtube_client_secrets.is_file():
        raise UploadError(f"OAuth client file missing: {cfg.youtube_client_secrets}")

    flow = InstalledAppFlow.from_client_secrets_file(str(cfg.youtube_client_secrets), SCOPES)
    creds = flow.run_local_server(port=0)
    _save_token(creds, cfg)
    return creds


def build_service(cfg: Config):
    return build("youtube", "v3", credentials=get_credentials(cfg), cache_discovery=False)


# --- request body ----------------------------------------------------------


def shorts_description(description: str) -> str:
    """The description with ``#Shorts`` as its last word, added once."""
    text = description.rstrip()
    if text.split()[-1:] == [SHORTS_TAG]:
        return text
    return f"{text}\n\n{SHORTS_TAG}" if text else SHORTS_TAG


def fit_tags(tags: list[str]) -> list[str]:
    """Drop blank and duplicate tags, then any that overflow the 500 char budget."""
    kept, seen, used = [], set(), 0
    for tag in tags:
        tag = " ".join(tag.replace("<", "").replace(">", "").split())
        if not tag or tag.lower() in seen:
            continue
        cost = len(tag) + (2 if " " in tag else 0) + (1 if kept else 0)
        if used + cost > MAX_TAGS_CHARS:
            log.warning("tag %r dropped; over the %d character budget", tag, MAX_TAGS_CHARS)
            continue
        kept.append(tag)
        seen.add(tag.lower())
        used += cost
    return kept


def request_body(video: dict, privacy: str) -> dict:
    return {
        "snippet": {
            "title": video["title"],
            "description": shorts_description(video["description"]),
            "tags": fit_tags(video.get("tags", [])),
            "categoryId": CATEGORY_ID,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True,
        },
    }


# --- upload ----------------------------------------------------------------


def _backoff(attempt: int) -> float:
    return min(2 ** attempt, MAX_BACKOFF_SEC) + random.uniform(0, 1)


def _resumable(request) -> dict:
    """Drive ``next_chunk`` to completion, retrying 5xx and dropped connections.

    The retry count resets whenever a chunk gets through, so one slow stretch
    does not use up the budget for the rest of the file.
    """
    failures = 0
    response = None
    while response is None:
        try:
            status, response = request.next_chunk()
        except HttpError as exc:
            if exc.resp.status not in RETRYABLE_STATUS:
                raise UploadError(f"YouTube refused the upload: HTTP {exc.resp.status}") from exc
            reason = f"HTTP {exc.resp.status}"
        except (httplib2.HttpLib2Error, OSError) as exc:
            reason = f"{type(exc).__name__}: {exc}"
        else:
            failures = 0
            if status is not None:
                log.info("uploaded %d%%", int(status.progress() * 100))
            continue

        failures += 1
        if failures > MAX_RETRIES:
            raise UploadError(f"upload failed after {MAX_RETRIES} retries ({reason})")
        wait = _backoff(failures)
        log.warning("upload chunk failed (%s); retry %d in %.1fs", reason, failures, wait)
        _sleep(wait)
    return response


def upload(video_path: Path, video: dict, cfg: Config, privacy: str = DEFAULT_PRIVACY) -> str:
    """Upload one rendered video and return its YouTube id."""
    if privacy not in PRIVACY_STATUSES:
        raise UploadError(f"privacy must be one of {sorted(PRIVACY_STATUSES)}, not {privacy!r}")
    video_path = Path(video_path)
    if not video_path.is_file():
        raise UploadError(f"video file missing: {video_path}")

    service = build_service(cfg)
    media = MediaFileUpload(
        str(video_path), mimetype="video/mp4", chunksize=CHUNK_BYTES, resumable=True
    )
    request = service.videos().insert(
        part="snippet,status", body=request_body(video, privacy), media_body=media
    )
    response = _resumable(request)

    youtube_id = response.get("id")
    if not youtube_id:
        raise UploadError(f"YouTube returned no video id: {response}")
    log.info("uploaded %s as %s (%s)", video_path.name, youtube_id, privacy)
    return youtube_id


# --- CLI -------------------------------------------------------------------


def _video_from_package(package_path: Path, video_format: str) -> dict:
    package = json.loads(Path(package_path).read_text(encoding="utf-8"))
    for video in package.get("videos", []):
        if video.get("format") == video_format:
            return video
    raise UploadError(f"no {video_format!r} video in {package_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.youtube_upload")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("auth", help="run the browser consent flow and cache the token")

    send = commands.add_parser("upload", help="upload one rendered video")
    send.add_argument("video", type=Path)
    send.add_argument("package", type=Path, help="daily package JSON")
    send.add_argument("--format", required=True, choices=["main", "punch"])
    send.add_argument(
        "--privacy", default=DEFAULT_PRIVACY, choices=sorted(PRIVACY_STATUSES),
        help="defaults to unlisted; anything else must be asked for here",
    )

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config()

    if args.command == "auth":
        get_credentials(cfg, interactive=True)
        print(f"token cached at {cfg.youtube_token}")
        return 0

    video = _video_from_package(args.package, args.format)
    print(upload(args.video, video, cfg, privacy=args.privacy))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
