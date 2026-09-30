"""Timeline building (T4a) and vertical render (T4b).

``build_timeline`` fills each block's narration with 2-4 s shots, hard cutting
on block boundaries and taking ``src_start`` from the middle of each snippet
(after skipping the first second), and emits captions chunked to 3-5 words
across the block.

``render`` is T4b and is not implemented here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from pipeline.config import Config
from pipeline.snippets import Snippet, pick
from pipeline.tts import DEFAULT_GAP_SEC, BlockAudio

MIN_SHOT_SEC = 2.0
MAX_SHOT_SEC = 4.0
TARGET_SHOT_SEC = 3.0
SKIP_HEAD_SEC = 1.0
MIN_CAPTION_WORDS = 3
MAX_CAPTION_WORDS = 5


class TimelineError(RuntimeError):
    """Raised when shots or captions cannot be built."""


@dataclass
class Shot:
    snippet: Path
    src_start: float
    duration: float
    zoom: bool


@dataclass
class Caption:
    start: float
    end: float
    text: str
    emphasis: list[str] = field(default_factory=list)


def split_shot_durations(total: float) -> list[float]:
    """Split ``total`` seconds into pieces in [2, 4], except a short last block."""
    if total <= 0:
        raise TimelineError("block duration must be positive")
    if total <= MAX_SHOT_SEC:
        return [total]

    n = max(1, round(total / TARGET_SHOT_SEC))
    while total / n > MAX_SHOT_SEC:
        n += 1
    while n > 1 and total / n < MIN_SHOT_SEC:
        n -= 1
    return [total / n] * n


def chunk_words(text: str) -> list[str]:
    """Split a line into 3-5 word caption chunks (last chunk may be shorter)."""
    words = text.split()
    if not words:
        return []
    if len(words) <= MAX_CAPTION_WORDS:
        return [" ".join(words)]

    chunks: list[str] = []
    i = 0
    while i < len(words):
        left = len(words) - i
        if left <= MAX_CAPTION_WORDS:
            chunks.append(" ".join(words[i:]))
            break
        take = MAX_CAPTION_WORDS if left - MAX_CAPTION_WORDS >= MIN_CAPTION_WORDS else MIN_CAPTION_WORDS
        chunks.append(" ".join(words[i : i + take]))
        i += take
    return chunks


def src_start_for(snippet: Snippet, shot_dur: float) -> float:
    """Skip the first second; take the window from the middle of what remains."""
    dur = snippet.duration_sec
    if shot_dur >= dur:
        return 0.0
    start = SKIP_HEAD_SEC
    if start + shot_dur > dur:
        start = max(0.0, dur - shot_dur)
    else:
        slack = dur - start - shot_dur
        start = start + slack / 2.0
    return round(start, 3)


def build_timeline(
    video: dict,
    blocks: list[BlockAudio],
    cfg: Config,
    today: date,
    used_today: set[str],
) -> tuple[list[Shot], list[Caption]]:
    """Map each voiced block onto 2-4 s NASA shots and timed caption chunks."""
    script = video.get("script_blocks") or []
    if len(script) != len(blocks):
        raise TimelineError(
            f"script has {len(script)} blocks but audio has {len(blocks)}"
        )

    ordered = sorted(blocks, key=lambda b: b.index)
    shots: list[Shot] = []
    captions: list[Caption] = []
    clock = 0.0
    zoom_toggle = True

    for i, audio in enumerate(ordered):
        block = script[audio.index]
        moods = list(block.get("moods") or [])
        emphasis_words = [w.lower() for w in (block.get("emphasis") or [])]
        durations = split_shot_durations(audio.duration_sec)
        picked = pick(moods, audio.duration_sec, used_today, cfg, today)
        if len(picked) < len(durations):
            raise TimelineError(
                f"block {audio.index}: need {len(durations)} shots, picker gave {len(picked)}"
            )

        for shot_i, shot_dur in enumerate(durations):
            snippet = picked[shot_i]
            used_today.add(snippet.path)
            shots.append(
                Shot(
                    snippet=snippet.full_path,
                    src_start=src_start_for(snippet, shot_dur),
                    duration=shot_dur,
                    zoom=zoom_toggle,
                )
            )
            zoom_toggle = not zoom_toggle

        chunks = chunk_words(block.get("text") or "")
        if chunks:
            slice_dur = audio.duration_sec / len(chunks)
            for ci, chunk in enumerate(chunks):
                words_in_chunk = chunk.split()
                cap_emph = [
                    w for w in words_in_chunk
                    if w.lower().strip(".,!?") in emphasis_words
                ]
                captions.append(
                    Caption(
                        start=round(clock + ci * slice_dur, 3),
                        end=round(clock + (ci + 1) * slice_dur, 3),
                        text=chunk,
                        emphasis=cap_emph,
                    )
                )

        clock += audio.duration_sec
        if i < len(ordered) - 1:
            shots[-1].duration += DEFAULT_GAP_SEC
            clock += DEFAULT_GAP_SEC

    return shots, captions


def render(*_args, **_kwargs):
    """T4b — not implemented in this ticket."""
    raise NotImplementedError("T4b render is a later ticket")
