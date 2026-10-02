"""T3: library scanning, the CSV index, and the picker.

ffprobe is mocked for the logic tests so the suite runs without FFmpeg; the
tests at the bottom cut real fixture clips and skip when FFmpeg is absent.
"""

import csv
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from pipeline import snippets
from pipeline.config import Config
from pipeline.snippets import (
    FIELDNAMES,
    Snippet,
    SnippetError,
    csv_path,
    folder_moods,
    library_ok,
    load_snippets,
    mark_used,
    parse_tags,
    pick,
    scan_library,
)

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="FFmpeg not on PATH")

TODAY = date(2026, 9, 28)


def make_cfg(tmp_path: Path, **overrides) -> Config:
    base = dict(
        llm_api_key="k",
        llm_base_url="https://example.invalid",
        llm_model="claude-opus-5",
        elevenlabs_api_key="k",
        elevenlabs_voice_id="voice",
        youtube_client_secrets=tmp_path / "client_secret.json",
        youtube_token=tmp_path / "token.json",
        library_dir=tmp_path / "library",
        music_dir=tmp_path / "music",
        output_dir=tmp_path / "output",
        data_dir=tmp_path / "data",
        font_path=tmp_path / "font.ttf",
    )
    return Config(**(base | overrides))


def add_clip(cfg: Config, folder: str, name: str) -> Path:
    path = Path(cfg.library_dir) / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not really a video")
    return path


@pytest.fixture
def good_probe(monkeypatch):
    """Every clip probes as a valid 1920x1080, 6 s file."""
    monkeypatch.setattr(snippets, "probe_clip", lambda path: (6.0, 1920, 1080))


def write_index(cfg: Config, rows: list[dict]) -> None:
    path = csv_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in FIELDNAMES})


def row(path, moods="", *, faces="0", logo="0", last_used="", folder="01_launch"):
    return {
        "path": path, "folder": folder, "moods": moods,
        "duration_sec": "6.000", "width": "1920", "height": "1080",
        "nasa_id": "", "credit": "", "faces": faces, "logo_risk": logo,
        "last_used": last_used,
    }


def read_index(cfg: Config) -> list[dict]:
    with csv_path(cfg).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


# --- tags and folder defaults ---------------------------------------------


def test_parse_tags_without_a_double_underscore():
    assert parse_tags("launch_sls_01") == (frozenset(), False, False)


def test_parse_tags_reads_moods_and_flags():
    moods, faces, logo = parse_tags("launch_sls_01__pressure_ignition_faces")

    assert moods == frozenset({"pressure", "ignition"})
    assert faces is True
    assert logo is False


def test_parse_tags_reads_the_logo_flag():
    moods, faces, logo = parse_tags("pad_02__standards_logo")

    assert moods == frozenset({"standards"})
    assert (faces, logo) == (False, True)


def test_parse_tags_ignores_unknown_tags(caplog):
    with caplog.at_level("WARNING"):
        moods, _, _ = parse_tags("eva_03__work_sparkly")

    assert moods == frozenset({"work"})
    assert "sparkly" in caplog.text


def test_flags_are_not_moods():
    moods, _, _ = parse_tags("x__faces_logo")

    assert moods == frozenset()


def test_folder_moods_come_from_queries_yaml():
    defaults = folder_moods()

    assert defaults["01_launch"] == frozenset(
        {"commitment", "ignition", "heritage", "pressure", "future"}
    )
    assert defaults["12_still_kb"] == frozenset({"heritage", "humility"})


# --- scan_library ----------------------------------------------------------


def test_scan_indexes_clips_and_merges_moods(tmp_path, good_probe):
    cfg = make_cfg(tmp_path)
    add_clip(cfg, "01_launch", "sls_01__pressure.mp4")

    count = scan_library(cfg)

    assert count == 1
    indexed = read_index(cfg)[0]
    assert indexed["path"] == "01_launch/sls_01__pressure.mp4"
    assert indexed["folder"] == "01_launch"
    # folder defaults union the filename tag
    assert set(indexed["moods"].split("|")) == {
        "commitment", "ignition", "heritage", "pressure", "future"
    }
    assert indexed["duration_sec"] == "6.000"
    assert (indexed["width"], indexed["height"]) == ("1920", "1080")
    assert (indexed["faces"], indexed["logo_risk"]) == ("0", "0")
    assert (indexed["nasa_id"], indexed["credit"], indexed["last_used"]) == ("", "", "")


