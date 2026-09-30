"""T4a timeline and T4b render.

The picker runs for real against a CSV index in ``tmp_path``. Render tests use
generated fixture clips and silence; no network, and the FFmpeg ones skip when
FFmpeg is not on PATH.
"""

import csv
import json
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from pipeline.assemble import (
    ACCENT,
    FPS,
    HEIGHT,
    HOOK_SEC,
    MAX_SHOT_SEC,
    MIN_SHOT_SEC,
    SAFE_BOTTOM,
    SAFE_RIGHT,
    SKIP_HEAD_SEC,
    WIDTH,
    Caption,
    RenderError,
    Shot,
    TimelineError,
    ass_time,
    block_captions,
    block_spans,
    build_ass,
    build_timeline,
    caption_markup,
    chunk_words,
    final_graph,
    font_family,
    frame_counts,
    pick_music,
    render,
    shot_count,
    shot_filter,
    source_start,
)
from pipeline.config import ROOT, Config
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


# --- render: pure pieces ---------------------------------------------------

ANTON = ROOT / "assets" / "fonts" / "Anton-Regular.ttf"
HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="FFmpeg not on PATH")


def test_frame_counts_do_not_drift_over_a_long_video():
    shots = [Shot(Path("x.mp4"), 1.0, 2.95, False) for _ in range(70)]

    counts = frame_counts(shots)

    assert sum(counts) == round(70 * 2.95 * FPS)
    assert set(counts) <= {88, 89}


def test_frame_counts_follow_the_timeline_cut_points():
    shots = [Shot(Path("x.mp4"), 1.0, d, False) for d in (2.017, 3.333, 2.65)]

    edges, total = [], 0
    for count in frame_counts(shots):
        total += count
        edges.append(total)

    assert edges == [round(2.017 * FPS), round(5.35 * FPS), round(8.0 * FPS)]


def test_shot_filter_crops_to_vertical_and_zooms_only_when_asked():
    still = shot_filter(90, zoom=False)
    moving = shot_filter(90, zoom=True)

    for graph in (still, moving):
        assert f"crop={WIDTH}:{HEIGHT}" in graph
        assert "force_original_aspect_ratio=increase" in graph
    assert "zoompan" not in still
    assert "zoompan" in moving and f"s={WIDTH}x{HEIGHT}" in moving


def test_final_graph_takes_audio_only_from_voice_and_music():
    graph = final_graph(30.0)

    assert "[0:a]" not in graph  # the joined shots carry no audio at all
    assert "[1:a]" in graph and "[2:a]" in graph
    assert "sidechaincompress" in graph
    assert "loudnorm=I=-14" in graph


@pytest.mark.parametrize(
    "seconds, text",
    [(0, "0:00:00.00"), (1.5, "0:00:01.50"), (61.234, "0:01:01.23"), (3725.5, "1:02:05.50")],
)
def test_ass_time(seconds, text):
    assert ass_time(seconds) == text


def style_fields(ass: str, name: str) -> dict[str, str]:
    header = next(line for line in ass.splitlines() if line.startswith("Format: Name"))
    keys = [key.strip() for key in header.removeprefix("Format:").split(",")]
    line = next(line for line in ass.splitlines() if line.startswith(f"Style: {name},"))
    return dict(zip(keys, (value.strip() for value in line.removeprefix("Style:").split(","))))


def test_caption_style_keeps_clear_of_the_shorts_ui():
    style = style_fields(build_ass([], "", "Anton"), "Caption")

    assert style["Fontname"] == "Anton"
    assert style["Alignment"] == "2"  # bottom centre, grows upward
    assert int(style["MarginV"]) >= SAFE_BOTTOM
    assert int(style["MarginR"]) >= SAFE_RIGHT
    assert style["MarginL"] == style["MarginR"]  # centred on the frame


