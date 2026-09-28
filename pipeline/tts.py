"""ElevenLabs narration, one request per script block (T2).

Per-block synthesis keeps picture cuts aligned to spoken lines. Results are
cached by sha256 of voice id plus text, durations come from ``ffprobe``, and the
blocks are concatenated with a small gap into one voice track. Voice id comes
from config only, so switching to Tom's clone is an env var change.
"""
