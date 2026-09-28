"""Daily entry point wiring the whole pipeline (T7).

Order: library check, theme pick, package, then per video (main, punch) TTS,
timeline, render, upload; afterwards mark theme and clips used, log and notify.
If either video fails before upload, neither is uploaded and nothing is marked
used. ``--dry-run`` does everything except upload. Output lands in
``output/YYYY-MM-DD/{main,punch}.mp4``.
"""
