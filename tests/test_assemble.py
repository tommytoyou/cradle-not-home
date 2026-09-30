"""T4a: shot cutting, source offsets, caption chunking, and the timeline.

The picker runs for real against a CSV index in ``tmp_path``; no FFmpeg, no
network. Render (T4b) tests will join this file when that ticket lands.
"""

import csv
from datetime import date
from pathlib import Path

import pytest

from pipeline.assemble import (
    MAX_SHOT_SEC,
    MIN_SHOT_SEC,
    SKIP_HEAD_SEC,
    Caption,
    Shot,
    TimelineError,
    block_captions,
    block_spans,
    build_timeline,
    chunk_words,
    shot_count,
    source_start,
)
from pipeline.config import Config
from pipeline.snippets import FIELDNAMES, csv_path
from pipeline.tts import BlockAudio

TODAY = date(2026, 9, 28)
GAP = 0.35


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


def row(path, moods="awe", *, faces="0", duration="6.000", folder="04_orbit_earth"):
    return {
        "path": path, "folder": folder, "moods": moods,
        "duration_sec": duration, "width": "1920", "height": "1080",
        "nasa_id": "", "credit": "", "faces": faces, "logo_risk": "0",
        "last_used": "",
    }


def write_index(cfg: Config, rows: list[dict]) -> None:
    path = csv_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        for item in rows:
            writer.writerow(item)


def audio(index: int, seconds: float) -> BlockAudio:
    return BlockAudio(index=index, path=Path(f"block{index}.mp3"), duration_sec=seconds)


VIDEO = {
    "format": "punch",
    "hook_text": "Gravity is not the enemy",
    "script_blocks": [
        {
            "text": "You think the pressure is your problem.",
            "moods": ["pressure"],
            "emphasis": ["pressure"],
        },
        {
            "text": "Not by resisting their pull, but by using it to go further than fuel alone allows.",
            "moods": ["awe"],
            "emphasis": ["pull", "further"],
        },
        {
            "text": "Then let it throw you.",
            "moods": ["leaving"],
            "emphasis": [],
        },
    ],
}


@pytest.fixture
def cfg(tmp_path):
    config = make_cfg(tmp_path)
    write_index(config, [row(f"04_orbit_earth/{i:02d}.mp4") for i in range(30)])
    return config


# --- shot_count ------------------------------------------------------------


@pytest.mark.parametrize("span", [2.0, 2.5, 3.9, 4.0, 4.1, 5.0, 7.3, 9.0, 12.35, 20.0, 31.7])
def test_shot_count_keeps_every_shot_between_two_and_four_seconds(span):
    length = span / shot_count(span)

    assert MIN_SHOT_SEC <= length <= MAX_SHOT_SEC


def test_shot_count_aims_for_three_seconds():
    assert shot_count(9.0) == 3
    assert shot_count(30.0) == 10


def test_a_span_under_two_seconds_is_one_shot():
    assert shot_count(1.4) == 1
    assert shot_count(1.5) == 1


def test_a_four_second_span_is_one_shot():
    assert shot_count(4.0) == 1


# --- source_start ----------------------------------------------------------


def test_source_start_skips_the_head_and_centres_the_shot():
    # 8 s clip, first second skipped: 7 s usable, 3 s shot centred in it.
    assert source_start(8.0, 3.0) == pytest.approx(SKIP_HEAD_SEC + 2.0)


def test_source_start_never_runs_past_the_clip():
    start = source_start(4.0, 3.0)

    assert start >= SKIP_HEAD_SEC
    assert start + 3.0 <= 4.0


def test_source_start_takes_the_middle_when_the_head_cannot_be_skipped():
    assert source_start(4.0, 3.6) == pytest.approx(0.2)


def test_source_start_rejects_a_shot_longer_than_the_clip():
    with pytest.raises(TimelineError, match="does not fit"):
        source_start(4.0, 4.5)


# --- block_spans -----------------------------------------------------------


