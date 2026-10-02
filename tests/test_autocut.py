"""T8a: broll/autocut.py.

The end-to-end tests cut a generated 1080p source with four scenes: moving test
bars (6 s), black (5 s), moving HD bars (14 s) and flat grey (6 s). Expected:
one clip, a near-black drop, two clips, one clip flagged static.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from broll import autocut
from broll.autocut import (
    CLIP_MAX_SEC,
    CLIP_MIN_SEC,
    INBOX,
    PAGE_NAME,
    REVIEW_DIR,
    TARGET_FOLDERS,
    clip_name,
    load_manifest,
    parse_source,
    reject_reason,
    safe_id,
    split_scene,
    write_review_page,
)
from pipeline.script_agent import MOODS
from pipeline.snippets import MAX_DURATION_SEC, MIN_DURATION_SEC, probe_clip

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="FFmpeg not on PATH")

SOURCE_NAME = "05_eva_jsc2024__eva-01.mp4"
NASA_ID = "jsc2024__eva-01"


def make_video(dest: Path, scenes: list[str], size: str = "1920x1080") -> Path:
    """Concatenate lavfi sources (each given as ``filter:d=<sec>``) into one H.264 file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = []
    for scene in scenes:
        args += ["-f", "lavfi", "-i", scene.format(size=size)]
    inputs = "".join(f"[{i}]" for i in range(len(scenes)))
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", *args, "-filter_complex",
         f"{inputs}concat=n={len(scenes)}:v=1:a=0,format=yuv420p",
         "-c:v", "libx264", "-preset", "ultrafast", str(dest)],
        check=True,
    )
    return dest


FOUR_SCENES = [
    "testsrc2=s={size}:r=10:d=6,scroll=h=0.005",
    "color=c=black:s={size}:r=10:d=5",
    "smptehdbars=s={size}:r=10:d=14,scroll=h=-0.01",
    "color=c=0x808080:s={size}:r=10:d=6",
]


@pytest.fixture(scope="module")
def cut(tmp_path_factory):
    """One autocut run over the four-scene source; returns (library, new clips)."""
    if not HAS_FFMPEG:
        pytest.skip("FFmpeg not on PATH")
    root = tmp_path_factory.mktemp("autocut")
    make_video(root / "sources" / SOURCE_NAME, FOUR_SCENES)
    library = root / "library"
    return library, autocut.autocut(root / "sources", library)


# --- names -----------------------------------------------------------------


@pytest.mark.parametrize("name, expected", [
    ("05_eva_jsc2024-01.mp4", ("05_eva", "jsc2024-01")),
    ("10_failure_process_KSC_1.mov", ("10_failure_process", "KSC_1")),
    ("_inbox_iss071e1.mp4", ("", "iss071e1")),
    ("my_own_upload.mp4", ("", "my_own_upload")),
])
def test_parse_source_reads_the_collector_name(name, expected):
    assert parse_source(Path(name)) == expected


def test_safe_id_never_contains_the_tag_separator():
    assert safe_id("a__b c/d") == "a_b_c_d"
    assert "__" not in safe_id("__x___y__")


def test_clip_name_carries_the_nasa_id_and_start():
    assert clip_name("KSC-20221116", 12.34) == "KSC-20221116_t00123.mp4"


# --- splitting -------------------------------------------------------------


def test_a_short_scene_gives_no_clip():
    assert split_scene(0.0, CLIP_MIN_SEC) == []


def test_a_mid_scene_gives_one_trimmed_clip():
    [(start, end)] = split_scene(10.0, 16.0)
    assert start > 10.0 and end < 16.0


@pytest.mark.parametrize("length", [CLIP_MAX_SEC + 0.5, 30.0, 61.0, 200.0])
def test_long_scenes_split_into_equal_legal_pieces(length):
    pieces = split_scene(5.0, 5.0 + length)

    assert len(pieces) > 1
    for start, end in pieces:
        assert CLIP_MIN_SEC <= end - start <= CLIP_MAX_SEC
    assert all(a[1] == pytest.approx(b[0]) for a, b in zip(pieces, pieces[1:]))


