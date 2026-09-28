"""T1a: theme rotation picks in the specified order and writes atomically."""

import json
from datetime import date
from pathlib import Path

import pytest

from pipeline.themes import ThemeError, load_themes, mark_used, pick_theme

TODAY = date(2026, 9, 28)


def stub(theme_id: str, last_used: str | None = None) -> dict:
    return {"id": theme_id, "title_seed": f"seed {theme_id}", "angles": [], "last_used": last_used}


def write(tmp_path: Path, themes: list[dict]) -> Path:
    path = tmp_path / "themes.json"
    path.write_text(json.dumps(themes, indent=2), encoding="utf-8")
    return path


def test_load_returns_themes_in_file_order(tmp_path):
    path = write(tmp_path, [stub("b"), stub("a")])

    assert [t["id"] for t in load_themes(path)] == ["b", "a"]


def test_load_rejects_missing_file(tmp_path):
    with pytest.raises(ThemeError, match="not found"):
        load_themes(tmp_path / "nope.json")


def test_load_rejects_bad_json(tmp_path):
    path = tmp_path / "themes.json"
    path.write_text("{oops", encoding="utf-8")

    with pytest.raises(ThemeError, match="not valid JSON"):
        load_themes(path)


def test_load_rejects_empty_array(tmp_path):
    with pytest.raises(ThemeError, match="non-empty"):
        load_themes(write(tmp_path, []))


def test_load_rejects_missing_keys(tmp_path):
    path = write(tmp_path, [{"id": "a", "title_seed": "seed a"}])

    with pytest.raises(ThemeError, match="missing"):
        load_themes(path)


def test_load_rejects_duplicate_ids(tmp_path):
    with pytest.raises(ThemeError, match="duplicate"):
        load_themes(write(tmp_path, [stub("a"), stub("a")]))


def test_load_rejects_unreadable_last_used(tmp_path):
    with pytest.raises(ThemeError, match="last_used"):
        load_themes(write(tmp_path, [stub("a", "not-a-date")]))


def test_pick_prefers_never_used_in_file_order():
    themes = [stub("a", "2026-09-01"), stub("b"), stub("c")]

    assert pick_theme(themes, TODAY)["id"] == "b"


def test_pick_falls_back_to_oldest_last_used():
    themes = [stub("a", "2026-09-20"), stub("b", "2026-09-05"), stub("c", "2026-09-11")]

    assert pick_theme(themes, TODAY)["id"] == "b"


def test_pick_breaks_ties_by_file_order():
    themes = [stub("a", "2026-09-05"), stub("b", "2026-09-05")]

    assert pick_theme(themes, TODAY)["id"] == "a"


def test_pick_is_idempotent_within_a_day():
    themes = [stub("a", TODAY.isoformat()), stub("b")]

    assert pick_theme(themes, TODAY)["id"] == "a"


def test_pick_rejects_empty_list():
    with pytest.raises(ThemeError, match="no themes"):
        pick_theme([], TODAY)


def test_mark_used_persists_and_leaves_others_alone(tmp_path):
    path = write(tmp_path, [stub("a"), stub("b", "2026-09-01")])

    mark_used(path, "a", TODAY)

    themes = load_themes(path)
    assert themes[0]["last_used"] == "2026-09-28"
    assert themes[1]["last_used"] == "2026-09-01"
    assert [t["id"] for t in themes] == ["a", "b"]
    assert themes[0]["title_seed"] == "seed a"


def test_mark_used_then_pick_advances_the_rotation(tmp_path):
    path = write(tmp_path, [stub("a"), stub("b")])

    mark_used(path, "a", TODAY)

    tomorrow = date(2026, 9, 29)
    assert pick_theme(load_themes(path), tomorrow)["id"] == "b"


def test_mark_used_rejects_unknown_id(tmp_path):
    path = write(tmp_path, [stub("a")])

    with pytest.raises(ThemeError, match="unknown theme id"):
        mark_used(path, "zzz", TODAY)


def test_mark_used_leaves_no_temp_files(tmp_path):
    path = write(tmp_path, [stub("a")])

    mark_used(path, "a", TODAY)

    assert [p.name for p in tmp_path.iterdir()] == ["themes.json"]
