"""NASA clip index and picker over ``data/snippets.csv`` (T3).

Scans the gitignored library into the CSV, deriving moods from folder defaults
in ``broll/queries.yaml`` plus filename tags after a double underscore, and
rejecting clips under 1080 px tall or outside 4-12 s. Picking orders by mood
match then least recently used, skipping anything already used today (shared
across both videos) or inside the cooldown, and fails closed when the library is
thin. Also the ``python -m pipeline.snippets scan`` entry point.
"""
