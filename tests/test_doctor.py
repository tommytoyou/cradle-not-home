"""T12: pipeline/doctor.py.

Checks run against a fake ``.env`` mapping pointing into ``tmp_path``; the
PATH lookup and the Task Scheduler query are stubbed, except one test that
asks the real Task Scheduler for a name that cannot exist.
"""

import csv
import shutil
import sys

import pytest

from pipeline import doctor
from pipeline.config import REQUIRED_TEXT, ROOT
from pipeline.doctor import FAIL, PASS, WARN, TASK_NAME, main, run_checks
from pipeline.snippets import FIELDNAMES

ON_WINDOWS = sys.platform == "win32"


@pytest.fixture
def env(tmp_path):
    """A complete .env whose every path exists and is healthy."""
    values = {key: "x" for key in REQUIRED_TEXT}
    values |= {
        "YOUTUBE_CLIENT_SECRETS": str(tmp_path / "client_secret.json"),
        "YOUTUBE_TOKEN": str(tmp_path / "token.json"),
        "LIBRARY_DIR": str(tmp_path / "library"),
        "MUSIC_DIR": str(tmp_path / "music"),
        "OUTPUT_DIR": str(tmp_path / "output"),
        "DATA_DIR": str(tmp_path / "data"),
        "FONT_PATH": str(tmp_path / "font.ttf"),
        "MIN_LIBRARY_CLIPS": "3",
    }
    (tmp_path / "music").mkdir()
    (tmp_path / "music" / "bed.mp3").write_bytes(b"mp3")
    (tmp_path / "font.ttf").write_bytes(b"font")
    (tmp_path / "token.json").write_text("{}")
    write_library(tmp_path, [("05_eva/a.mp4", "0"), ("05_eva/b.mp4", "0"), ("08_deep/c.mp4", "0")])
    return values


def write_library(tmp_path, clips, *, on_disk=None):
    """Index ``clips`` as (path, logo_risk); create the files listed in ``on_disk``."""
    library = tmp_path / "library"
    for path, _ in clips if on_disk is None else [(p, "0") for p in on_disk]:
        (library / path).parent.mkdir(parents=True, exist_ok=True)
        (library / path).write_bytes(b"clip")
    (tmp_path / "data").mkdir(exist_ok=True)
    with (tmp_path / "data" / "snippets.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        for path, logo in clips:
            writer.writerow({"path": path, "logo_risk": logo})


@pytest.fixture
def healthy(monkeypatch):
    """FFmpeg on PATH, Windows, and the task registered in dry-run mode."""
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"C:/bin/{name}.exe")
    monkeypatch.setattr(doctor.sys, "platform", "win32")
    monkeypatch.setattr(doctor, "scheduled_task",
                        lambda name: ("Ready", r'-NoProfile -File "C:\repo\scripts\run_daily.ps1"'))


def results(env, **kwargs):
    return {r.name: r for r, _ in run_checks(env, **kwargs)}


def test_all_healthy(env, healthy):
    found = results(env)

    assert {name: r.status for name, r in found.items()} == {
        "ffmpeg": PASS, "ffprobe": PASS, ".env values": PASS, "music": PASS,
        "library": PASS, "font": PASS, "YouTube token": PASS, "scheduled task": PASS,
    }
    assert found["library"].detail == "3 usable of 3 indexed, need 3"
    assert found["scheduled task"].detail == f"'{TASK_NAME}' ready, dry run"


