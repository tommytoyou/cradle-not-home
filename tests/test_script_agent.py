"""T1b: prompt assembly, package validation, the fact guard, and retries.

Offline: the vendor call is monkeypatched everywhere, so no network and no SDK.
"""

import copy
import json
from datetime import date
from pathlib import Path

import pytest

from pipeline import script_agent
from pipeline.config import ROOT, Config
from pipeline.script_agent import (
    CREDIT_BLOCK,
    MAX_ATTEMPTS,
    MOODS,
    ScriptAgentError,
    build_messages,
    build_package,
    estimate_duration_sec,
    validate_package,
)

TODAY = date(2026, 9, 28)
FEWSHOTS = sorted((ROOT / "prompts").glob("fewshot_*.json"))


def load_example(name: str = "fewshot_01.json") -> dict:
    return json.loads((ROOT / "prompts" / name).read_text(encoding="utf-8"))


def make_cfg(**overrides) -> Config:
    base = dict(
        llm_api_key="key",
        llm_base_url="https://example.invalid",
        llm_model="claude-opus-5",
        elevenlabs_api_key="key",
        elevenlabs_voice_id="voice",
        youtube_client_secrets=Path("client_secret.json"),
        youtube_token=Path("token.json"),
        library_dir=Path("library"),
        music_dir=Path("music"),
        output_dir=Path("output"),
        data_dir=Path("data"),
        font_path=Path("font.ttf"),
    )
    return Config(**(base | overrides))


def valid_package(example: dict | None = None) -> dict:
    example = example or load_example()
    return {
        "id": TODAY.isoformat(),
        "theme_id": example["theme"]["id"],
        "videos": copy.deepcopy(example["output"]["videos"]),
    }


def reply(example: dict | None = None) -> str:
    example = example or load_example()
    return json.dumps({"videos": example["output"]["videos"]})


def fake_completer(*replies: str):
    """Return a stand-in for _complete that yields each reply in turn."""
    calls = []

    def _complete(messages, system, cfg):
        calls.append({"messages": messages, "system": system})
        return replies[min(len(calls) - 1, len(replies) - 1)]

    _complete.calls = calls
    return _complete


# --- moods and duration ----------------------------------------------------


def test_moods_is_the_closed_list_from_queries_yaml():
    assert isinstance(MOODS, frozenset)
    assert MOODS == {
        "awe", "beauty", "commitment", "destination", "discipline", "first",
        "future", "grind", "heritage", "humility", "ignition", "isolation",
        "leaving", "perspective", "pressure", "proof", "reveal", "scale",
        "standards", "systems", "training", "work",
    }


def test_estimate_duration_sec():
    assert estimate_duration_sec("one two three") == pytest.approx(3 / 140 * 60)
    assert estimate_duration_sec("a b c d", wpm=60) == pytest.approx(4.0)
    assert estimate_duration_sec("") == 0.0


# --- the delivered examples are the reference ------------------------------


@pytest.mark.parametrize("path", FEWSHOTS, ids=lambda p: p.name)
def test_delivered_fewshots_validate(path):
    example = json.loads(path.read_text(encoding="utf-8"))
    assert validate_package(valid_package(example), example["theme"]) == []


@pytest.mark.parametrize("path", FEWSHOTS, ids=lambda p: p.name)
def test_delivered_fewshot_durations_match_their_own_targets(path):
    example = json.loads(path.read_text(encoding="utf-8"))
    for video in example["output"]["videos"]:
        text = " ".join(block["text"] for block in video["script_blocks"])
        assert estimate_duration_sec(text) == pytest.approx(
            video["duration_target_sec"], abs=1.0
        )


# --- structural validation -------------------------------------------------


def test_missing_key_is_reported():
    pkg = valid_package()
    del pkg["theme_id"]

    assert any("theme_id" in problem for problem in validate_package(pkg))


def test_needs_exactly_one_main_and_one_punch():
    pkg = valid_package()
    pkg["videos"][1]["format"] = "main"

    problems = validate_package(pkg)

    assert any("one main and one punch" in problem for problem in problems)


