"""Unlisted YouTube uploads via Data API v3 (T6).

Installed-app OAuth with the token cached at ``cfg.youtube_token``, resumable
upload retrying on 5xx. Uploads default to unlisted and anything else needs an
explicit CLI flag; every upload sets ``status.containsSyntheticMedia = true`` for
the synthetic voice and ends the description with ``#Shorts``.

BLOCKED until Tom creates the OAuth client and the channel; tests mock the
service.
"""
