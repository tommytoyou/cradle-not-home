"""T8a: broll/apply_review.py.

Most tests use stand-in clip files and a probe that reports every clip as a
valid 1080p, 6 s video; the last one runs autocut and the real scanner.
"""

import csv
import json
import shutil
from pathlib import Path

import pytest

from broll import apply_review as review_module
from broll import autocut
from broll.apply_review import (
    ReviewError,
    apply_review,
    credit_for,
    load_decisions,
    tagged_name,
)
from broll.autocut import INBOX, PAGE_NAME, REVIEW_DIR, load_manifest
from pipeline import snippets
from pipeline.snippets import csv_path
from tests.test_autocut import FOUR_SCENES, NASA_ID, SOURCE_NAME, make_video, needs_ffmpeg, page_data
from tests.test_snippets import make_cfg


CLIPS = {
    "jsc1_t00002.mp4": {"nasa_id": "jsc1", "source": "05_eva_jsc1.mp4", "start": 0.2, "end": 6.0, "folder": "05_eva"},
    "jsc1_t00100.mp4": {"nasa_id": "jsc1", "source": "05_eva_jsc1.mp4", "start": 10.0, "end": 16.0, "folder": "05_eva"},
    "KSC_2_t00002.mp4": {"nasa_id": "KSC__2", "source": "KSC__2.mp4", "start": 0.2, "end": 7.0, "folder": ""},
}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A library whose inbox holds CLIPS with previews and a manifest."""
    monkeypatch.setattr(snippets, "probe_clip", lambda path: (6.0, 1920, 1080))
    cfg = make_cfg(tmp_path)
    inbox = Path(cfg.library_dir) / INBOX
    (inbox / REVIEW_DIR).mkdir(parents=True)
    for name in CLIPS:
        (inbox / name).write_bytes(b"clip " + name.encode())
        for suffix in (".jpg", ".mp4"):
            (inbox / REVIEW_DIR / (Path(name).stem + suffix)).write_bytes(b"preview")
    autocut.save_manifest(inbox, {"sources": {"05_eva_jsc1.mp4": {}}, "clips": dict(CLIPS)})
    return cfg


def decide(tmp_path: Path, clips: dict) -> Path:
    path = tmp_path / "review_decisions.json"
    path.write_text(json.dumps({"version": 1, "clips": clips}), encoding="utf-8")
    return path


def keep(folder="05_eva", moods=(), faces=False, logo=False) -> dict:
    return {"decision": "keep", "folder": folder, "moods": list(moods), "faces": faces, "logo": logo}


REJECT = {"decision": "reject"}


def library_files(cfg) -> list[str]:
    root = Path(cfg.library_dir)
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def index(cfg) -> dict[str, dict]:
    with csv_path(cfg).open(newline="", encoding="utf-8") as handle:
        return {row["path"]: row for row in csv.DictReader(handle)}


# --- names -----------------------------------------------------------------


def test_tagged_name_sorts_moods_then_flags():
    assert tagged_name("a_t00001.mp4", ["work", "awe"], True, True) == "a_t00001__awe_work_faces_logo.mp4"


def test_tagged_name_without_tags_is_unchanged():
    assert tagged_name("a_t00001.mp4", [], False, False) == "a_t00001.mp4"


def test_tagged_names_read_back_through_the_scanner():
    moods, faces, logo = snippets.parse_tags(Path(tagged_name("x_t1.mp4", ["awe"], True, False)).stem)
    assert (moods, faces, logo) == ({"awe"}, True, False)


def test_credit_links_the_nasa_page():
    assert credit_for("jsc1") == "NASA (https://images.nasa.gov/details/jsc1)"


# --- applying --------------------------------------------------------------


def test_keep_moves_with_tags_and_reject_deletes(tmp_path, cfg):
    summary = apply_review(decide(tmp_path, {
        "jsc1_t00002.mp4": keep("05_eva", ["work", "awe"], faces=True),
        "jsc1_t00100.mp4": REJECT,
    }), cfg)

    files = library_files(cfg)
    assert "05_eva/jsc1_t00002__awe_work_faces.mp4" in files
    assert not any(f.startswith(f"{INBOX}/jsc1_") for f in files)
    assert not any("jsc1_t" in f for f in files if f.startswith(f"{INBOX}/{REVIEW_DIR}/"))
    assert summary.kept == ["05_eva/jsc1_t00002__awe_work_faces.mp4"]
    assert summary.rejected == ["jsc1_t00100.mp4"]
    assert summary.left == 1


def test_kept_clips_are_indexed_with_nasa_id_and_credit(tmp_path, cfg):
    apply_review(decide(tmp_path, {
        "jsc1_t00002.mp4": keep("05_eva", ["awe"], faces=True),
        "KSC_2_t00002.mp4": keep("08_deep", logo=True),
    }), cfg)

    rows = index(cfg)
    eva = rows["05_eva/jsc1_t00002__awe_faces.mp4"]
    assert (eva["nasa_id"], eva["credit"]) == ("jsc1", credit_for("jsc1"))
    assert eva["moods"] == "awe|isolation|work"
    assert (eva["faces"], eva["logo_risk"]) == ("1", "0")
    deep = rows["08_deep/KSC_2_t00002__logo.mp4"]
    assert deep["nasa_id"] == "KSC__2"  # exact id from the manifest, not the file name
    assert deep["logo_risk"] == "1"
    assert not any(path.startswith(INBOX) for path in rows)


def test_undecided_clips_stay_and_the_page_is_rewritten(tmp_path, cfg):
    apply_review(decide(tmp_path, {"jsc1_t00002.mp4": REJECT}), cfg)

    library = Path(cfg.library_dir)
    names = [c["name"] for c in page_data(library)["clips"]]
    assert names == ["KSC_2_t00002.mp4", "jsc1_t00100.mp4"]
    assert set(load_manifest(library / INBOX)["clips"]) == set(names)
    assert (library / INBOX / REVIEW_DIR / "jsc1_t00100.jpg").is_file()


def test_applying_the_same_file_twice_is_harmless(tmp_path, cfg, caplog):
    decisions = decide(tmp_path, {"jsc1_t00002.mp4": keep(), "jsc1_t00100.mp4": REJECT})
    apply_review(decisions, cfg)
    before = library_files(cfg)

    # The manifest forgot them, so a second run reports them as unknown.
    with pytest.raises(ReviewError, match="not an autocut clip"):
        apply_review(decisions, cfg)
    assert library_files(cfg) == before


def test_a_decided_clip_already_gone_is_skipped(tmp_path, cfg, caplog):
    (Path(cfg.library_dir) / INBOX / "jsc1_t00100.mp4").unlink()

    summary = apply_review(decide(tmp_path, {"jsc1_t00100.mp4": REJECT}), cfg)

    assert summary.missing == ["jsc1_t00100.mp4"]
    assert "no longer in _inbox" in caplog.text
    assert "jsc1_t00100.mp4" not in load_manifest(Path(cfg.library_dir) / INBOX)["clips"]


# --- validation ------------------------------------------------------------


@pytest.mark.parametrize("decision, message", [
    ({"jsc1_t00002.mp4": {"decision": "maybe"}}, "keep or reject"),
    ({"jsc1_t00002.mp4": keep(folder="99_nowhere")}, "unknown folder"),
    ({"jsc1_t00002.mp4": keep(folder=INBOX)}, "unknown folder"),
    ({"jsc1_t00002.mp4": keep(folder="")}, "unknown folder"),
    ({"jsc1_t00002.mp4": keep(moods=["awe", "spooky"])}, r"unknown moods \['spooky'\]"),
    ({"jsc1_t00002.mp4": {**keep(), "moods": "awe"}}, "list of names"),
    ({"stranger.mp4": REJECT}, "not an autocut clip"),
])
def test_a_bad_decision_changes_nothing(tmp_path, cfg, decision, message):
    before = library_files(cfg)
    good = {"jsc1_t00100.mp4": REJECT}

    with pytest.raises(ReviewError, match=message):
        apply_review(decide(tmp_path, good | decision), cfg)

    assert library_files(cfg) == before
    assert not csv_path(cfg).exists()


def test_every_problem_is_listed_at_once(tmp_path, cfg):
    with pytest.raises(ReviewError) as caught:
        apply_review(decide(tmp_path, {
            "jsc1_t00002.mp4": keep(folder="nope"),
            "jsc1_t00100.mp4": keep(moods=["spooky"]),
        }), cfg)

    assert "jsc1_t00002.mp4" in str(caught.value)
    assert "jsc1_t00100.mp4" in str(caught.value)


def test_an_existing_target_is_not_overwritten(tmp_path, cfg):
    target = Path(cfg.library_dir) / "05_eva" / "jsc1_t00002.mp4"
    target.parent.mkdir()
    target.write_bytes(b"older clip")

    with pytest.raises(ReviewError, match="already exists"):
        apply_review(decide(tmp_path, {"jsc1_t00002.mp4": keep()}), cfg)

    assert target.read_bytes() == b"older clip"


def test_missing_ffprobe_stops_before_moving(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(review_module.shutil, "which", lambda name: None)
    before = library_files(cfg)

    with pytest.raises(ReviewError, match="ffprobe"):
        apply_review(decide(tmp_path, {"jsc1_t00002.mp4": keep()}), cfg)

    assert library_files(cfg) == before


def test_load_decisions_errors(tmp_path):
    with pytest.raises(ReviewError, match="export it from review.html"):
        load_decisions(tmp_path / "missing.json")
    (tmp_path / "bad.json").write_text("{nope")
    with pytest.raises(ReviewError, match="not valid JSON"):
        load_decisions(tmp_path / "bad.json")
    (tmp_path / "other.json").write_text('{"something": 1}')
    with pytest.raises(ReviewError, match="no 'clips'"):
        load_decisions(tmp_path / "other.json")


def test_load_decisions_accepts_a_bom(tmp_path):
    path = tmp_path / "d.json"
    path.write_text('{"clips": {}}', encoding="utf-8-sig")
    assert load_decisions(path) == {}


def test_main_reports_errors_and_exits_1(tmp_path, cfg, monkeypatch, capsys):
    monkeypatch.setattr(review_module, "load_config", lambda: cfg)

    assert review_module.main([str(tmp_path / "missing.json")]) == 1
    assert "no decisions file" in capsys.readouterr().err


def test_main_defaults_to_the_inbox_file(cfg, monkeypatch, capsys):
    monkeypatch.setattr(review_module, "load_config", lambda: cfg)
    decide(Path(cfg.library_dir) / INBOX, {"jsc1_t00100.mp4": REJECT})

    assert review_module.main([]) == 0
    assert "kept 0, rejected 1, 2 still to review" in capsys.readouterr().out


# --- with real video -------------------------------------------------------


@needs_ffmpeg
def test_autocut_then_apply_with_the_real_scanner(tmp_path):
    cfg = make_cfg(tmp_path)
    make_video(tmp_path / "sources" / SOURCE_NAME, FOUR_SCENES[:1])
    [clip] = autocut.autocut(tmp_path / "sources", Path(cfg.library_dir))

    apply_review(decide(tmp_path, {clip.name: keep("04_orbit_earth", ["awe"])}), cfg)

    rows = index(cfg)
    [(path, row)] = rows.items()
    assert path == "04_orbit_earth/jsc2024_eva-01_t00002__awe.mp4"
    assert (row["nasa_id"], row["height"]) == (NASA_ID, "1080")
    assert row["credit"] == credit_for(NASA_ID)
    assert not (Path(cfg.library_dir) / INBOX / REVIEW_DIR / (Path(clip.name).stem + ".mp4")).exists()
    assert page_data(Path(cfg.library_dir))["clips"] == []
    assert (Path(cfg.library_dir) / INBOX / PAGE_NAME).is_file()
