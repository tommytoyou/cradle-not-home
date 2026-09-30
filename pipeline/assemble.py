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
import random
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from PIL import ImageFont

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


# --- render ----------------------------------------------------------------

WIDTH, HEIGHT = 1080, 1920
FPS = 30

# Shorts UI covers the bottom 20% (title, channel) and right 15% (buttons).
SAFE_BOTTOM = round(HEIGHT * 0.20)
SAFE_RIGHT = round(WIDTH * 0.15)
CAPTION_GAP = 60
CAPTION_SIZE = 110
HOOK_SIZE = 140
HOOK_SEC = 1.5

# ASS colours are &HAABBGGRR.
WHITE = "&H00FFFFFF"
BLACK = "&H00000000"
ACCENT = "&H0000C8FF"  # warm amber

ZOOM_END = 1.08
# zoompan snaps its crop to whole pixels; working at 2x hides the shimmer.
ZOOM_OVERSAMPLE = 2

# Contrast up, blacks crushed, slight desaturation, then moving grain.
GRADE = (
    "eq=contrast=1.15:brightness=-0.03:saturation=0.8,"
    "curves=all='0/0 0.12/0.02 0.5/0.46 1/0.97',"
    "noise=alls=7:allf=t"
)

MUSIC_SUFFIXES = frozenset({".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac"})
MUSIC_LEVEL = 0.35
TARGET_LUFS = -14
AUDIO_RATE = 48000


class RenderError(RuntimeError):
    """Raised for missing inputs, a missing binary, or a failed FFmpeg call."""


def _ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found is None:
        raise RenderError("ffmpeg not found on PATH; install FFmpeg")
    return found


def _run(cmd: list[str], cwd: Path) -> None:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-3:]
        raise RenderError("ffmpeg failed: " + " / ".join(tail))


def frame_counts(shots: list[Shot], fps: int = FPS) -> list[int]:
    """Frames per shot, rounded on the running total so the picture never drifts
    from the voice track however many shots there are."""
    counts, cursor, placed = [], 0.0, 0
    for shot in shots:
        cursor += shot.duration
        edge = round(cursor * fps)
        counts.append(edge - placed)
        placed = edge
    return counts


def shot_filter(frames: int, zoom: bool) -> str:
    """Centre crop to 9:16, optional slow push in, fixed length in frames."""
    stages = [
        f"fps={FPS}",
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase",
        f"crop={WIDTH}:{HEIGHT}",
    ]
    if zoom:
        step = (ZOOM_END - 1) / max(frames - 1, 1)
        stages += [
            f"scale={WIDTH * ZOOM_OVERSAMPLE}:{HEIGHT * ZOOM_OVERSAMPLE}",
            f"zoompan=z='1+{step:.6f}*on':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2'"
            f":d=1:s={WIDTH}x{HEIGHT}:fps={FPS}",
        ]
    # A clip that ends early holds its last frame rather than shortening the cut.
    stages += ["tpad=stop_mode=clone:stop_duration=1", "setsar=1", "format=yuv420p"]
    return ",".join(stages)


def _shot_command(shot: Shot, frames: int, out: Path) -> list[str]:
    return [
        _ffmpeg(), "-y", "-v", "error",
        "-ss", f"{shot.src_start:.3f}",
        "-i", str(shot.snippet),
        "-vf", shot_filter(frames, shot.zoom),
        "-frames:v", str(frames),
        "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "14",
        str(out),
    ]


# --- captions file ---------------------------------------------------------


def ass_time(seconds: float) -> str:
    centis = max(0, round(seconds * 100))
    hours, centis = divmod(centis, 360000)
    minutes, centis = divmod(centis, 6000)
    secs, centis = divmod(centis, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def _ass_text(text: str) -> str:
    """Neutralise ASS override syntax in script text."""
    return " ".join(text.replace("\\", "/").replace("{", "(").replace("}", ")").split())


def caption_markup(caption: Caption) -> str:
    """Caption text with its emphasis words switched to the accent colour."""
    wanted = {_bare(word) for word in caption.emphasis}
    words = []
    for word in caption.text.split():
        safe = _ass_text(word)
        if _bare(word) in wanted:
            safe = f"{{\\c{ACCENT}&}}{safe}{{\\c{WHITE}&}}"
        words.append(safe)
    return " ".join(words)


def font_family(font_path: Path) -> str:
    """Family name libass matches the ``Fontname`` style field against."""
    try:
        return ImageFont.truetype(str(font_path), 10).getname()[0]
    except OSError as exc:
        raise RenderError(f"cannot read font {font_path}") from exc


def build_ass(captions: list[Caption], hook_text: str, font_name: str) -> str:
    """Script for libass on a 1080x1920 canvas.

    Captions sit bottom centre with their lowest line just above the bottom 20%,
    growing upward when they wrap, with side margins wide enough to keep clear
    of the right 15%. The hook sits mid frame over the opening.
    """
    side = SAFE_RIGHT + 10
    style = (
        "Style: {name},{font},{size},{white},{white},{black},&H80000000,"
        "0,0,0,0,100,100,0,0,1,{outline},4,{align},{side},{side},{margin},1"
    )
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {WIDTH}",
        f"PlayResY: {HEIGHT}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        style.format(name="Caption", font=font_name, size=CAPTION_SIZE, white=WHITE,
                     black=BLACK, outline=6, align=2, side=side,
                     margin=SAFE_BOTTOM + CAPTION_GAP),
        style.format(name="Hook", font=font_name, size=HOOK_SIZE, white=WHITE,
                     black=BLACK, outline=8, align=5, side=side, margin=0),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if hook_text.strip():
        lines.append(
            f"Dialogue: 1,{ass_time(0)},{ass_time(HOOK_SEC)},Hook,,0,0,0,,"
            f"{_ass_text(hook_text).upper()}"
        )
    for caption in captions:
        lines.append(
            f"Dialogue: 0,{ass_time(caption.start)},{ass_time(caption.end)},Caption,,0,0,0,,"
            f"{caption_markup(caption)}"
        )
    return "\n".join(lines) + "\n"