def test_block_spans_give_each_gap_to_the_block_before_it():
    spans = block_spans([audio(0, 3.0), audio(1, 5.0), audio(2, 2.0)], GAP)

    assert spans == [
        (0.0, pytest.approx(3.35)),
        (pytest.approx(3.35), pytest.approx(5.35)),
        (pytest.approx(8.7), pytest.approx(2.0)),
    ]


# --- captions --------------------------------------------------------------


@pytest.mark.parametrize("count", range(3, 31))
def test_chunks_are_three_to_five_words_and_keep_every_word(count):
    words = [f"w{i}" for i in range(count)]

    chunks = chunk_words(words)

    assert [w for chunk in chunks for w in chunk] == words
    assert all(3 <= len(chunk) <= 5 for chunk in chunks)


def test_a_long_line_breaks_into_three_to_five_word_chunks():
    text = "You were born in a cradle and you called it a home"

    chunks = chunk_words(text.split())

    assert all(3 <= len(chunk) <= 5 for chunk in chunks)
    assert " ".join(" ".join(chunk) for chunk in chunks) == text


def test_a_short_line_stays_whole():
    assert chunk_words("Leave the well.".split()) == [["Leave", "the", "well."]]


def test_a_short_block_is_one_chunk():
    assert chunk_words(["Go."]) == [["Go."]]
    assert chunk_words([]) == []


def test_block_captions_cover_the_spoken_audio_exactly():
    block = VIDEO["script_blocks"][1]

    captions = block_captions(block, start=4.0, duration=6.0)

    assert captions[0].start == 4.0
    assert captions[-1].end == 10.0
    for before, after in zip(captions, captions[1:]):
        assert before.end == pytest.approx(after.start, abs=0.001)
        assert before.end > before.start
    assert " ".join(c.text for c in captions) == block["text"]


def test_block_captions_mark_emphasis_through_punctuation_and_case():
    block = {"text": "Use the PULL, not the fuel alone.", "moods": ["awe"], "emphasis": ["pull"]}

    captions = block_captions(block, start=0.0, duration=3.0)

    hit = [c for c in captions if "PULL," in c.text]
    assert hit and hit[0].emphasis == ["pull"]
    assert all(c.emphasis == [] for c in captions if c not in hit)


# --- build_timeline --------------------------------------------------------


def blocks_for_video():
    return [audio(0, 2.6), audio(1, 7.4), audio(2, 1.9)]


def test_timeline_covers_the_whole_voice_track(cfg):
    blocks = blocks_for_video()

    shots, captions = build_timeline(VIDEO, blocks, cfg, TODAY, set())

    voice_sec = sum(b.duration_sec for b in blocks) + GAP * (len(blocks) - 1)
    assert sum(s.duration for s in shots) == pytest.approx(voice_sec, abs=0.002)
    assert captions[-1].end == pytest.approx(voice_sec, abs=0.002)
    assert all(isinstance(s, Shot) for s in shots)
    assert all(isinstance(c, Caption) for c in captions)


def test_every_shot_is_two_to_four_seconds_when_the_block_allows(cfg):
    shots, _ = build_timeline(VIDEO, [audio(0, 3.0), audio(1, 9.0), audio(2, 5.0)], cfg, TODAY, set())

    assert all(MIN_SHOT_SEC <= s.duration <= MAX_SHOT_SEC for s in shots)


def test_cuts_land_on_block_boundaries(cfg):
    blocks = blocks_for_video()

    shots, _ = build_timeline(VIDEO, blocks, cfg, TODAY, set())

    cut_times, cursor = set(), 0.0
    for shot in shots:
        cursor = round(cursor + shot.duration, 3)
        cut_times.add(cursor)
    for start, _span in block_spans(blocks, GAP)[1:]:
        assert round(start, 3) in cut_times


def test_no_clip_repeats_and_used_today_is_updated(cfg):
    used = {"04_orbit_earth/00.mp4"}

    shots, _ = build_timeline(VIDEO, blocks_for_video(), cfg, TODAY, used)

    paths = [s.snippet for s in shots]
    assert len(paths) == len(set(paths))
    assert Path(cfg.library_dir) / "04_orbit_earth/00.mp4" not in paths
    assert len(used) == 1 + len(shots)