def test_scan_writes_the_agreed_header(tmp_path, good_probe):
    cfg = make_cfg(tmp_path)
    add_clip(cfg, "01_launch", "a.mp4")

    scan_library(cfg)

    with csv_path(cfg).open(newline="", encoding="utf-8") as handle:
        assert next(csv.reader(handle)) == FIELDNAMES


def test_scan_sets_the_flags_from_filename_tags(tmp_path, good_probe):
    cfg = make_cfg(tmp_path)
    add_clip(cfg, "05_eva", "eva_01__work_faces.mp4")
    add_clip(cfg, "05_eva", "eva_02__work_logo.mp4")

    scan_library(cfg)

    by_path = {r["path"]: r for r in read_index(cfg)}
    assert by_path["05_eva/eva_01__work_faces.mp4"]["faces"] == "1"
    assert by_path["05_eva/eva_02__work_logo.mp4"]["logo_risk"] == "1"


def test_scan_rejects_short_long_and_short_clips(tmp_path, monkeypatch, caplog):
    cfg = make_cfg(tmp_path)
    add_clip(cfg, "01_launch", "ok.mp4")
    add_clip(cfg, "01_launch", "brief.mp4")
    add_clip(cfg, "01_launch", "epic.mp4")
    add_clip(cfg, "01_launch", "sd.mp4")

    sizes = {
        "ok.mp4": (6.0, 1920, 1080),
        "brief.mp4": (2.0, 1920, 1080),
        "epic.mp4": (30.0, 1920, 1080),
        "sd.mp4": (6.0, 1280, 720),
    }
    monkeypatch.setattr(snippets, "probe_clip", lambda path: sizes[path.name])

    with caplog.at_level("WARNING"):
        count = scan_library(cfg)

    assert count == 1
    assert [r["path"] for r in read_index(cfg)] == ["01_launch/ok.mp4"]
    assert "brief.mp4" in caplog.text and "epic.mp4" in caplog.text
    assert "720 px tall" in caplog.text


def test_scan_skips_non_video_files(tmp_path, good_probe):
    cfg = make_cfg(tmp_path)
    add_clip(cfg, "01_launch", "clip.mp4")
    add_clip(cfg, "01_launch", "notes.txt")

    assert scan_library(cfg) == 1


def test_rescan_preserves_hand_edited_columns(tmp_path, good_probe):
    cfg = make_cfg(tmp_path)
    add_clip(cfg, "01_launch", "sls_01.mp4")
    scan_library(cfg)

    rows = read_index(cfg)
    rows[0]["nasa_id"] = "NASA-12345"
    rows[0]["credit"] = "NASA/KSC"
    rows[0]["last_used"] = "2026-09-20"
    write_index(cfg, rows)

    scan_library(cfg)

    after = read_index(cfg)[0]
    assert after["nasa_id"] == "NASA-12345"
    assert after["credit"] == "NASA/KSC"
    assert after["last_used"] == "2026-09-20"


def test_rescan_drops_rows_for_deleted_clips(tmp_path, good_probe):
    cfg = make_cfg(tmp_path)
    keep = add_clip(cfg, "01_launch", "keep.mp4")
    gone = add_clip(cfg, "01_launch", "gone.mp4")
    assert scan_library(cfg) == 2

    gone.unlink()

    assert scan_library(cfg) == 1
    assert [r["path"] for r in read_index(cfg)] == ["01_launch/keep.mp4"]
    assert keep.exists()


def test_scan_requires_the_library_directory(tmp_path):
    with pytest.raises(SnippetError, match="library directory not found"):
        scan_library(make_cfg(tmp_path))


