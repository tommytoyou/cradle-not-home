"""T0 smoke test: the scaffolding exists and imports offline."""

import csv
import importlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MODULES = [
    "config",
    "themes",
    "script_agent",
    "tts",
    "snippets",
    "assemble",
    "youtube_upload",
    "runlog",
    "run_daily",
]


def test_repo_scaffolding():
    # Every pipeline module imports and carries a docstring.
    for name in MODULES:
        module = importlib.import_module(f"pipeline.{name}")
        assert module.__doc__, f"pipeline/{name}.py needs a docstring"

    # Themes: 30 delivered by Claude AI, unique ids, none used yet.
    themes = json.loads((ROOT / "data" / "themes.json").read_text(encoding="utf-8"))
    assert len(themes) >= 30
    assert len({t["id"] for t in themes}) == len(themes)
    for theme in themes:
        assert set(theme) == {"id", "title_seed", "angles", "facts", "last_used"}
        assert theme["title_seed"]
        assert theme["angles"]
        assert theme["last_used"] is None

    # Snippet index: header only, columns as specified in T3.
    with (ROOT / "data" / "snippets.csv").open(newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert rows == [
        ["path", "folder", "moods", "duration_sec", "width", "height",
         "nasa_id", "credit", "faces", "logo_risk", "last_used"]
    ]

    # Secrets and generated data stay out of git.
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
    for pattern in [".env", "sources/", "library/", "music/", "output/",
                    "token.json", "client_secret*.json", "data/runs.jsonl"]:
        assert pattern in gitignore, f"{pattern} missing from .gitignore"