def test_hook_is_shown_for_the_first_second_and_a_half():
    ass = build_ass([], "Gravity is not the enemy", "Anton")

    hook = [line for line in ass.splitlines() if line.startswith("Dialogue") and ",Hook," in line]
    assert hook == [
        f"Dialogue: 1,0:00:00.00,{ass_time(HOOK_SEC)},Hook,,0,0,0,,GRAVITY IS NOT THE ENEMY"
    ]


def test_empty_hook_adds_no_event():
    assert ",Hook,,0" not in build_ass([], "  ", "Anton")


def test_captions_become_timed_dialogue_lines():
    ass = build_ass([Caption(1.2, 2.75, "Then let it throw you.", [])], "", "Anton")

    assert "Dialogue: 0,0:00:01.20,0:00:02.75,Caption,,0,0,0,,Then let it throw you." in ass


def test_emphasis_words_get_the_accent_colour():
    markup = caption_markup(Caption(0, 1, "using it to go further.", ["further"]))

    assert markup.startswith("using it to go ")
    assert f"{{\\c{ACCENT}&}}further.{{" in markup
    assert markup.count("\\c") == 2  # switched on, then back to white


def test_script_text_cannot_inject_ass_overrides():
    markup = caption_markup(Caption(0, 1, r"a {\b1} c\N", []))

    assert "{" not in markup and "\\" not in markup


def test_font_family_reads_the_committed_anton():
    assert font_family(ANTON) == "Anton"
    assert (ANTON.parent / "OFL.txt").is_file()


def test_font_family_rejects_a_non_font(tmp_path):
    fake = tmp_path / "font.ttf"
    fake.write_text("not a font")

    with pytest.raises(RenderError):
        font_family(fake)


def test_pick_music_ignores_non_audio_and_fails_closed_when_empty(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.music_dir.mkdir()
    (cfg.music_dir / "notes.txt").write_text("x")

    with pytest.raises(RenderError, match="no music"):
        pick_music(cfg)

    (cfg.music_dir / "bed.MP3").write_bytes(b"")
    assert pick_music(cfg).name == "bed.MP3"


def test_pick_music_fails_closed_without_a_folder(tmp_path):
    with pytest.raises(RenderError, match="no music"):
        pick_music(make_cfg(tmp_path))


def test_render_checks_its_inputs_before_running_ffmpeg(tmp_path):
    cfg = make_cfg(tmp_path, font_path=ANTON)
    voice = tmp_path / "voice.wav"
    shot = Shot(tmp_path / "clip.mp4", 1.0, 3.0, False)

    with pytest.raises(RenderError, match="no shots"):
        render([], [], voice, "", cfg, tmp_path / "out.mp4")
    with pytest.raises(RenderError, match="voice file missing"):
        render([shot], [], voice, "", cfg, tmp_path / "out.mp4")

    voice.write_bytes(b"")
    with pytest.raises(RenderError, match="font missing"):
        render([shot], [], voice, "", make_cfg(tmp_path), tmp_path / "out.mp4")
    with pytest.raises(RenderError, match="no music"):
        render([shot], [], voice, "", cfg, tmp_path / "out.mp4")


# --- render: a real 30 s video from fixture clips ----------------------------

RENDER_SEC = 30.0
# Wide glyphs, long enough to wrap, so the widest and tallest case is measured.
LONG_LINE = "WWWWW MMMMMMM WWWWWWW MMMMMM WWWWW"


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def probe(path: Path) -> dict:
    output = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(output)


def gray_frame(path: Path, at: float) -> bytes:
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{at}", "-i", str(path), "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True, check=True,
    ).stdout