def test_scan_leaves_no_temp_files(tmp_path, good_probe):
    cfg = make_cfg(tmp_path)
    add_clip(cfg, "01_launch", "a.mp4")

    scan_library(cfg)

    assert [p.name for p in Path(cfg.data_dir).iterdir()] == ["snippets.csv"]


# --- library_ok ------------------------------------------------------------


def test_library_ok_is_false_below_the_minimum(tmp_path):
    cfg = make_cfg(tmp_path, min_library_clips=3)
    write_index(cfg, [row("a.mp4"), row("b.mp4")])

    assert library_ok(cfg) is False


def test_library_ok_is_true_at_the_minimum(tmp_path):
    cfg = make_cfg(tmp_path, min_library_clips=2)
    write_index(cfg, [row("a.mp4"), row("b.mp4")])

    assert library_ok(cfg) is True


def test_library_ok_with_no_index_at_all(tmp_path):
    assert library_ok(make_cfg(tmp_path)) is False


# --- load_snippets ---------------------------------------------------------


def test_load_snippets_parses_types(tmp_path):
    cfg = make_cfg(tmp_path)
    write_index(cfg, [row("01_launch/a.mp4", "awe|grind", faces="1", last_used="2026-09-01")])

    snippet = load_snippets(cfg)[0]

    assert isinstance(snippet, Snippet)
    assert snippet.moods == ("awe", "grind")
    assert snippet.faces is True
    assert snippet.logo_risk is False
    assert snippet.last_used == date(2026, 9, 1)
    assert snippet.duration_sec == 6.0
    assert snippet.full_path == Path(cfg.library_dir) / "01_launch/a.mp4"


def test_load_snippets_reports_a_malformed_index(tmp_path):
    cfg = make_cfg(tmp_path)
    bad = row("a.mp4")
    bad["duration_sec"] = "not a number"
    write_index(cfg, [bad])

    with pytest.raises(SnippetError, match="malformed"):
        load_snippets(cfg)


# --- pick ------------------------------------------------------------------


def test_pick_returns_enough_clips_for_the_seconds(tmp_path):
    cfg = make_cfg(tmp_path)
    write_index(cfg, [row(f"{i}.mp4", "awe") for i in range(20)])

    assert len(pick(["awe"], 12.0, set(), cfg, TODAY)) == 4     # 12 / 3
    assert len(pick(["awe"], 10.0, set(), cfg, TODAY)) == 4     # rounds up
    assert len(pick(["awe"], 0.5, set(), cfg, TODAY)) == 1      # never zero


def test_pick_prefers_mood_matches(tmp_path):
    cfg = make_cfg(tmp_path)
    write_index(cfg, [
        row("none.mp4", "grind"),
        row("one.mp4", "awe"),
        row("two.mp4", "awe|scale"),
    ])

    chosen = pick(["awe", "scale"], 6.0, set(), cfg, TODAY)

    assert [s.path for s in chosen] == ["two.mp4", "one.mp4"]


def test_pick_orders_least_recently_used_first(tmp_path):
    cfg = make_cfg(tmp_path)
    write_index(cfg, [
        row("recent.mp4", "awe", last_used="2026-09-10"),
        row("never.mp4", "awe"),
        row("older.mp4", "awe", last_used="2026-09-02"),
    ])

    chosen = pick(["awe"], 9.0, set(), cfg, TODAY)

    assert [s.path for s in chosen] == ["never.mp4", "older.mp4", "recent.mp4"]


def test_pick_never_selects_a_logo_risk(tmp_path):
    cfg = make_cfg(tmp_path)
    write_index(cfg, [row("branded.mp4", "awe", logo="1"), row("clean.mp4", "awe")])

    chosen = pick(["awe"], 3.0, set(), cfg, TODAY)

    assert [s.path for s in chosen] == ["clean.mp4"]


def test_pick_skips_clips_used_today(tmp_path):
    cfg = make_cfg(tmp_path)
    write_index(cfg, [row("a.mp4", "awe"), row("b.mp4", "awe")])

    chosen = pick(["awe"], 3.0, {"a.mp4"}, cfg, TODAY)

    assert [s.path for s in chosen] == ["b.mp4"]


