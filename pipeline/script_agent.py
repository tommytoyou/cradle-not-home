"""One LLM call turns the day's theme into both scripts (T1b).

Builds a daily package validated against ``pipeline/schemas/daily_package.json``:
exactly one ``main`` (60-180 s) and one ``punch`` (22-32 s estimated), separate
original scripts rather than a cut of each other. Moods are checked against a
closed ``MOODS`` list derived from ``broll/queries.yaml``. The NASA credit block
is appended to every description in code, never left to the LLM. The vendor call
stays in one function so it can be swapped.

The fact guard is the load-bearing safety check: every digit sequence in every
script block must appear in the theme's ``facts``, so an unattended pipeline
cannot invent a statistic.
"""

from __future__ import annotations

import json
import re
from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from pipeline.config import ROOT, Config

PROMPTS_DIR = ROOT / "prompts"
QUERIES_PATH = ROOT / "broll" / "queries.yaml"
SCHEMA_PATH = Path(__file__).parent / "schemas" / "daily_package.json"

# Verbatim from 02_CHANNEL_FORMAT.md. Appended in code; the LLM never writes it.
CREDIT_BLOCK = (
    "Visuals: NASA public-domain mission footage, edited and graded. "
    "NASA does not endorse this channel. Script and narration original. "
    "Music licensed."
)

MAX_ATTEMPTS = 3
MAX_OUTPUT_TOKENS = 16000
DEFAULT_WPM = 140
DURATION_LIMITS = {"main": (60.0, 180.0), "punch": (22.0, 32.0)}
MIN_BLOCK_WORDS = 5
MAX_BLOCK_WORDS = 30
MAX_HOOK_WORDS = 6

_DIGIT_RE = re.compile(r"\d+(?:\.\d+)?")


class ScriptAgentError(RuntimeError):
    """Raised when no attempt produced a valid package, or a prompt is missing."""


def _load_moods() -> frozenset[str]:
    """The closed mood vocabulary, read from the b-roll query catalogue."""
    data = yaml.safe_load(QUERIES_PATH.read_text(encoding="utf-8"))
    moods: set[str] = set()
    for entry in data["queries"]:
        moods.update(entry.get("moods", []))
    return frozenset(moods)


MOODS: frozenset[str] = _load_moods()


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def estimate_duration_sec(text: str, wpm: int = DEFAULT_WPM) -> float:
    """Narration seconds for ``text`` at ``wpm`` words per minute."""
    return len(text.split()) / wpm * 60.0


def _video_text(video: dict) -> str:
    return " ".join(block["text"] for block in video["script_blocks"])


def _format_schema_error(error) -> str:
    location = "/".join(str(part) for part in error.absolute_path) or "package"
    return f"{location}: {error.message}"


def validate_package(pkg: dict, theme: dict | None = None) -> list[str]:
    """Return a list of problems; empty means valid.

    ``theme`` is optional only so the signature in the ticket stays callable on
    its own. Pass it — without the theme the fact guard cannot run, and
    :func:`build_package` always passes it.
    """
    errors = sorted(
        _validator().iter_errors(pkg),
        key=lambda e: [str(part) for part in e.absolute_path],
    )
    if errors:
        # Structure is wrong; the semantic checks below would only add noise.
        return [_format_schema_error(error) for error in errors]

    problems: list[str] = []

    formats = sorted(video["format"] for video in pkg["videos"])
    if formats != ["main", "punch"]:
        problems.append(f"videos must be exactly one main and one punch, got {formats}")

    for index, video in enumerate(pkg["videos"]):
        where = f"videos/{index} ({video['format']})"
        low, high = DURATION_LIMITS[video["format"]]
        estimate = estimate_duration_sec(_video_text(video))
        if not low <= estimate <= high:
            problems.append(
                f"{where}: narration estimates {estimate:.1f}s, needs {low:g}-{high:g}s"
            )

        hook_words = len(video["hook_text"].split())
        if hook_words > MAX_HOOK_WORDS:
            problems.append(
                f"{where}: hook_text has {hook_words} words, max {MAX_HOOK_WORDS}"
            )

        for block_index, block in enumerate(video["script_blocks"]):
            block_where = f"{where} block {block_index}"

            unknown = [mood for mood in block["moods"] if mood not in MOODS]
            if unknown:
                problems.append(f"{block_where}: moods not in MOODS: {unknown}")

            words = len(block["text"].split())
            if not MIN_BLOCK_WORDS <= words <= MAX_BLOCK_WORDS:
                problems.append(
                    f"{block_where}: {words} words, needs "
                    f"{MIN_BLOCK_WORDS}-{MAX_BLOCK_WORDS}"
                )

            for word in block["emphasis"]:
                pattern = rf"\b{re.escape(word)}\b"
                if not re.search(pattern, block["text"], re.IGNORECASE):
                    problems.append(
                        f"{block_where}: emphasis {word!r} is not in the block text"
                    )

    if theme is not None:
        problems.extend(_fact_problems(pkg, theme))

    return problems


