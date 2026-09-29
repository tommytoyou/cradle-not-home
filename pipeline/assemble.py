"""Timeline building (T4a) and vertical render (T4b).

``build_timeline`` fills each block's narration with 2-4 s shots, hard cutting on
block boundaries and taking ``src_start`` from the middle of each snippet, and
emits captions chunked to 3-5 words across the block.

``render`` writes 1080x1920 H.264 + AAC at 30 fps using FFmpeg filter graphs
rather than MoviePy frame loops: centre crop to 9:16, optional slow zoom, dark
grade, source audio dropped, burned ``.ass`` captions kept clear of the Shorts UI
safe area, hook text over the first 1.5 s, and ducked music at about -14 LUFS.
It accepts any voice file so T11 can reuse it at 1920x1080 for long form.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from pipeline import snippets
from pipeline.config import Config
from pipeline.tts import DEFAULT_GAP_SEC, BlockAudio

MIN_SHOT_SEC = 2.0
MAX_SHOT_SEC = 4.0
TARGET_SHOT_SEC = 3.0

# Opening seconds of a NASA clip are often a slate, fade or camera settle.
SKIP_HEAD_SEC = 1.0

MIN_CAPTION_WORDS = 3
MAX_CAPTION_WORDS = 5
TARGET_CAPTION_WORDS = 4

# Stills are pre-rendered Ken Burns clips; they read as frozen without motion.
ALWAYS_ZOOM_FOLDERS = frozenset({"12_still_kb"})

# Extra candidates requested when the opening clip shows a face.
FACE_HEADROOM_CLIPS = 3

log = logging.getLogger(__name__)

_WORD_EDGE_RE = re.compile(r"^\W+|\W+$")


class TimelineError(RuntimeError):
    """Raised when blocks and audio disagree or a clip cannot supply its shot."""


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
    emphasis: list[str]


# --- shots -----------------------------------------------------------------


def shot_count(span_sec: float) -> int:
    """How many 2-4 s shots fill ``span_sec``, aiming for about 3 s each.

    A span under 2 s gets a single short shot; there is no way to honour the
    minimum without crossing the block boundary.
    """
    fewest = max(1, math.ceil(span_sec / MAX_SHOT_SEC))
    most = max(1, math.floor(span_sec / MIN_SHOT_SEC))
    return min(max(round(span_sec / TARGET_SHOT_SEC), fewest), most)


def source_start(clip_sec: float, shot_sec: float) -> float:
    """Centre the shot in the clip after skipping its first second."""
    if shot_sec > clip_sec:
        raise TimelineError(f"a {shot_sec:.2f}s shot does not fit a {clip_sec:.2f}s clip")
    usable = clip_sec - SKIP_HEAD_SEC
    if shot_sec <= usable:
        return round(SKIP_HEAD_SEC + (usable - shot_sec) / 2, 3)
    # Too short to skip the head in full; take the exact middle instead.
    return round((clip_sec - shot_sec) / 2, 3)


def block_spans(blocks: list[BlockAudio], gap_sec: float) -> list[tuple[float, float]]:
    """(start, picture length) per block on the concatenated voice track.

    Mirrors :func:`pipeline.tts.concat_voice`: the gap after a block belongs to
    that block's picture, so each cut lands as the next line starts.
    """
    spans = []
    cursor = 0.0
    for position, block in enumerate(blocks):
        tail = gap_sec if position < len(blocks) - 1 else 0.0
        spans.append((cursor, block.duration_sec + tail))
        cursor += block.duration_sec + tail
    return spans


# --- captions --------------------------------------------------------------


def _bare(word: str) -> str:
    return _WORD_EDGE_RE.sub("", word).lower()


def chunk_words(words: list[str]) -> list[list[str]]:
    """Split into 3-5 word chunks as evenly as possible, keeping order."""
    total = len(words)
    if total <= MAX_CAPTION_WORDS:
        return [words] if words else []

    fewest = math.ceil(total / MAX_CAPTION_WORDS)
    most = total // MIN_CAPTION_WORDS
    count = min(max(round(total / TARGET_CAPTION_WORDS), fewest), most)

    base, extra = divmod(total, count)
    chunks, cursor = [], 0
    for position in range(count):
        size = base + (1 if position < extra else 0)
        chunks.append(words[cursor:cursor + size])
        cursor += size
    return chunks


def block_captions(block: dict, start: float, duration: float) -> list[Caption]:
    """Chunk one block's text and spread it across its spoken audio.

    Time is shared by character count, a closer proxy for speech than words.
    """
    chunks = chunk_words(block["text"].split())
    if not chunks:
        return []

    wanted = {_bare(word): word for word in block.get("emphasis", [])}
    weights = [sum(len(word) for word in chunk) + len(chunk) for chunk in chunks]
    total = sum(weights)

    captions, cursor = [], start
    for chunk, weight in zip(chunks, weights):
        end = cursor + duration * weight / total
        hits = [wanted[_bare(word)] for word in chunk if _bare(word) in wanted]
        captions.append(
            Caption(
                start=round(cursor, 3),
                end=round(end, 3),
                text=" ".join(chunk),
                emphasis=list(dict.fromkeys(hits)),
            )
        )
        cursor = end
    captions[-1].end = round(start + duration, 3)
    return captions


# --- the timeline ----------------------------------------------------------


def _face_free_opening(
    moods: list[str],
    span: float,
    used_today: set[str],
    cfg: Config,
    today: date,
    picked: list[snippets.Snippet],
) -> list[snippets.Snippet]:
    """Re-pick the first block with spare candidates so a face can move back.

    The picker only reorders what it chose; a short first block may get a single
    clip, and if that clip shows a face there is nothing to swap it with.
    """
    try:
        wider = snippets.pick(
            moods, span + FACE_HEADROOM_CLIPS * snippets.AVERAGE_SHOT_SEC,
            used_today, cfg, today,
        )
    except snippets.SnippetError:
        log.warning("no spare clips to keep a face off the first shot")
        return picked
    return wider


def build_timeline(
    video: dict,
    blocks: list[BlockAudio],
    cfg: Config,
    today: date,
    used_today: set[str],
    gap_sec: float = DEFAULT_GAP_SEC,
) -> tuple[list[Shot], list[Caption]]:
    """Shots and captions for one video, cut to its block audio.

    ``used_today`` is updated in place with every clip this video takes, so the
    caller passes the same set to the second video of the day and later hands it
    to :func:`pipeline.snippets.mark_used`. ``gap_sec`` must match the value
    given to :func:`pipeline.tts.concat_voice`.
    """
    script = video["script_blocks"]
    ordered = sorted(blocks, key=lambda block: block.index)
    if [block.index for block in ordered] != list(range(len(script))):
        raise TimelineError(
            f"{len(script)} script blocks but audio for indexes "
            f"{[block.index for block in ordered]}"
        )

    shots: list[Shot] = []
    captions: list[Caption] = []

    for block, audio, (start, span) in zip(script, ordered, block_spans(ordered, gap_sec)):
        count = shot_count(span)
        picked = snippets.pick(block["moods"], span, used_today, cfg, today)
        if not shots and picked[0].faces:
            picked = _face_free_opening(block["moods"], span, used_today, cfg, today, picked)
        if len(picked) < count:
            raise TimelineError(
                f"block {audio.index} needs {count} clips, picker gave {len(picked)}"
            )

        # Cut points rounded on the track, so shot lengths add up with no drift.
        cuts = [round(start + span * i / count, 3) for i in range(count + 1)]
        for position, snippet in enumerate(picked[:count]):
            length = round(cuts[position + 1] - cuts[position], 3)
            shots.append(
                Shot(
                    snippet=snippet.full_path,
                    src_start=source_start(snippet.duration_sec, length),
                    duration=length,
                    zoom=snippet.folder in ALWAYS_ZOOM_FOLDERS or len(shots) % 2 == 1,
                )
            )
            used_today.add(snippet.path)

        captions.extend(block_captions(block, start, audio.duration_sec))

    return shots, captions