# --- final pass ------------------------------------------------------------


def pick_music(cfg: Config) -> Path:
    """A random track from ``music/``; none at all is a hard stop."""
    tracks = []
    if cfg.music_dir.is_dir():
        tracks = sorted(
            path for path in cfg.music_dir.iterdir()
            if path.is_file() and path.suffix.lower() in MUSIC_SUFFIXES
        )
    if not tracks:
        raise RenderError(f"no music tracks in {cfg.music_dir}")
    return random.choice(tracks)


def final_graph(seconds: float) -> str:
    """Grade and captions on the joined shots; voice over ducked, looped music."""
    length = f"{seconds:.3f}"
    stereo = f"aformat=sample_rates={AUDIO_RATE}:channel_layouts=stereo"
    return ";".join([
        f"[0:v]{GRADE},subtitles=captions.ass:fontsdir=fonts,format=yuv420p[v]",
        f"[1:a]{stereo},apad,atrim=0:{length},asplit=2[voice][key]",
        f"[2:a]{stereo},atrim=0:{length},volume={MUSIC_LEVEL}[bed]",
        "[bed][key]sidechaincompress=threshold=0.02:ratio=8:attack=20:release=400[ducked]",
        "[voice][ducked]amix=inputs=2:duration=first:normalize=0,"
        f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11,aresample={AUDIO_RATE}[a]",
    ])


def _final_command(voice: Path, music: Path, seconds: float, out: Path) -> list[str]:
    return [
        _ffmpeg(), "-y", "-v", "error",
        "-f", "concat", "-safe", "0", "-i", "shots.txt",
        "-i", str(voice),
        "-stream_loop", "-1", "-i", str(music),
        "-filter_complex", final_graph(seconds),
        "-map", "[v]", "-map", "[a]",
        "-t", f"{seconds:.3f}",
        "-r", str(FPS),
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", str(AUDIO_RATE),
        "-movflags", "+faststart",
        str(out),
    ]


def render(
    shots: list[Shot],
    captions: list[Caption],
    voice: Path,
    hook_text: str,
    cfg: Config,
    out: Path,
) -> Path:
    """Render one vertical video to ``out`` and return it.

    Each shot is cut to an exact frame count in its own pass, then one filter
    graph joins them, grades, burns captions and mixes the audio. Only voice and
    music reach the soundtrack; every clip's own audio is dropped.
    """
    if not shots:
        raise RenderError("no shots to render")
    voice = Path(voice).resolve()
    if not voice.is_file():
        raise RenderError(f"voice file missing: {voice}")
    font = Path(cfg.font_path)
    if not font.is_file():
        raise RenderError(f"font missing: {font} (set FONT_PATH)")
    music = pick_music(cfg).resolve()

    out = Path(out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    counts = frame_counts(shots)
    seconds = sum(counts) / FPS

    with tempfile.TemporaryDirectory(prefix=f"{out.stem}_", dir=out.parent) as tmp:
        work = Path(tmp)
        # Paths inside filter arguments need FFmpeg escaping, and a Windows drive
        # colon breaks them; relative names in the work directory avoid both.
        fonts = work / "fonts"
        fonts.mkdir()
        shutil.copy2(font, fonts / font.name)
        (work / "captions.ass").write_text(
            build_ass(captions, hook_text, font_family(font)), encoding="utf-8"
        )

        listing = []
        for position, (shot, frames) in enumerate(zip(shots, counts)):
            if frames <= 0:
                log.warning("shot %d rounds to no frames; dropped", position)
                continue
            name = f"shot_{position:03d}.mp4"
            _run(_shot_command(shot, frames, work / name), work)
            listing.append(f"file '{name}'")
        (work / "shots.txt").write_text("\n".join(listing) + "\n", encoding="utf-8")

        _run(_final_command(voice, music, seconds, out), work)

    log.info("rendered %s (%.2fs, %d shots, music %s)", out, seconds, len(shots), music.name)
    return out