def test_main_exits_0_when_healthy(env, healthy, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "read_env", lambda: env)

    assert main([]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 9
    assert all(line.startswith("[PASS] ") for line in out[:8])
    assert out[-1] == "doctor: 8 passed, 0 failed, 0 warning(s)"


def test_output_is_ascii(env, healthy, monkeypatch, capsys):
    """Windows PowerShell 5.1 consoles default to a legacy code page."""
    monkeypatch.setattr(doctor, "read_env", lambda: {})
    main([])
    capsys.readouterr().out.encode("ascii")


# --- one failure each ------------------------------------------------------


def test_missing_ffprobe_fails_with_the_install_fix(env, healthy, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None if name == "ffprobe" else "x")

    result = results(env)["ffprobe"]

    assert result.status == FAIL
    assert "winget install Gyan.FFmpeg" in result.line()


def test_missing_and_bad_env_values_are_listed(env, healthy):
    env["ELEVENLABS_VOICE_ID"] = "  "
    del env["LLM_MODEL"]
    env["CLIP_COOLDOWN_DAYS"] = "three"

    result = results(env)[".env values"]

    assert result.status == FAIL
    assert "missing ELEVENLABS_VOICE_ID, LLM_MODEL" in result.detail
    assert "CLIP_COOLDOWN_DAYS='three'" in result.detail


def test_one_missing_value_does_not_hide_the_rest(env, healthy):
    del env["ELEVENLABS_VOICE_ID"]  # still blocked on the stakeholder

    found = results(env)

    assert found[".env values"].status == FAIL
    assert all(found[name].status == PASS for name in ("music", "library", "font"))


@pytest.mark.parametrize("make", ["empty", "missing", "wrong type"])
def test_music_needs_a_track(env, healthy, tmp_path, make):
    track = tmp_path / "music" / "bed.mp3"
    track.unlink()
    if make == "missing":
        (tmp_path / "music").rmdir()
    elif make == "wrong type":
        (tmp_path / "music" / "notes.txt").write_text("x")

    result = results(env)["music"]

    assert result.status == FAIL
    assert ".mp3" in result.fix and str(tmp_path / "music") in result.fix


def test_library_below_the_minimum_fails(env, healthy):
    env["MIN_LIBRARY_CLIPS"] = "200"

    result = results(env)["library"]

    assert result.status == FAIL
    assert result.detail == "3 usable of 3 indexed, need 200"
    assert "python -m broll.autocut" in result.fix


def test_logo_clips_and_deleted_files_are_not_usable(env, healthy, tmp_path):
    write_library(tmp_path, [("05_eva/a.mp4", "0"), ("05_eva/logo.mp4", "1"), ("05_eva/gone.mp4", "0")],
                  on_disk=["05_eva/a.mp4", "05_eva/logo.mp4"])
    env["MIN_LIBRARY_CLIPS"] = "2"

    result = results(env)["library"]

    assert result.status == FAIL
    assert result.detail.startswith("1 usable of 3 indexed, need 2")


def test_library_mentions_clips_waiting_for_review(env, healthy, tmp_path):
    env["MIN_LIBRARY_CLIPS"] = "200"
    inbox = tmp_path / "library" / "_inbox"
    inbox.mkdir()
    for i in range(4):
        (inbox / f"x_t{i:05d}.mp4").write_bytes(b"")

    result = results(env)["library"]

    assert result.detail.endswith("; 4 waiting in _inbox review")
    assert "python -m broll.apply_review" in result.fix


def test_library_default_minimum_is_the_config_default(env, healthy):
    del env["MIN_LIBRARY_CLIPS"]

    assert results(env)["library"].detail == "3 usable of 3 indexed, need 200"


def test_library_without_an_index(env, healthy, tmp_path):
    (tmp_path / "data" / "snippets.csv").unlink()

    result = results(env)["library"]

    assert result.status == FAIL
    assert result.fix == "python -m pipeline.snippets scan"


def test_missing_font_fails(env, healthy, tmp_path):
    (tmp_path / "font.ttf").unlink()

    assert results(env)["font"].status == FAIL


def test_missing_token_is_only_a_warning(env, healthy, tmp_path, monkeypatch, capsys):
    (tmp_path / "token.json").unlink()
    monkeypatch.setattr(doctor, "read_env", lambda: env)

    result = results(env)["YouTube token"]
    code = main([])

    assert result.status == WARN
    assert "python -m pipeline.youtube_upload auth" in result.fix
    assert code == 0
    assert capsys.readouterr().out.splitlines()[-1] == "doctor: 7 passed, 0 failed, 1 warning(s)"


def test_unset_paths_say_which_key(env, healthy):
    for key in ("MUSIC_DIR", "LIBRARY_DIR", "FONT_PATH"):
        del env[key]

    found = results(env)

    assert found["music"].detail == "MUSIC_DIR is not set"
    assert found["library"].detail == "LIBRARY_DIR is not set"
    assert found["font"].detail == "FONT_PATH is not set"


# --- the scheduled task ----------------------------------------------------


def test_unregistered_task_fails_with_the_install_command(env, healthy, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "scheduled_task", lambda name: None)
    monkeypatch.setattr(doctor, "read_env", lambda: env)

    result = results(env)["scheduled task"]
    code = main([])

    assert result.status == FAIL
    assert r"scripts\install_task.ps1" in result.fix
    assert code == 1
    assert "doctor: 7 passed, 1 failed, 0 warning(s)" in capsys.readouterr().out


def test_live_task_is_reported(env, healthy, monkeypatch):
    monkeypatch.setattr(doctor, "scheduled_task", lambda name: ("Ready", '-File "run_daily.ps1" -Live'))

    assert results(env)["scheduled task"].detail.endswith("ready, LIVE uploads")


def test_disabled_task_fails(env, healthy, monkeypatch):
    monkeypatch.setattr(doctor, "scheduled_task", lambda name: ("Disabled", "-File run_daily.ps1"))

    result = results(env)["scheduled task"]

    assert result.status == FAIL
    assert "Enable-ScheduledTask" in result.fix


def test_task_name_option_is_used(env, healthy, monkeypatch):
    asked = []
    monkeypatch.setattr(doctor, "scheduled_task", lambda name: asked.append(name) or None)
    monkeypatch.setattr(doctor, "read_env", lambda: env)

    main(["--task-name", "Other task"])

    assert asked == ["Other task"]


def test_task_check_needs_windows(env, healthy, monkeypatch):
    monkeypatch.setattr(doctor.sys, "platform", "linux")

    assert results(env)["scheduled task"].status == FAIL


def test_install_script_default_name_matches():
    text = (ROOT / "scripts" / "install_task.ps1").read_text(encoding="ascii")

    assert f"[string]$TaskName = '{TASK_NAME}'" in text


@pytest.mark.skipif(not ON_WINDOWS or shutil.which("powershell.exe") is None,
                    reason="needs Windows PowerShell")
def test_real_lookup_of_an_absent_task_is_none():
    assert doctor.scheduled_task("cradle-not-home doctor test, no such task") is None
