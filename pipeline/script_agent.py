"""One LLM call turns the day's theme into both scripts (T1b).

Builds a daily package validated against ``pipeline/schemas/daily_package.json``:
exactly one ``main`` (60-180 s) and one ``punch`` (22-32 s estimated), separate
original scripts rather than a cut of each other. Moods are checked against a
closed ``MOODS`` list derived from ``broll/queries.yaml``. The NASA credit block
is appended to every description in code, never left to the LLM. The vendor call
stays in one function so it can be swapped.
"""
