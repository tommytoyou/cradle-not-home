"""T4a: build_timeline shots and captions. Picker is mocked; no FFmpeg."""

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline.assemble import (
    Caption,
    Shot,
    TimelineError,
    build_timeline,
    chunk_words,
    split_shot_durations,
    src_start_for,
)
from pipeline.config import Config
from pipeline.snippets import Snippet
from pipeline.tts import DEFAULT_GAP_SEC, BlockAudio

TODAY = date(2026, 9, 30)


def make_cfg(tmp_path: Path) -> Config:
    return Config(
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


def snip(cfg: Config, name: str, duration: float = 8.0, moods=("awe",)) -> Snippet:
    rel = f"01_launch/{name}"
    return Snippet(
        path=rel,
        folder="01_launch",
        moods=moods,
        duration_sec=duration,
        width=1920,
        height=1080,
        nasa_id="",
        credit="",
        faces=False,
        logo_risk=False,
        last_used=None,
        full_path=Path(cfg.library_dir) / rel,
    )


def test_split_short_block_is_one_shot():
    assert split_shot_durations(1.5) == [1.5]
    assert split_shot_durations(4.0) == [4.0]


def test_split_long_block_stays_in_2_to_4():
    parts = split_shot_durations(9.0)
    assert abs(sum(parts) - 9.0) < 1e-9
    assert all(2.0 <= p <= 4.0 for p in parts)
    assert len(parts) == 3


def test_chunk_words_keeps_short_lines():
    assert chunk_words("Leave the well.") == ["Leave the well."]


def test_chunk_words_breaks_long_lines():
    text = "You were born in a cradle and you called it a home"
    chunks = chunk_words(text)
    assert all(1 <= len(c.split()) <= 5 for c in chunks)
    assert " ".join(chunks) == text


def test_src_start_skips_first_second_and_uses_middle():
    s = Snippet(
        path="a.mp4", folder="01_launch", moods=("awe",),
        duration_sec=8.0, width=1920, height=1080,
        nasa_id="", credit="", faces=False, logo_risk=False,
        last_used=None, full_path=Path("a.mp4"),
    )
    assert src_start_for(s, 3.0) == pytest.approx(3.0)


def test_build_timeline_hard_cuts_on_blocks_and_adds_gap(tmp_path):
    cfg = make_cfg(tmp_path)
    queue = [snip(cfg, f"c{i}.mp4") for i in range(6)]

    def fake_pick(moods, seconds, used_today, cfg, today):
        n = max(1, round(seconds / 3))
        taken = queue[:n]
        del queue[:n]
        return taken

    video = {
        "script_blocks": [
            {"text": "You were born in a cradle.", "moods": ["heritage"], "emphasis": ["cradle"]},
            {"text": "Leave the well now.", "moods": ["commitment"], "emphasis": ["Leave"]},
        ]
    }
    blocks = [
        BlockAudio(index=0, path=tmp_path / "a.mp3", duration_sec=6.0),
        BlockAudio(index=1, path=tmp_path / "b.mp3", duration_sec=3.0),
    ]

    with patch("pipeline.assemble.pick", side_effect=fake_pick):
        used: set[str] = set()
        shots, captions = build_timeline(video, blocks, cfg, TODAY, used)

    assert all(isinstance(s, Shot) for s in shots)
    assert all(isinstance(c, Caption) for c in captions)
    assert sum(s.duration for s in shots) == pytest.approx(6.0 + 3.0 + DEFAULT_GAP_SEC)
    assert captions[0].start == pytest.approx(0.0)
    assert any("cradle" in c.emphasis for c in captions)
    assert shots[0].zoom is True
    assert used


def test_used_today_is_mutated(tmp_path):
    cfg = make_cfg(tmp_path)
    clip = snip(cfg, "only.mp4")

    video = {"script_blocks": [{"text": "Go.", "moods": ["awe"], "emphasis": []}]}
    blocks = [BlockAudio(index=0, path=tmp_path / "a.mp3", duration_sec=2.0)]
    used: set[str] = set()
    with patch("pipeline.assemble.pick", return_value=[clip]):
        build_timeline(video, blocks, cfg, TODAY, used)
    assert clip.path in used


def test_mismatched_block_counts_fail(tmp_path):
    cfg = make_cfg(tmp_path)
    with pytest.raises(TimelineError, match="script has"):
        build_timeline(
            {"script_blocks": []},
            [BlockAudio(index=0, path=tmp_path / "a.mp3", duration_sec=1.0)],
            cfg,
            TODAY,
            set(),
        )
