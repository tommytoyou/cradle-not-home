"""ElevenLabs narration, one request per script block (T2).

Per-block synthesis keeps picture cuts aligned to spoken lines. Results are
cached by sha256 of voice id plus text, durations come from ``ffprobe``, and the
blocks are concatenated with a small gap into one voice track. Voice id comes
from config only, so switching to Tom's clone is an env var change.

The cache is the block filenames themselves: a block whose audio already sits in
``workdir`` is not re-synthesised, so re-running a day after a later step failed
costs nothing.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import requests

from pipeline.config import Config

ELEVENLABS_BASE_URL = "https://api.elevenlabs.io/v1"
ELEVENLABS_MODEL = "eleven_multilingual_v2"
REQUEST_TIMEOUT_SEC = 120

AUDIO_SUFFIX = ".mp3"
SAMPLE_RATE = 44100
DEFAULT_GAP_SEC = 0.35


class TTSError(RuntimeError):
    """Raised for a failed synthesis, a missing binary, or an unreadable file."""


@dataclass
class BlockAudio:
    index: int
    path: Path
    duration_sec: float


def cache_key(voice_id: str, text: str) -> str:
    """sha256 of voice id plus text — the block's cache identity."""
    return hashlib.sha256(f"{voice_id}\n{text}".encode("utf-8")).hexdigest()


def _binary(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise TTSError(f"{name} not found on PATH; install FFmpeg")
    return found


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-3:]
        raise TTSError(f"{Path(cmd[0]).name} failed: " + " / ".join(tail))
    return result.stdout


def probe_duration_sec(path: Path) -> float:
    """Duration of an audio file, via ffprobe."""
    output = _run(
        [
            _binary("ffprobe"),
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(path),
        ]
    )
    try:
        return float(json.loads(output)["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TTSError(f"ffprobe reported no duration for {path}") from exc


def _synthesize(text: str, cfg: Config) -> bytes:
    """The only function that talks to ElevenLabs."""
    response = requests.post(
        f"{ELEVENLABS_BASE_URL}/text-to-speech/{cfg.elevenlabs_voice_id}",
        headers={"xi-api-key": cfg.elevenlabs_api_key, "accept": "audio/mpeg"},
        json={"text": text, "model_id": ELEVENLABS_MODEL},
        timeout=REQUEST_TIMEOUT_SEC,
    )
    if response.status_code != 200:
        raise TTSError(
            f"ElevenLabs returned {response.status_code}: {response.text[:200]}"
        )
    return response.content


def synth_blocks(video: dict, cfg: Config, workdir: Path) -> list[BlockAudio]:
    """Synthesise one audio file per script block, in order."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    blocks: list[BlockAudio] = []
    for index, block in enumerate(video["script_blocks"]):
        text = block["text"]
        path = workdir / f"{cache_key(cfg.elevenlabs_voice_id, text)}{AUDIO_SUFFIX}"

        if not (path.exists() and path.stat().st_size > 0):
            audio = _synthesize(text, cfg)
            if not audio:
                raise TTSError(f"block {index} came back with no audio")
            # Write via a temp file so an interrupted run leaves no half cache entry.
            partial = path.with_name(path.name + ".part")
            partial.write_bytes(audio)
            partial.replace(path)

        blocks.append(
            BlockAudio(index=index, path=path, duration_sec=probe_duration_sec(path))
        )

    return blocks


def _concat_command(paths: list[Path], out: Path, gap_sec: float) -> list[str]:
    """Build the FFmpeg call: block, gap, block, gap, ... block."""
    cmd = [_binary("ffmpeg"), "-y"]
    inputs = 0

    for position, path in enumerate(paths):
        if position and gap_sec > 0:
            cmd += [
                "-f", "lavfi",
                "-t", f"{gap_sec}",
                "-i", f"anullsrc=r={SAMPLE_RATE}:cl=mono",
            ]
            inputs += 1
        cmd += ["-i", str(path)]
        inputs += 1

    # Normalise every input before concat; the filter needs matching formats.
    stages = [
        f"[{i}:a]aformat=sample_rates={SAMPLE_RATE}:channel_layouts=mono[a{i}]"
        for i in range(inputs)
    ]
    chain = "".join(f"[a{i}]" for i in range(inputs))
    graph = ";".join(stages) + f";{chain}concat=n={inputs}:v=0:a=1[out]"

    cmd += [
        "-filter_complex", graph,
        "-map", "[out]",
        "-ac", "1",
        "-ar", str(SAMPLE_RATE),
        str(out),
    ]
    return cmd


def concat_voice(
    blocks: list[BlockAudio],
    out: Path,
    gap_sec: float = DEFAULT_GAP_SEC,
) -> float:
    """Join the blocks in index order with ``gap_sec`` of silence between them."""
    if not blocks:
        raise TTSError("no blocks to concatenate")

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(blocks, key=lambda block: block.index)

    _run(_concat_command([block.path for block in ordered], out, gap_sec))
    return probe_duration_sec(out)
