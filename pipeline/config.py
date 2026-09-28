"""Typed config loaded from ``.env``; no hardcoded paths anywhere else (T1a).

Exposes a frozen ``Config`` dataclass (API keys, voice id, OAuth paths, library
and output dirs, ``min_library_clips``, ``clip_cooldown_days``, optional notify
webhook) and ``load_config()``, which raises ``ConfigError`` listing every
missing variable at once.
"""
