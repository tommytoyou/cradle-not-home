"""T2: per-block synthesis, the sha256 cache, and gapped concatenation.

ElevenLabs is mocked everywhere — no test makes a network call. The ffmpeg and
ffprobe calls are mocked too, so the suite runs without FFmpeg installed; the
tests at the bottom exercise the real binaries and skip when they are absent.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from pipeline import tts
from pipeline.config import Config
from pipeline.tts import (
    BlockAudio,
    TTSError,
    cache_key,
    concat_voice,
    probe_duration_sec,
    synth_blocks,
)

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="FFmpeg not on PATH")

VIDEO = {
    "format": "punch",
    "script_blocks": [
        {"text": "You think the pressure is your problem.", "moods": ["pressure"], "emphasis": []},
        {"text": "Not by resisting their pull. By using it.", "moods": ["reveal"], "emphasis": []},
        {"text": "Then let it throw you further.", "moods": ["leaving"], "emphasis": []},
    ],
}


def make_cfg(**overrides) -> Config:
    base = dict(
        llm_api_key="key",
        llm_base_url="https://example.invalid",
        llm_model="claude-opus-5",
        elevenlabs_api_key="eleven-key",
        elevenlabs_voice_id="voice-abc",
        youtube_client_secrets=Path("client_secret.json"),
        youtube_token=Path("token.json"),
        library_dir=Path("library"),
        music_dir=Path("music"),
        output_dir=Path("output"),
        data_dir=Path("data"),
        font_path=Path("font.ttf"),
    )
    return Config(**(base | overrides))


@pytest.fixture
def fake_vendor(monkeypatch):
    """Stand in for ElevenLabs; records every call and returns fake audio."""
    calls = []

    def _synthesize(text, cfg):
        calls.append({"text": text, "voice_id": cfg.elevenlabs_voice_id})
        return b"ID3fake-audio-" + text.encode("utf-8")[:8]

    monkeypatch.setattr(tts, "_synthesize", _synthesize)
    return calls


@pytest.fixture
def fake_duration(monkeypatch):
    """Every probed file is 2.0 s, without invoking ffprobe."""
    monkeypatch.setattr(tts, "probe_duration_sec", lambda path: 2.0)


# --- cache key -------------------------------------------------------------


def test_cache_key_is_stable_and_hex():
    key = cache_key("voice-abc", "one line")

    assert key == cache_key("voice-abc", "one line")
    assert len(key) == 64
    assert set(key) <= set("0123456789abcdef")


def test_cache_key_changes_with_text_and_with_voice():
    base = cache_key("voice-abc", "one line")

    assert cache_key("voice-abc", "another line") != base
    assert cache_key("voice-xyz", "one line") != base


# --- synth_blocks ----------------------------------------------------------


def test_one_request_per_block_in_order(tmp_path, fake_vendor, fake_duration):
    blocks = synth_blocks(VIDEO, make_cfg(), tmp_path)

    assert len(fake_vendor) == 3
    assert [call["text"] for call in fake_vendor] == [
        b["text"] for b in VIDEO["script_blocks"]
    ]
    assert [block.index for block in blocks] == [0, 1, 2]
    assert all(block.duration_sec == 2.0 for block in blocks)
    assert all(block.path.exists() for block in blocks)


def test_files_are_named_by_cache_key(tmp_path, fake_vendor, fake_duration):
    cfg = make_cfg()

    blocks = synth_blocks(VIDEO, cfg, tmp_path)

    for block, spec in zip(blocks, VIDEO["script_blocks"]):
        assert block.path.name == cache_key(cfg.elevenlabs_voice_id, spec["text"]) + ".mp3"


def test_second_run_reuses_the_cache(tmp_path, fake_vendor, fake_duration):
    cfg = make_cfg()
    first = synth_blocks(VIDEO, cfg, tmp_path)
    assert len(fake_vendor) == 3

    second = synth_blocks(VIDEO, cfg, tmp_path)

    assert len(fake_vendor) == 3  # no further synthesis
    assert [b.path for b in second] == [b.path for b in first]


def test_a_different_voice_does_not_hit_the_cache(tmp_path, fake_vendor, fake_duration):
    synth_blocks(VIDEO, make_cfg(), tmp_path)

    synth_blocks(VIDEO, make_cfg(elevenlabs_voice_id="voice-clone"), tmp_path)

    assert len(fake_vendor) == 6


def test_an_empty_cache_file_is_resynthesised(tmp_path, fake_vendor, fake_duration):
    cfg = make_cfg()
    blocks = synth_blocks(VIDEO, cfg, tmp_path)
    blocks[0].path.write_bytes(b"")

    synth_blocks(VIDEO, cfg, tmp_path)

    assert len(fake_vendor) == 4


def test_workdir_is_created(tmp_path, fake_vendor, fake_duration):
    workdir = tmp_path / "output" / "2026-09-28" / "punch"

    synth_blocks(VIDEO, make_cfg(), workdir)

    assert workdir.is_dir()


def test_empty_audio_is_an_error(tmp_path, monkeypatch, fake_duration):
    monkeypatch.setattr(tts, "_synthesize", lambda text, cfg: b"")

    with pytest.raises(TTSError, match="no audio"):
        synth_blocks(VIDEO, make_cfg(), tmp_path)


def test_no_part_files_are_left_behind(tmp_path, fake_vendor, fake_duration):
    synth_blocks(VIDEO, make_cfg(), tmp_path)

    assert list(tmp_path.glob("*.part")) == []


# --- the vendor call -------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code=200, content=b"audio", text=""):
        self.status_code = status_code
        self.content = content
        self.text = text


def test_vendor_call_uses_the_config_voice_and_key(monkeypatch):
    sent = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.update(url=url, headers=headers, json=json, timeout=timeout)
        return FakeResponse(content=b"mp3-bytes")

    monkeypatch.setattr(tts.requests, "post", fake_post)

    audio = tts._synthesize("a line", make_cfg())

    assert audio == b"mp3-bytes"
    assert sent["url"].endswith("/text-to-speech/voice-abc")
    assert sent["headers"]["xi-api-key"] == "eleven-key"
    assert sent["json"]["text"] == "a line"
    assert sent["timeout"] == tts.REQUEST_TIMEOUT_SEC


def test_vendor_error_status_raises(monkeypatch):
    monkeypatch.setattr(
        tts.requests,
        "post",
        lambda *a, **k: FakeResponse(status_code=401, content=b"", text="bad key"),
    )

    with pytest.raises(TTSError, match="401"):
        tts._synthesize("a line", make_cfg())


# --- ffprobe ---------------------------------------------------------------


def test_probe_duration_parses_ffprobe_json(monkeypatch):
    monkeypatch.setattr(tts, "_binary", lambda name: name)
    monkeypatch.setattr(tts, "_run", lambda cmd: '{"format": {"duration": "12.34"}}')

    assert probe_duration_sec(Path("x.mp3")) == pytest.approx(12.34)


def test_probe_duration_rejects_unparseable_output(monkeypatch):
    monkeypatch.setattr(tts, "_binary", lambda name: name)
    monkeypatch.setattr(tts, "_run", lambda cmd: "{}")

    with pytest.raises(TTSError, match="no duration"):
        probe_duration_sec(Path("x.mp3"))


def test_missing_binary_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(tts.shutil, "which", lambda name: None)

    with pytest.raises(TTSError, match="install FFmpeg"):
        tts._binary("ffprobe")


def test_run_raises_on_non_zero_exit(monkeypatch):
    def fake_run(cmd, capture_output, text):
        return subprocess.CompletedProcess(cmd, 1, "", "boom\nsecond line")

    monkeypatch.setattr(tts.subprocess, "run", fake_run)

    with pytest.raises(TTSError, match="second line"):
        tts._run(["ffmpeg", "-y"])


# --- concat_voice ----------------------------------------------------------


@pytest.fixture
def captured_concat(monkeypatch):
    """Capture the ffmpeg command instead of running it."""
    commands = []
    monkeypatch.setattr(tts, "_binary", lambda name: name)
    monkeypatch.setattr(tts, "_run", lambda cmd: commands.append(cmd) or "")
    monkeypatch.setattr(tts, "probe_duration_sec", lambda path: 7.5)
    return commands


def blocks_for(tmp_path, count=3):
    made = []
    for index in range(count):
        path = tmp_path / f"block{index}.mp3"
        path.write_bytes(b"audio")
        made.append(BlockAudio(index=index, path=path, duration_sec=2.0))
    return made


def test_concat_returns_the_probed_duration(tmp_path, captured_concat):
    total = concat_voice(blocks_for(tmp_path), tmp_path / "voice.wav")

    assert total == 7.5


def test_concat_inserts_a_gap_between_every_pair(tmp_path, captured_concat):
    concat_voice(blocks_for(tmp_path, 3), tmp_path / "voice.wav", gap_sec=0.35)

    cmd = captured_concat[0]
    assert cmd.count("anullsrc=r=44100:cl=mono") == 2  # 3 blocks -> 2 gaps
    assert cmd[cmd.index("-t") + 1] == "0.35"
    assert "concat=n=5:v=0:a=1[out]" in cmd[cmd.index("-filter_complex") + 1]


def test_concat_with_zero_gap_adds_no_silence(tmp_path, captured_concat):
    concat_voice(blocks_for(tmp_path, 3), tmp_path / "voice.wav", gap_sec=0)

    cmd = captured_concat[0]
    assert "anullsrc=r=44100:cl=mono" not in cmd
    assert "concat=n=3:v=0:a=1[out]" in cmd[cmd.index("-filter_complex") + 1]


def test_concat_uses_index_order_not_list_order(tmp_path, captured_concat):
    blocks = blocks_for(tmp_path, 3)
    shuffled = [blocks[2], blocks[0], blocks[1]]

    concat_voice(shuffled, tmp_path / "voice.wav")

    cmd = captured_concat[0]
    inputs = [cmd[i + 1] for i, part in enumerate(cmd) if part == "-i"]
    mp3s = [Path(p).name for p in inputs if p.endswith(".mp3")]
    assert mp3s == ["block0.mp3", "block1.mp3", "block2.mp3"]


def test_concat_creates_the_output_parent(tmp_path, captured_concat):
    out = tmp_path / "output" / "2026-09-28" / "voice.wav"

    concat_voice(blocks_for(tmp_path), out)

    assert out.parent.is_dir()


def test_concat_rejects_an_empty_block_list(tmp_path):
    with pytest.raises(TTSError, match="no blocks"):
        concat_voice([], tmp_path / "voice.wav")


# --- real FFmpeg -----------------------------------------------------------


@needs_ffmpeg
def test_probe_duration_against_a_real_file(tmp_path):
    path = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-t", "1.5",
         "-i", "sine=frequency=440:sample_rate=44100", "-ac", "1", str(path)],
        capture_output=True,
        check=True,
    )

    assert probe_duration_sec(path) == pytest.approx(1.5, abs=0.05)


@needs_ffmpeg
def test_concat_of_real_tones_has_the_gaps_in_it(tmp_path, monkeypatch):
    """Three 1 s tones with 0.35 s gaps should land near 3.7 s."""
    def fake_synthesize(text, cfg):
        source = tmp_path / "src.mp3"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-t", "1.0",
             "-i", "sine=frequency=440:sample_rate=44100", "-ac", "1", str(source)],
            capture_output=True,
            check=True,
        )
        return source.read_bytes()

    monkeypatch.setattr(tts, "_synthesize", fake_synthesize)

    blocks = synth_blocks(VIDEO, make_cfg(), tmp_path / "work")
    total = concat_voice(blocks, tmp_path / "voice.wav", gap_sec=0.35)

    assert len(blocks) == 3
    assert total == pytest.approx(3 * 1.0 + 2 * 0.35, abs=0.15)
    assert (tmp_path / "voice.wav").stat().st_size > 0
