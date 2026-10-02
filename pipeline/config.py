"""Typed config loaded from ``.env``; no hardcoded paths anywhere else (T1a).

Exposes a frozen ``Config`` dataclass (API keys, voice id, OAuth paths, library
and output dirs, ``min_library_clips``, ``clip_cooldown_days``, optional notify
webhook) and ``load_config()``, which raises ``ConfigError`` listing every
missing variable at once.

Relative paths in ``.env`` resolve against the repo root, so ``LIBRARY_DIR=library``
means the gitignored ``library/`` beside this package.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent

# env var -> Config field, for the values that must be present.
REQUIRED_TEXT = {
    "LLM_API_KEY": "llm_api_key",
    "LLM_BASE_URL": "llm_base_url",
    "LLM_MODEL": "llm_model",
    "ELEVENLABS_API_KEY": "elevenlabs_api_key",
    "ELEVENLABS_VOICE_ID": "elevenlabs_voice_id",
}
REQUIRED_PATHS = {
    "YOUTUBE_CLIENT_SECRETS": "youtube_client_secrets",
    "YOUTUBE_TOKEN": "youtube_token",
    "LIBRARY_DIR": "library_dir",
    "MUSIC_DIR": "music_dir",
    "OUTPUT_DIR": "output_dir",
    "DATA_DIR": "data_dir",
    "FONT_PATH": "font_path",
}
OPTIONAL_INTS = {
    "MIN_LIBRARY_CLIPS": "min_library_clips",
    "CLIP_COOLDOWN_DAYS": "clip_cooldown_days",
}


class ConfigError(RuntimeError):
    """Raised by :func:`load_config` with every unusable variable listed."""


@dataclass(frozen=True)
class Config:
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    elevenlabs_api_key: str
    elevenlabs_voice_id: str
    youtube_client_secrets: Path
    youtube_token: Path
    library_dir: Path
    music_dir: Path
    output_dir: Path
    data_dir: Path
    font_path: Path
    min_library_clips: int = 200
    clip_cooldown_days: int = 3
    notify_webhook_url: str | None = None


def resolve_path(value: str) -> Path:
    path = Path(value.strip()).expanduser()
    return path if path.is_absolute() else ROOT / path


def read_env(
    env_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Raw ``.env`` values overlaid with the process environment, which wins."""
    environ = os.environ if environ is None else environ
    path = ROOT / ".env" if env_file is None else Path(env_file)

    values: dict[str, str] = {}
    if path.is_file():
        values.update({k: v for k, v in dotenv_values(path).items() if v is not None})
    values.update(environ)
    return values


def load_config(
    env_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Config:
    """Build a :class:`Config` from ``.env`` plus the process environment.

    The real environment wins over the file. Both arguments exist so tests can
    run hermetically; production calls ``load_config()`` with neither.
    """
    path = ROOT / ".env" if env_file is None else Path(env_file)
    values = read_env(env_file, environ)

    def present(key: str) -> str | None:
        value = values.get(key, "")
        return value.strip() or None

    kwargs: dict[str, object] = {}
    missing: list[str] = []
    invalid: list[str] = []

    for key, field in REQUIRED_TEXT.items():
        value = present(key)
        if value is None:
            missing.append(key)
        else:
            kwargs[field] = value

    for key, field in REQUIRED_PATHS.items():
        value = present(key)
        if value is None:
            missing.append(key)
        else:
            kwargs[field] = resolve_path(value)

    defaults = {f.name: f.default for f in fields(Config)}
    for key, field in OPTIONAL_INTS.items():
        value = present(key)
        if value is None:
            kwargs[field] = defaults[field]
            continue
        try:
            kwargs[field] = int(value)
        except ValueError:
            invalid.append(f"{key}={value!r} (want a whole number)")

    kwargs["notify_webhook_url"] = present("NOTIFY_WEBHOOK_URL")

    if missing or invalid:
        lines = [f"Cannot load config from {path}:"]
        if missing:
            lines.append("  missing or empty: " + ", ".join(sorted(missing)))
        for item in sorted(invalid):
            lines.append(f"  unusable: {item}")
        lines.append("  see .env.example for the full list")
        raise ConfigError("\n".join(lines))

    return Config(**kwargs)
