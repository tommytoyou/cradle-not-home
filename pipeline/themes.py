"""Theme rotation over ``data/themes.json`` (T1a).

Loads themes, picks the day's theme (never-used first, then oldest
``last_used``), and records the pick with an atomic write so a crashed run
cannot corrupt the file.
"""
