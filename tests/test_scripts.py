"""T10: the Task Scheduler scripts.

``run_daily.ps1`` runs for real, but from a copy in ``tmp_path`` next to a stub
``pipeline.run_daily`` that records its arguments, so the pipeline never runs.
``install_task.ps1`` needs an elevated shell and changes the machine, so it is
only parsed and inspected, never executed.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from pipeline.config import ROOT

SCRIPTS = ROOT / "scripts"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")
needs_powershell = pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell not found")

STUB = """\
import json, os, sys
print("ARGS " + json.dumps(sys.argv[1:]))
print("VENV " + os.environ.get("STUB_VENV", "no"))
print("stderr line", file=sys.stderr)
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
"""


def powershell(*args: str, cwd: Path | None = None, env: dict | None = None):
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", *args],
        capture_output=True, text=True, cwd=cwd, env=env,
    )


@pytest.mark.parametrize("name", ["run_daily.ps1", "install_task.ps1"])
def test_scripts_are_ascii(name):
    """Windows PowerShell 5.1 reads a file without a BOM as ANSI."""
    (SCRIPTS / name).read_bytes().decode("ascii")


@needs_powershell
@pytest.mark.parametrize("name", ["run_daily.ps1", "install_task.ps1"])
def test_scripts_parse(name):
    path = str(SCRIPTS / name).replace("'", "''")
    check = (
        "$errors = $null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$null, [ref]$errors) | Out-Null; "
        "$errors | ForEach-Object { $_.Message }; exit $errors.Count"
    )
    result = powershell("-Command", check)

    assert result.returncode == 0, result.stdout


@pytest.fixture
def fake_repo(tmp_path):
    """A repo copy where ``python -m pipeline.run_daily`` is the stub above."""
    repo = tmp_path / "repo with spaces"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPTS / "run_daily.ps1", repo / "scripts" / "run_daily.ps1")
    (repo / "pipeline").mkdir()
    (repo / "pipeline" / "__init__.py").write_text("")
    (repo / "pipeline" / "run_daily.py").write_text(STUB)
    return repo


def run_script(repo: Path, *args: str, **env_extra: str):
    env = {k: v for k, v in os.environ.items() if not k.startswith("STUB_")}
    env.update(env_extra)
    # Started from somewhere else, as Task Scheduler would.
    return powershell("-File", str(repo / "scripts" / "run_daily.ps1"), *args,
                      cwd=repo.parent, env=env)


def log_lines(repo: Path) -> list[str]:
    return (repo / "output" / "scheduler.log").read_text(encoding="utf-8-sig").splitlines()


def stub_args(repo: Path) -> list[list[str]]:
    return [json.loads(line[5:]) for line in log_lines(repo) if line.startswith("ARGS ")]


@needs_powershell
def test_run_daily_is_a_dry_run_by_default(fake_repo):
    result = run_script(fake_repo)

    assert result.returncode == 0, result.stderr
    assert stub_args(fake_repo) == [["--dry-run"]]
    assert any("start: dry run (no uploads)" in line for line in log_lines(fake_repo))


@needs_powershell
def test_live_switch_drops_dry_run(fake_repo):
    result = run_script(fake_repo, "-Live")

    assert result.returncode == 0, result.stderr
    assert stub_args(fake_repo) == [[]]
    assert any("start: LIVE (uploads on)" in line for line in log_lines(fake_repo))


@needs_powershell
def test_output_and_errors_are_appended_to_the_log(fake_repo):
    run_script(fake_repo)
    run_script(fake_repo)

    lines = log_lines(fake_repo)
    assert len(stub_args(fake_repo)) == 2
    assert lines.count("stderr line") == 2
    assert not any("NativeCommandError" in line for line in lines)
    assert sum("finished with exit code 0" in line for line in lines) == 2


@needs_powershell
def test_pipeline_exit_code_is_passed_through(fake_repo):
    result = run_script(fake_repo, STUB_EXIT="1")

    assert result.returncode == 1
    assert any("finished with exit code 1" in line for line in log_lines(fake_repo))


@needs_powershell
def test_venv_is_activated_when_present(fake_repo):
    scripts = fake_repo / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "Activate.ps1").write_text("$env:STUB_VENV = 'yes'\n")

    run_script(fake_repo)

    lines = log_lines(fake_repo)
    assert "VENV yes" in lines
    assert not any("no .venv found" in line for line in lines)


@needs_powershell
def test_missing_venv_falls_back_to_path_python_and_says_so(fake_repo):
    run_script(fake_repo)

    lines = log_lines(fake_repo)
    assert "VENV no" in lines
    assert any("no .venv found; using python from PATH" in line for line in lines)


def test_install_task_registers_the_ticketed_schedule():
    text = (SCRIPTS / "install_task.ps1").read_text(encoding="ascii")

    assert "[string]$At = '02:00'" in text
    assert "-Daily" in text
    assert "-WakeToRun" in text
    assert "-LogonType S4U" in text  # runs whether the user is logged on or not
    assert "run_daily.ps1" in text
    assert "IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)" in text


def test_install_task_is_dry_run_unless_asked():
    text = (SCRIPTS / "install_task.ps1").read_text(encoding="ascii")

    assert "[switch]$Live" in text
    assert "if ($Live) {\n    $arguments += ' -Live'" in text.replace("\r\n", "\n")