def test_rejects_a_third_video():
    pkg = valid_package()
    pkg["videos"].append(copy.deepcopy(pkg["videos"][0]))

    assert validate_package(pkg) != []


def test_rejects_title_over_90_chars():
    pkg = valid_package()
    pkg["videos"][0]["title"] = "x" * 91

    assert any("title" in problem for problem in validate_package(pkg))


def test_rejects_unknown_mood():
    pkg = valid_package()
    pkg["videos"][0]["script_blocks"][0]["moods"] = ["nostalgia"]

    assert any("nostalgia" in problem for problem in validate_package(pkg))


def test_rejects_more_than_three_moods():
    pkg = valid_package()
    pkg["videos"][0]["script_blocks"][0]["moods"] = ["awe", "grind", "work", "scale"]

    assert validate_package(pkg) != []


def test_rejects_block_outside_word_limits():
    pkg = valid_package()
    pkg["videos"][0]["script_blocks"][0]["text"] = "Too short."

    assert any("words" in problem for problem in validate_package(pkg))


def test_rejects_emphasis_absent_from_block_text():
    pkg = valid_package()
    pkg["videos"][0]["script_blocks"][0]["emphasis"] = ["unrelated"]

    problems = validate_package(pkg)

    assert any("unrelated" in problem for problem in problems)


def test_emphasis_match_ignores_case():
    pkg = valid_package()
    block = pkg["videos"][0]["script_blocks"][0]
    word = block["text"].split()[0].strip(".,")
    block["emphasis"] = [word.upper()]

    assert validate_package(pkg) == []


def test_rejects_hook_text_over_six_words():
    pkg = valid_package()
    pkg["videos"][0]["hook_text"] = "one two three four five six seven"

    assert any("hook_text" in problem for problem in validate_package(pkg))


def test_rejects_duration_outside_the_format_window():
    pkg = valid_package()
    # Trim the main down to punch length.
    pkg["videos"][0]["script_blocks"] = pkg["videos"][0]["script_blocks"][:2]

    problems = validate_package(pkg)

    assert any("needs 60-180s" in problem for problem in problems)


# --- fact guard ------------------------------------------------------------


def test_fact_guard_allows_numbers_present_in_facts():
    example = load_example()

    assert validate_package(valid_package(example), example["theme"]) == []


def test_fact_guard_rejects_a_number_absent_from_facts():
    example = load_example()
    theme = copy.deepcopy(example["theme"])
    theme["facts"] = []

    problems = validate_package(valid_package(example), theme)

    assert any("not in theme" in problem for problem in problems)


def test_fact_guard_rejects_an_invented_statistic():
    example = load_example()
    pkg = valid_package(example)
    pkg["videos"][1]["script_blocks"][0]["text"] = "Only 400 people ever made the trip."

    problems = validate_package(pkg, example["theme"])

    assert any("'400'" in problem for problem in problems)


def test_fact_guard_handles_a_theme_with_no_facts_key():
    example = load_example()
    theme = {k: v for k, v in example["theme"].items() if k != "facts"}
    pkg = valid_package(example)

    assert any("not in theme" in problem for problem in validate_package(pkg, theme))


def test_fact_guard_is_skipped_without_a_theme():
    example = load_example()
    pkg = valid_package(example)
    pkg["videos"][1]["script_blocks"][0]["text"] = "Only 400 people ever made the trip."

    assert all("not in theme" not in problem for problem in validate_package(pkg))


# --- prompt assembly -------------------------------------------------------


def test_build_messages_alternates_and_ends_with_todays_theme():
    theme = {"id": "cradle", "title_seed": "seed", "angles": [], "facts": [],
             "last_used": "2026-01-01"}

    messages = build_messages(theme)

    assert len(messages) == 2 * len(FEWSHOTS) + 1
    assert [m["role"] for m in messages] == ["user", "assistant"] * len(FEWSHOTS) + ["user"]
    final = json.loads(messages[-1]["content"])
    assert final["id"] == "cradle"
    assert "last_used" not in final