def test_targets_sit_inside_the_scanner_limits():
    assert MIN_DURATION_SEC < CLIP_MIN_SEC < CLIP_MAX_SEC < MAX_DURATION_SEC


# --- frame checks ----------------------------------------------------------


def frames(make, count=8):
    return [make(i).astype(np.float32) for i in range(count)]


def test_black_frames_are_rejected():
    assert reject_reason(frames(lambda i: np.full((90, 160), 6 + i % 2))) == "near black"


def test_a_lit_object_on_black_is_not_black():
    def planet(i):
        frame = np.zeros((90, 160))
        frame[30:60, 40 + 5 * i:80 + 5 * i] = 200
        return frame

    assert reject_reason(frames(planet)) is None


def test_still_frames_are_rejected():
    still = np.random.default_rng(1).integers(0, 255, (90, 160))
    assert reject_reason(frames(lambda i: still)) == "near static"


def test_moving_frames_pass():
    rng = np.random.default_rng(2)
    assert reject_reason(frames(lambda i: rng.integers(0, 255, (90, 160)))) is None


def test_unreadable_when_too_few_frames():
    assert reject_reason([]) == "unreadable"


# --- end to end ------------------------------------------------------------


def test_scenes_become_clips_black_is_dropped_static_is_flagged(cut):
    library, made = cut

    assert [(c.name, c.static) for c in made] == [
        ("jsc2024_eva-01_t00002.mp4", False),
        ("jsc2024_eva-01_t00112.mp4", False),
        ("jsc2024_eva-01_t00180.mp4", False),
        ("jsc2024_eva-01_t00252.mp4", True),
    ]
    assert {c.nasa_id for c in made} == {NASA_ID}
    assert {c.folder for c in made} == {"05_eva"}
    manifest = load_manifest(library / INBOX)
    assert manifest["sources"][SOURCE_NAME]["dropped"] == {"near black": 1}
    assert manifest["sources"][SOURCE_NAME]["static"] == 1
    assert manifest["clips"]["jsc2024_eva-01_t00252.mp4"]["static"] is True
    assert manifest["clips"]["jsc2024_eva-01_t00002.mp4"]["static"] is False
    assert (library / INBOX / "jsc2024_eva-01_t00252.mp4").is_file()


def test_clips_are_1080p_in_range_and_muted(cut):
    library, made = cut
    for clip in made:
        path = library / INBOX / clip.name
        duration, width, height = probe_clip(path)
        assert (width, height) == (1920, 1080)
        assert MIN_DURATION_SEC <= duration <= MAX_DURATION_SEC
        streams = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True,
        ).stdout.split()
        assert streams == ["video"]


def test_every_clip_has_a_thumbnail_and_a_small_preview(cut):
    library, made = cut
    review = library / INBOX / REVIEW_DIR
    for clip in made:
        stem = Path(clip.name).stem
        assert (review / f"{stem}.jpg").stat().st_size > 0
        duration, _, height = probe_clip(review / f"{stem}.mp4")
        assert height == 270
        assert duration <= 3.1


def page_data(library: Path) -> dict:
    text = (library / INBOX / PAGE_NAME).read_text(encoding="utf-8")
    match = re.search(r"const DATA = (.*?);\n", text)
    return json.loads(match.group(1))


def test_review_page_lists_clips_folders_moods_and_flags(cut):
    library, made = cut
    data = page_data(library)

    assert [c["name"] for c in data["clips"]] == [c.name for c in made]
    assert data["clips"][0]["nasa_id"] == NASA_ID
    assert data["clips"][0]["folder"] == "05_eva"
    assert data["folders"] == TARGET_FOLDERS
    assert INBOX not in data["folders"]
    assert data["moods"] == sorted(MOODS)
    assert data["flags"] == ["faces", "logo"]
    assert data["folderMoods"]["05_eva"] == ["isolation", "work"]
    assert [c["static"] for c in data["clips"]] == [False, False, False, True]