def bright_box(frame: bytes, threshold: int = 128) -> tuple[int, int, int, int] | None:
    """(left, top, right, bottom) of pixels above ``threshold``, or None."""
    table = bytes(1 if value > threshold else 0 for value in range(256))
    left, top, right, bottom = WIDTH, None, -1, -1
    for y in range(HEIGHT):
        line = frame[y * WIDTH:(y + 1) * WIDTH].translate(table)
        first = line.find(b"\x01")
        if first < 0:
            continue
        top = y if top is None else top
        bottom = y
        left = min(left, first)
        right = max(right, line.rfind(b"\x01"))
    return None if top is None else (left, top, right, bottom)


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    """Black 16:9 clips carrying a loud tone, rendered over silent voice and music.

    Black picture means anything bright in the output is caption or hook text;
    silent voice and music mean anything audible leaked from the clips.
    """
    if not HAS_FFMPEG:
        pytest.skip("FFmpeg not on PATH")
    root = tmp_path_factory.mktemp("render")
    cfg = make_cfg(root, font_path=ANTON)
    cfg.music_dir.mkdir()

    clips = []
    for i in range(3):
        clip = root / f"clip{i}.mp4"
        ffmpeg("-f", "lavfi", "-i", "color=c=black:s=1920x1080:r=25:d=8",
               "-f", "lavfi", "-i", "sine=f=1000:d=8:sample_rate=48000",
               "-af", "volume=0.9", "-c:v", "libx264", "-preset", "ultrafast",
               "-c:a", "aac", "-shortest", str(clip))
        clips.append(clip)
    voice = root / "voice.wav"
    ffmpeg("-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "28", str(voice))
    ffmpeg("-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "7",
           str(cfg.music_dir / "bed.wav"))

    shots = [Shot(clips[i % 3], 2.0, 3.0, i % 2 == 1) for i in range(10)]
    captions = [
        Caption(2.0 + 2.5 * i, 4.5 + 2.5 * i, LONG_LINE, ["MMMMMMM"]) for i in range(11)
    ]
    return render(shots, captions, voice, "Gravity is not the enemy", cfg,
                  root / "output" / "punch.mp4")


@needs_ffmpeg
def test_render_is_vertical_h264_aac_at_thirty_fps(rendered):
    info = probe(rendered)
    video = [s for s in info["streams"] if s["codec_type"] == "video"]
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]

    assert len(video) == 1 and len(audio) == 1
    assert video[0]["codec_name"] == "h264"
    assert (video[0]["width"], video[0]["height"]) == (WIDTH, HEIGHT)
    assert video[0]["r_frame_rate"] == f"{FPS}/1"
    assert video[0]["pix_fmt"] == "yuv420p"
    assert audio[0]["codec_name"] == "aac"
    assert float(info["format"]["duration"]) == pytest.approx(RENDER_SEC, abs=0.1)


@needs_ffmpeg
def test_render_plays_end_to_end(rendered):
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(rendered), "-f", "null", "-"],
        capture_output=True, text=True,
    )

    assert result.returncode == 0
    assert result.stderr.strip() == ""


@needs_ffmpeg
def test_render_has_no_source_audio(rendered):
    result = subprocess.run(
        ["ffmpeg", "-i", str(rendered), "-af", "volumedetect", "-vn", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    )
    peak = float(re.search(r"max_volume: (-?[\d.]+|-inf) dB", result.stderr).group(1))

    assert peak < -60


@needs_ffmpeg
def test_render_burns_the_hook_over_the_opening(rendered):
    box = bright_box(gray_frame(rendered, 0.5))

    assert box is not None
    _, top, _, bottom = box
    assert top < HEIGHT / 2 < bottom
    assert bright_box(gray_frame(rendered, HOOK_SEC + 0.2)) is None  # before captions start


@needs_ffmpeg
@pytest.mark.parametrize("at", [3.0, 10.0, 17.0, 26.0])
def test_captions_stay_inside_the_safe_area(rendered, at):
    box = bright_box(gray_frame(rendered, at))

    assert box is not None, "caption missing"
    left, top, right, bottom = box
    assert bottom < HEIGHT - SAFE_BOTTOM
    assert right < WIDTH - SAFE_RIGHT
    assert WIDTH - right == pytest.approx(left, abs=40)  # centred
    assert top > HEIGHT / 2  # lower part of the frame, even when wrapped
