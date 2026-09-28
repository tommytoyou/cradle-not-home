"""Theme rotation over ``data/themes.json`` (T1a).

Loads themes, picks the day's theme (never-used first, then oldest
``last_used``), and records the pick with an atomic write so a crashed run
cannot corrupt the file.

``last_used`` is an ISO date string (``"2026-09-28"``) or ``null``.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path

REQUIRED_KEYS = frozenset({"id", "title_seed", "angles", "last_used"})


class ThemeError(RuntimeError):
    """Raised when the themes file is unreadable or a theme id is unknown."""


def _last_used(theme: dict) -> date | None:
    value = theme["last_used"]
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ThemeError(
            f"theme {theme['id']!r} has an unreadable last_used: {value!r}"
        ) from None


def load_themes(path: Path) -> list[dict]:
    """Read and validate the themes file, returning the themes in file order."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ThemeError(f"themes file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ThemeError(f"{path} is not valid JSON: {exc}") from exc

    if not isinstance(raw, list) or not raw:
        raise ThemeError(f"{path} must hold a non-empty JSON array")

    seen: set[str] = set()
    for index, theme in enumerate(raw):
        if not isinstance(theme, dict):
            raise ThemeError(f"{path} entry {index} is not an object")
        absent = REQUIRED_KEYS - theme.keys()
        if absent:
            raise ThemeError(f"{path} entry {index} is missing {sorted(absent)}")
        theme_id = theme["id"]
        if theme_id in seen:
            raise ThemeError(f"{path} has a duplicate theme id: {theme_id!r}")
        seen.add(theme_id)
        _last_used(theme)

    return raw


def pick_theme(themes: list[dict], today: date) -> dict:
    """Pick the day's theme: never used first, then the oldest ``last_used``.

    A theme already marked used today is returned again, so re-running a day
    does not burn a second theme. Ties fall back to file order.
    """
    if not themes:
        raise ThemeError("no themes to pick from")

    dated = [(theme, _last_used(theme)) for theme in themes]

    for theme, used in dated:
        if used == today:
            return theme

    unused = [theme for theme, used in dated if used is None]
    if unused:
        return unused[0]

    return min(dated, key=lambda pair: pair[1])[0]


def mark_used(path: Path, theme_id: str, today: date) -> None:
    """Stamp ``theme_id`` with ``today`` and rewrite the file atomically."""
    path = Path(path)
    themes = load_themes(path)

    for theme in themes:
        if theme["id"] == theme_id:
            theme["last_used"] = today.isoformat()
            break
    else:
        raise ThemeError(f"unknown theme id: {theme_id!r}")

    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(themes, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
