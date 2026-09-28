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