def test_pick_respects_the_cooldown(tmp_path):
    cfg = make_cfg(tmp_path, clip_cooldown_days=3)
    write_index(cfg, [
        row("cooling.mp4", "awe", last_used="2026-09-26"),   # 2 days ago
        row("ready.mp4", "awe", last_used="2026-09-25"),     # 3 days ago
    ])

    chosen = pick(["awe"], 3.0, set(), cfg, TODAY)

    assert [s.path for s in chosen] == ["ready.mp4"]


def test_pick_falls_back_to_any_mood_and_says_so(tmp_path, caplog):
    cfg = make_cfg(tmp_path)
    write_index(cfg, [row("a.mp4", "grind"), row("b.mp4", "work")])

    with caplog.at_level("WARNING"):
        chosen = pick(["awe"], 3.0, set(), cfg, TODAY)

    assert len(chosen) == 1
    assert "falling back to any mood" in caplog.text


def test_pick_fails_closed_when_the_pool_is_too_small(tmp_path):
    cfg = make_cfg(tmp_path)
    write_index(cfg, [row("a.mp4", "awe"), row("b.mp4", "awe")])

    with pytest.raises(SnippetError, match="need 4 clips"):
        pick(["awe"], 12.0, set(), cfg, TODAY)


def test_pick_keeps_a_face_off_the_front(tmp_path):
    cfg = make_cfg(tmp_path)
    write_index(cfg, [
        row("face.mp4", "awe|scale", faces="1"),   # best mood match
        row("plain.mp4", "awe"),
    ])

    chosen = pick(["awe", "scale"], 6.0, set(), cfg, TODAY)

    assert chosen[0].path == "plain.mp4"
    assert {s.path for s in chosen} == {"face.mp4", "plain.mp4"}


def test_pick_warns_when_every_clip_has_a_face(tmp_path, caplog):
    cfg = make_cfg(tmp_path)
    write_index(cfg, [row("f1.mp4", "awe", faces="1"), row("f2.mp4", "awe", faces="1")])

    with caplog.at_level("WARNING"):
        chosen = pick(["awe"], 6.0, set(), cfg, TODAY)

    assert len(chosen) == 2
    assert "flagged faces" in caplog.text


# --- mark_used -------------------------------------------------------------


def test_mark_used_stamps_only_the_named_clips(tmp_path):
    cfg = make_cfg(tmp_path)
    write_index(cfg, [row("a.mp4"), row("b.mp4"), row("c.mp4", last_used="2026-01-01")])

    mark_used(["a.mp4", "c.mp4"], TODAY, cfg)

    stamps = {r["path"]: r["last_used"] for r in read_index(cfg)}
    assert stamps == {"a.mp4": "2026-09-28", "b.mp4": "", "c.mp4": "2026-09-28"}


def test_mark_used_rejects_an_unknown_path(tmp_path):
    cfg = make_cfg(tmp_path)
    write_index(cfg, [row("a.mp4")])

    with pytest.raises(SnippetError, match="not in the index"):
        mark_used(["a.mp4", "ghost.mp4"], TODAY, cfg)


def test_mark_used_then_pick_respects_the_new_cooldown(tmp_path):
    cfg = make_cfg(tmp_path, clip_cooldown_days=3)
    write_index(cfg, [row("a.mp4", "awe"), row("b.mp4", "awe")])

    mark_used(["a.mp4"], TODAY, cfg)

    assert [s.path for s in pick(["awe"], 3.0, set(), cfg, TODAY)] == ["b.mp4"]


def test_mark_used_leaves_no_temp_files(tmp_path):
    cfg = make_cfg(tmp_path)
    write_index(cfg, [row("a.mp4")])

    mark_used(["a.mp4"], TODAY, cfg)

    assert [p.name for p in Path(cfg.data_dir).iterdir()] == ["snippets.csv"]


