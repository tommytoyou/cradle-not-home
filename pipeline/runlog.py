"""Run records and notification (T7).

Appends one JSON line per video to ``data/runs.jsonl`` (gitignored) with date,
package id, format, youtube id, visibility, duration, snippet count, status and
notes, and reports the day's records to stdout plus an optional webhook.
"""