def _fact_problems(pkg: dict, theme: dict) -> list[str]:
    """Every digit sequence in the scripts must come from the theme's facts."""
    allowed = " ".join(theme.get("facts") or [])
    problems: list[str] = []
    for index, video in enumerate(pkg["videos"]):
        for block_index, block in enumerate(video["script_blocks"]):
            for number in _DIGIT_RE.findall(block["text"]):
                if number not in allowed:
                    problems.append(
                        f"videos/{index} block {block_index}: {number!r} is not in "
                        f"theme {theme['id']!r} facts"
                    )
    return problems


def _theme_as_prompt(theme: dict) -> str:
    """The theme as the model sees it: everything but bookkeeping."""
    return json.dumps(
        {key: value for key, value in theme.items() if key != "last_used"},
        ensure_ascii=False,
        indent=2,
    )


def _load_fewshots() -> list[dict]:
    paths = sorted(PROMPTS_DIR.glob("fewshot_*.json"))
    if not paths:
        raise ScriptAgentError(f"no fewshot_*.json in {PROMPTS_DIR}")
    return [json.loads(path.read_text(encoding="utf-8")) for path in paths]


def build_messages(theme: dict) -> list[dict]:
    """Few-shots as alternating turns, then today's theme as the final user turn."""
    messages: list[dict] = []
    for example in _load_fewshots():
        messages.append({"role": "user", "content": _theme_as_prompt(example["theme"])})
        messages.append(
            {
                "role": "assistant",
                "content": json.dumps(example["output"], ensure_ascii=False, indent=2),
            }
        )
    messages.append({"role": "user", "content": _theme_as_prompt(theme)})
    return messages


def _system_prompt() -> str:
    path = PROMPTS_DIR / "script_system.md"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ScriptAgentError(f"system prompt not found: {path}") from None


def _complete(messages: list[dict], system: str, cfg: Config) -> str:
    """The only function that talks to the LLM vendor."""
    import anthropic  # imported lazily so the offline tests need no SDK

    client = anthropic.Anthropic(api_key=cfg.llm_api_key, base_url=cfg.llm_base_url)
    response = client.messages.create(
        model=cfg.llm_model,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=system,
        messages=messages,
    )
    return "".join(block.text for block in response.content if block.type == "text")


def _parse_videos(raw: str) -> list:
    """Parse the model's reply, tolerating a stray markdown fence."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ScriptAgentError(f"reply was not JSON: {exc}") from exc
    if not isinstance(payload, dict) or "videos" not in payload:
        raise ScriptAgentError("reply has no 'videos' key")
    return payload["videos"]


def build_package(theme: dict, cfg: Config, today: date) -> dict:
    """Ask for both scripts, validate, and retry up to ``MAX_ATTEMPTS`` times."""
    system = _system_prompt()
    messages = build_messages(theme)
    failures: list[str] = []

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            videos = _parse_videos(_complete(messages, system, cfg))
        except ScriptAgentError as exc:
            failures.append(f"attempt {attempt}: {exc}")
            continue

        pkg = {"id": today.isoformat(), "theme_id": theme["id"], "videos": videos}
        _append_credit(pkg)

        problems = validate_package(pkg, theme)
        if not problems:
            return pkg
        failures.append(f"attempt {attempt}: " + "; ".join(problems))

    raise ScriptAgentError(
        f"no valid package for theme {theme['id']!r} after {MAX_ATTEMPTS} attempts:\n"
        + "\n".join(failures)
    )


def _append_credit(pkg: dict) -> None:
    for video in pkg["videos"]:
        if isinstance(video, dict) and isinstance(video.get("description"), str):
            video["description"] = f"{video['description'].rstrip()}\n\n{CREDIT_BLOCK}"