# --- CLI -------------------------------------------------------------------


def test_cli_rejects_bad_arguments(capsys):
    assert snippets.main([]) == 2
    assert "usage" in capsys.readouterr().err


def test_cli_scan_reports_the_count(tmp_path, monkeypatch, good_probe, capsys):
    cfg = make_cfg(tmp_path, min_library_clips=1)
    add_clip(cfg, "01_launch", "a.mp4")
    monkeypatch.setattr(snippets, "load_config", lambda: cfg)

    assert snippets.main(["scan"]) == 0

    out = capsys.readouterr().out
    assert "1 clips indexed" in out
    assert "library ok" in out


def test_cli_scan_flags_a_thin_library(tmp_path, monkeypatch, good_probe, capsys):
    cfg = make_cfg(tmp_path, min_library_clips=200)
    add_clip(cfg, "01_launch", "a.mp4")
    monkeypatch.setattr(snippets, "load_config", lambda: cfg)

    snippets.main(["scan"])

    assert "LIBRARY THIN: 1 of 200" in capsys.readouterr().out


# --- real FFmpeg -----------------------------------------------------------


def cut_fixture(path: Path, seconds: float, width: int, height: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"testsrc=size={width}x{height}:rate=30:duration={seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-an", str(path)],
        capture_output=True,
        check=True,
    )


@needs_ffmpeg
def test_probe_clip_against_a_real_file(tmp_path):
    clip = tmp_path / "real.mp4"
    cut_fixture(clip, 5.0, 1920, 1080)

    duration, width, height = snippets.probe_clip(clip)

    assert duration == pytest.approx(5.0, abs=0.1)
    assert (width, height) == (1920, 1080)


@needs_ffmpeg
def test_scan_a_real_library(tmp_path):
    cfg = make_cfg(tmp_path)
    cut_fixture(Path(cfg.library_dir) / "01_launch" / "sls__ignition_faces.mp4", 5.0, 1920, 1080)
    cut_fixture(Path(cfg.library_dir) / "01_launch" / "too_small.mp4", 5.0, 1280, 720)
    cut_fixture(Path(cfg.library_dir) / "04_orbit_earth" / "cupola.mp4", 2.0, 1920, 1080)

    count = scan_library(cfg)

    assert count == 1
    indexed = read_index(cfg)[0]
    assert indexed["path"] == "01_launch/sls__ignition_faces.mp4"
    assert indexed["faces"] == "1"
    assert float(indexed["duration_sec"]) == pytest.approx(5.0, abs=0.1)
    assert indexed["height"] == "1080"


# --- review inbox and provenance (T8a) --------------------------------------


def test_scan_skips_the_review_inbox(tmp_path, good_probe):
    cfg = make_cfg(tmp_path)
    add_clip(cfg, "05_eva", "kept.mp4")
    add_clip(cfg, "_inbox", "waiting.mp4")
    add_clip(cfg, "_inbox/_review", "waiting.mp4")

    assert scan_library(cfg) == 1
    assert [r["path"] for r in read_index(cfg)] == ["05_eva/kept.mp4"]


def test_scan_fills_blank_provenance_but_keeps_hand_edits(tmp_path, good_probe):
    cfg = make_cfg(tmp_path)
    add_clip(cfg, "05_eva", "new.mp4")
    add_clip(cfg, "05_eva", "edited.mp4")
    write_index(cfg, [row("05_eva/edited.mp4") | {"nasa_id": "hand", "credit": "by hand"}])

    scan_library(cfg, {
        "05_eva/new.mp4": ("jsc1", "NASA one"),
        "05_eva/edited.mp4": ("jsc2", "NASA two"),
    })

    rows = {r["path"]: r for r in read_index(cfg)}
    assert (rows["05_eva/new.mp4"]["nasa_id"], rows["05_eva/new.mp4"]["credit"]) == ("jsc1", "NASA one")
    assert (rows["05_eva/edited.mp4"]["nasa_id"], rows["05_eva/edited.mp4"]["credit"]) == ("hand", "by hand")