def test_fewshot_turns_carry_the_example_theme_and_output():
    messages = build_messages({"id": "x", "title_seed": "s", "angles": [], "facts": []})
    first_example = load_example(FEWSHOTS[0].name)

    assert json.loads(messages[0]["content"])["id"] == first_example["theme"]["id"]
    assert json.loads(messages[1]["content"]) == first_example["output"]


def test_system_prompt_is_the_delivered_file(monkeypatch):
    completer = fake_completer(reply())
    monkeypatch.setattr(script_agent, "_complete", completer)

    build_package(load_example()["theme"], make_cfg(), TODAY)

    expected = (ROOT / "prompts" / "script_system.md").read_text(encoding="utf-8")
    assert completer.calls[0]["system"] == expected


# --- build_package --------------------------------------------------------


def test_build_package_stamps_id_and_theme_id(monkeypatch):
    monkeypatch.setattr(script_agent, "_complete", fake_completer(reply()))
    theme = load_example()["theme"]

    pkg = build_package(theme, make_cfg(), TODAY)

    assert pkg["id"] == "2026-09-28"
    assert pkg["theme_id"] == theme["id"]
    assert [v["format"] for v in pkg["videos"]] == ["main", "punch"]


def test_build_package_appends_the_credit_block_once(monkeypatch):
    monkeypatch.setattr(script_agent, "_complete", fake_completer(reply()))
    original = load_example()["output"]["videos"][0]["description"]

    pkg = build_package(load_example()["theme"], make_cfg(), TODAY)

    for video in pkg["videos"]:
        assert video["description"].count(CREDIT_BLOCK) == 1
        assert video["description"].endswith(CREDIT_BLOCK)
    assert pkg["videos"][0]["description"].startswith(original)


def test_build_package_tolerates_a_markdown_fence(monkeypatch):
    fenced = "```json\n" + reply() + "\n```"
    monkeypatch.setattr(script_agent, "_complete", fake_completer(fenced))

    pkg = build_package(load_example()["theme"], make_cfg(), TODAY)

    assert len(pkg["videos"]) == 2


def test_build_package_retries_then_succeeds(monkeypatch):
    completer = fake_completer("not json at all", '{"videos": []}', reply())
    monkeypatch.setattr(script_agent, "_complete", completer)

    pkg = build_package(load_example()["theme"], make_cfg(), TODAY)

    assert len(completer.calls) == 3
    assert pkg["theme_id"] == "example-gravity-assist"


def test_build_package_raises_after_max_attempts(monkeypatch):
    completer = fake_completer("still not json")
    monkeypatch.setattr(script_agent, "_complete", completer)

    with pytest.raises(ScriptAgentError) as excinfo:
        build_package(load_example()["theme"], make_cfg(), TODAY)

    assert len(completer.calls) == MAX_ATTEMPTS
    assert "after 3 attempts" in str(excinfo.value)


def test_build_package_retries_on_an_invented_statistic(monkeypatch):
    example = load_example()
    bad = copy.deepcopy(example["output"])
    bad["videos"][1]["script_blocks"][0]["text"] = "Only 400 ever left the ground here."
    completer = fake_completer(json.dumps(bad), reply())
    monkeypatch.setattr(script_agent, "_complete", completer)

    pkg = build_package(example["theme"], make_cfg(), TODAY)

    assert len(completer.calls) == 2
    assert validate_package(pkg, example["theme"]) == []


def test_build_package_reports_every_failure_in_the_error(monkeypatch):
    example = load_example()
    bad = copy.deepcopy(example["output"])
    bad["videos"][0]["script_blocks"][0]["moods"] = ["nostalgia"]
    monkeypatch.setattr(script_agent, "_complete", fake_completer(json.dumps(bad)))

    with pytest.raises(ScriptAgentError, match="nostalgia"):
        build_package(example["theme"], make_cfg(), TODAY)