def test_review_page_has_a_flagged_static_section():
    text = autocut.TEMPLATE_PATH.read_text(encoding="utf-8")

    assert 'id="static-section" hidden' in text
    assert "Flagged static" in text
    assert "staticGrid.append(card(clip))" in text
    # Static cards start undecided like every other card.
    assert 'return { decision: null' in text


def test_manifests_from_before_the_static_flag_still_load(tmp_path):
    inbox = tmp_path / INBOX
    inbox.mkdir()
    (inbox / "old_t00000.mp4").write_bytes(b"")
    autocut.save_manifest(inbox, {"sources": {}, "clips": {"old_t00000.mp4": {
        "nasa_id": "old", "source": "s.mp4", "start": 0, "end": 5, "folder": ""}}})

    [clip] = autocut.pending_clips(inbox, load_manifest(inbox))

    assert clip.static is False


def test_a_rerun_skips_sources_already_cut(cut, monkeypatch):
    library, _ = cut
    sources = library.parent / "sources"
    monkeypatch.setattr(autocut, "cut_source", lambda *a: pytest.fail("recut a done source"))

    assert autocut.autocut(sources, library) == []


@needs_ffmpeg
def test_force_recuts_and_low_res_sources_are_skipped(tmp_path):
    sources = tmp_path / "sources"
    make_video(sources / "01_launch_small.mp4", FOUR_SCENES[:1], size="1280x720")
    library = tmp_path / "library"

    assert autocut.autocut(sources, library) == []
    assert autocut.autocut(sources, library, force=True) == []
    manifest = load_manifest(library / INBOX)
    assert manifest["sources"]["01_launch_small.mp4"]["dropped"] == {"under 1080 px": 1}
    assert manifest["clips"] == {}


@needs_ffmpeg
def test_a_source_without_cuts_is_one_scene(tmp_path):
    make_video(tmp_path / "sources" / "08_deep_one.mp4", ["testsrc2=s={size}:r=10:d=7,scroll=h=0.005"])

    made = autocut.autocut(tmp_path / "sources", tmp_path / "library")

    assert [c.name for c in made] == ["one_t00002.mp4"]


def test_a_broken_source_is_logged_and_the_rest_continue(tmp_path, caplog):
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "05_eva_broken.mp4").write_bytes(b"not a video")

    assert autocut.autocut(sources, tmp_path / "library") == []
    assert "05_eva_broken.mp4" in caplog.text
    assert load_manifest(tmp_path / "library" / INBOX)["sources"] == {}


# --- page ------------------------------------------------------------------


def test_page_without_clips_still_renders(tmp_path):
    write_review_page(tmp_path)

    assert page_data(tmp_path)["clips"] == []


def test_page_data_cannot_close_the_script_tag(tmp_path):
    inbox = tmp_path / INBOX
    inbox.mkdir()
    (inbox / "x_t00000.mp4").write_bytes(b"")
    autocut.save_manifest(inbox, {"sources": {}, "clips": {"x_t00000.mp4": {
        "nasa_id": "</script><b>", "source": "s.mp4", "start": 0, "end": 5, "folder": ""}}})

    write_review_page(tmp_path)

    text = (inbox / PAGE_NAME).read_text(encoding="utf-8")
    assert text.count("</script>") == 1
    assert page_data(tmp_path)["clips"][0]["nasa_id"] == "</script><b>"


def test_page_hides_clips_no_longer_in_the_inbox(tmp_path):
    inbox = tmp_path / INBOX
    inbox.mkdir()
    (inbox / "here_t00000.mp4").write_bytes(b"")
    entry = {"nasa_id": "n", "source": "s.mp4", "start": 0, "end": 5, "folder": ""}
    autocut.save_manifest(inbox, {"sources": {}, "clips": {
        "here_t00000.mp4": entry, "gone_t00000.mp4": entry}})

    write_review_page(tmp_path)

    assert [c["name"] for c in page_data(tmp_path)["clips"]] == ["here_t00000.mp4"]