def test_the_second_video_of_the_day_takes_fresh_clips(cfg):
    used: set[str] = set()

    first, _ = build_timeline(VIDEO, blocks_for_video(), cfg, TODAY, used)
    second, _ = build_timeline(VIDEO, blocks_for_video(), cfg, TODAY, used)

    assert not {s.snippet for s in first} & {s.snippet for s in second}


def test_shots_come_from_the_middle_of_each_snippet(cfg):
    shots, _ = build_timeline(VIDEO, blocks_for_video(), cfg, TODAY, set())

    for shot in shots:
        assert shot.src_start >= SKIP_HEAD_SEC
        assert shot.src_start + shot.duration <= 6.0


def test_the_first_shot_is_never_a_face(tmp_path):
    cfg = make_cfg(tmp_path)
    write_index(cfg, [
        row("face.mp4", "pressure|awe", faces="1"),    # best match for block 0
        *[row(f"{i}.mp4", "awe") for i in range(10)],
    ])

    shots, _ = build_timeline(VIDEO, blocks_for_video(), cfg, TODAY, set())

    assert shots[0].snippet.name != "face.mp4"


def test_stills_always_zoom_and_other_shots_alternate(tmp_path):
    cfg = make_cfg(tmp_path)
    write_index(cfg, [
        row("still.mp4", "pressure", folder="12_still_kb"),
        *[row(f"{i}.mp4", "awe") for i in range(10)],
    ])

    shots, _ = build_timeline(VIDEO, blocks_for_video(), cfg, TODAY, set())

    assert shots[0].snippet.name == "still.mp4" and shots[0].zoom
    assert [s.zoom for s in shots[1:]] == [i % 2 == 1 for i in range(1, len(shots))]


def test_block_moods_drive_the_pick(tmp_path):
    cfg = make_cfg(tmp_path)
    write_index(cfg, [
        row("leaving.mp4", "leaving"),
        row("pressure.mp4", "pressure"),
        *[row(f"awe{i}.mp4", "awe") for i in range(6)],
    ])

    shots, _ = build_timeline(VIDEO, blocks_for_video(), cfg, TODAY, set())

    assert shots[0].snippet.name == "pressure.mp4"
    assert shots[-1].snippet.name == "leaving.mp4"


def test_captions_follow_the_speech_not_the_gap(cfg):
    blocks = blocks_for_video()

    _, captions = build_timeline(VIDEO, blocks, cfg, TODAY, set())

    second_block_start = blocks[0].duration_sec + GAP
    first_block = [c for c in captions if c.start < second_block_start]
    assert first_block[-1].end == pytest.approx(blocks[0].duration_sec)


def test_audio_must_match_the_script_blocks(cfg):
    with pytest.raises(TimelineError, match="3 script blocks"):
        build_timeline(VIDEO, [audio(0, 2.0), audio(1, 3.0)], cfg, TODAY, set())


def test_blocks_are_placed_by_index_not_list_order(cfg):
    blocks = blocks_for_video()

    in_order, _ = build_timeline(VIDEO, blocks, cfg, TODAY, set())
    shuffled, _ = build_timeline(VIDEO, blocks[::-1], cfg, TODAY, set())

    assert [s.duration for s in in_order] == [s.duration for s in shuffled]


def test_a_thin_library_fails_closed(tmp_path):
    from pipeline.snippets import SnippetError

    cfg = make_cfg(tmp_path)
    write_index(cfg, [row("only.mp4", "awe")])

    with pytest.raises(SnippetError):
        build_timeline(VIDEO, blocks_for_video(), cfg, TODAY, set())


def test_a_face_opens_with_a_warning_when_there_are_no_spare_clips(tmp_path, caplog):
    cfg = make_cfg(tmp_path)
    write_index(cfg, [row("face.mp4", "pressure", faces="1")])
    video = {"script_blocks": VIDEO["script_blocks"][:1]}

    with caplog.at_level("WARNING"):
        shots, _ = build_timeline(video, [audio(0, 2.6)], cfg, TODAY, set())

    assert shots[0].snippet.name == "face.mp4"
    assert "keep a face off the first shot" in caplog.text
