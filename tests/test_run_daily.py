"""T7: the daily runner and run log.

Themes and the snippet index are real files in ``tmp_path``; the LLM, TTS,
render and upload steps are replaced with fakes, so nothing touches the network
or FFmpeg.
"""

import csv
import json
from datetime import date
from pathlib import Path

import pytest
import requests

from pipeline import run_daily, runlog
from pipeline.assemble import Caption, Shot
from pipeline.config import Config
from pipeline.run_daily import SYNTHETIC_REMINDER, UPLOAD_SHEET_NAME, main, run, upload_sheet
from pipeline.runlog import DRY_RUN, FAILED, UPLOADED, RunRecord, append_run, notify, runs_path
from pipeline.script_agent import CREDIT_BLOCK
from pipeline.snippets import FIELDNAMES, csv_path
from pipeline.tts import BlockAudio

TODAY = date(2026, 10, 1)
CLIPS = [f"04_orbit_earth/{i:02d}.mp4" for i in range(6)]


def make_cfg(tmp_path: Path, **overrides) -> Config:
    base = dict(
        llm_api_key="k",
        llm_base_url="https://example.invalid",
        llm_model="claude-opus-5",
        elevenlabs_api_key="k",
        elevenlabs_voice_id="voice",
        youtube_client_secrets=tmp_path / "client_secret.json",
        youtube_token=tmp_path / "token.json",
        library_dir=tmp_path / "library",
        music_dir=tmp_path / "music",
        output_dir=tmp_path / "output",
        data_dir=tmp_path / "data",
        font_path=tmp_path / "font.ttf",
        min_library_clips=len(CLIPS),
    )
    return Config(**(base | overrides))


def write_data(cfg: Config, clips: list[str] = CLIPS) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    themes = [
        {"id": "gravity", "title_seed": "Gravity", "angles": [], "last_used": None},
        {"id": "fuel", "title_seed": "Fuel", "angles": [], "last_used": None},
    ]
    (cfg.data_dir / "themes.json").write_text(json.dumps(themes), encoding="utf-8")
    with csv_path(cfg).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        for path in clips:
            writer.writerow({
                "path": path, "folder": "04_orbit_earth", "moods": "awe",
                "duration_sec": "6.000", "width": "1920", "height": "1080",
                "nasa_id": "", "credit": "", "faces": "0", "logo_risk": "0", "last_used": "",
            })


def package(theme: dict, cfg: Config, today: date) -> dict:
    def video(fmt, title):
        return {
            "format": fmt,
            "title": title,
            "description": f"Words for {fmt}.\n\n{CREDIT_BLOCK}",
            "tags": ["space", "discipline", "space"],
            "duration_target_sec": 120 if fmt == "main" else 30,
            "hook_text": "Gravity is not the enemy",
            "script_blocks": [{"text": "Use the pull to go further.", "moods": ["awe"], "emphasis": []}],
        }
    return {
        "id": today.isoformat(),
        "theme_id": theme["id"],
        "videos": [video("main", "Gravity Is a Slingshot"), video("punch", "Use the Pull")],
    }


class Fakes:
    """Records what each stage was asked to do; any stage can be made to fail."""

    def __init__(self):
        self.fail: dict[str, Exception] = {}
        self.uploads: list[tuple[str, str]] = []
        self.packages: list[str] = []
        self.clip_cursor = 0

    def check(self, stage):
        if stage in self.fail:
            raise self.fail[stage]

    def build_package(self, theme, cfg, today):
        self.check("package")
        self.packages.append(theme["id"])
        return package(theme, cfg, today)

    def synth_blocks(self, video, cfg, workdir):
        self.check(f"tts:{video['format']}")
        return [BlockAudio(index=0, path=Path(workdir) / "b0.mp3", duration_sec=3.0)]

    def concat_voice(self, blocks, out):
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"voice")
        return 3.0

    def build_timeline(self, video, blocks, cfg, today, used_today):
        self.check(f"timeline:{video['format']}")
        clips = CLIPS[self.clip_cursor:self.clip_cursor + 2]
        self.clip_cursor += 2
        used_today.update(clips)
        shots = [Shot(cfg.library_dir / clip, 1.0, 1.5, False) for clip in clips]
        return shots, [Caption(0.0, 3.0, "Use the pull", [])]

    def render(self, shots, captions, voice, hook_text, cfg, out):
        self.check(f"render:{Path(out).stem}")
        Path(out).write_bytes(b"mp4")
        return Path(out)

    def upload(self, video_path, video, cfg, privacy="unlisted"):
        self.check(f"upload:{video['format']}")
        self.uploads.append((Path(video_path).name, privacy))
        return f"yt_{video['format']}"


@pytest.fixture
def fakes(monkeypatch):
    fake = Fakes()
    for name in ("build_package", "synth_blocks", "concat_voice", "build_timeline", "render", "upload"):
        monkeypatch.setattr(run_daily, name, getattr(fake, name))
    return fake


@pytest.fixture
def cfg(tmp_path):
    config = make_cfg(tmp_path)
    write_data(config)
    return config


def themes_file(cfg):
    return {t["id"]: t["last_used"] for t in json.loads((cfg.data_dir / "themes.json").read_text())}


def clip_dates(cfg):
    with csv_path(cfg).open(encoding="utf-8") as handle:
        return {row["path"]: row["last_used"] for row in csv.DictReader(handle)}


def logged(cfg):
    path = runs_path(cfg)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --- the happy path --------------------------------------------------------


def test_a_good_day_uploads_both_unlisted_and_marks_everything_used(cfg, fakes):
    records = run(TODAY, cfg)

    assert [(r.format, r.status, r.youtube_id, r.visibility) for r in records] == [
        ("main", UPLOADED, "yt_main", "unlisted"),
        ("punch", UPLOADED, "yt_punch", "unlisted"),
    ]
    assert fakes.uploads == [("main.mp4", "unlisted"), ("punch.mp4", "unlisted")]
    assert themes_file(cfg)["gravity"] == TODAY.isoformat()
    assert {p for p, d in clip_dates(cfg).items() if d == TODAY.isoformat()} == set(CLIPS[:4])


def test_files_land_in_the_dated_output_folder(cfg, fakes):
    run(TODAY, cfg)

    day = cfg.output_dir / "2026-10-01"
    assert (day / "main.mp4").is_file()
    assert (day / "punch.mp4").is_file()
    assert json.loads((day / "package.json").read_text())["theme_id"] == "gravity"
    assert not (day / UPLOAD_SHEET_NAME).exists()


def test_run_records_carry_every_field_and_go_to_the_log(cfg, fakes):
    records = run(TODAY, cfg)

    lines = logged(cfg)
    assert len(lines) == 2
    assert set(lines[0]) == {
        "date", "package_id", "format", "youtube_id", "visibility",
        "duration_sec", "snippet_count", "status", "notes",
    }
    assert lines[0] == {
        "date": "2026-10-01", "package_id": "2026-10-01", "format": "main",
        "youtube_id": "yt_main", "visibility": "unlisted", "duration_sec": 3.0,
        "snippet_count": 2, "status": UPLOADED, "notes": "",
    }
    assert [r.format for r in records] == ["main", "punch"]


def test_both_videos_share_one_set_of_used_clips(cfg, fakes, monkeypatch):
    seen = []
    original = fakes.build_timeline

    def spy(video, blocks, cfg, today, used_today):
        seen.append(id(used_today))
        return original(video, blocks, cfg, today, used_today)

    monkeypatch.setattr(run_daily, "build_timeline", spy)
    run(TODAY, cfg)

    assert len(seen) == 2 and seen[0] == seen[1]


# --- failing before upload -------------------------------------------------


@pytest.mark.parametrize(
    "stage", ["package", "tts:main", "tts:punch", "timeline:punch", "render:main", "render:punch"],
)
def test_a_failure_before_upload_uploads_nothing_and_marks_nothing(cfg, fakes, stage):
    fakes.fail[stage] = RuntimeError("boom")
    before_themes, before_clips = themes_file(cfg), clip_dates(cfg)

    records = run(TODAY, cfg)

    assert fakes.uploads == []
    assert [(r.format, r.status) for r in records] == [("main", FAILED), ("punch", FAILED)]
    assert all("boom" in r.notes for r in records)
    assert themes_file(cfg) == before_themes
    assert clip_dates(cfg) == before_clips
    assert len(logged(cfg)) == 2


def test_a_thin_library_fails_closed(tmp_path, fakes):
    cfg = make_cfg(tmp_path, min_library_clips=200)
    write_data(cfg)

    records = run(TODAY, cfg)

    assert {r.status for r in records} == {FAILED}
    assert "library check: library has fewer than 200 clips" in records[0].notes
    assert fakes.packages == []
    assert fakes.uploads == []


def test_a_rerun_after_a_failure_gets_the_same_theme(cfg, fakes):
    fakes.fail["render:punch"] = RuntimeError("disk full")
    run(TODAY, cfg)
    del fakes.fail["render:punch"]
    fakes.clip_cursor = 0

    run(TODAY, cfg)

    assert fakes.packages == ["gravity", "gravity"]


def test_stage_is_named_in_the_notes(cfg, fakes):
    fakes.fail["tts:punch"] = RuntimeError("quota")

    records = run(TODAY, cfg)

    assert records[0].notes == "punch: quota"


# --- failing during upload -------------------------------------------------


def test_one_failed_upload_keeps_the_other_and_marks_used(cfg, fakes):
    fakes.fail["upload:punch"] = RuntimeError("HTTP 403")

    records = run(TODAY, cfg)

    assert [(r.format, r.status, r.youtube_id) for r in records] == [
        ("main", UPLOADED, "yt_main"),
        ("punch", FAILED, None),
    ]
    assert records[1].notes == "upload: HTTP 403"
    assert records[1].duration_sec == 3.0
    assert themes_file(cfg)["gravity"] == TODAY.isoformat()


def test_both_uploads_failing_marks_nothing(cfg, fakes):
    fakes.fail["upload:main"] = RuntimeError("no token")
    fakes.fail["upload:punch"] = RuntimeError("no token")
    before_themes, before_clips = themes_file(cfg), clip_dates(cfg)

    records = run(TODAY, cfg)

    assert {r.status for r in records} == {FAILED}
    assert themes_file(cfg) == before_themes
    assert clip_dates(cfg) == before_clips


# --- dry run ---------------------------------------------------------------


def test_dry_run_does_everything_but_upload(cfg, fakes):
    records = run(TODAY, cfg, dry_run=True)

    assert fakes.uploads == []
    assert [(r.format, r.status, r.youtube_id) for r in records] == [
        ("main", DRY_RUN, None),
        ("punch", DRY_RUN, None),
    ]
    assert (cfg.output_dir / "2026-10-01" / "main.mp4").is_file()
    assert themes_file(cfg)["gravity"] == TODAY.isoformat()
    assert {p for p, d in clip_dates(cfg).items() if d == TODAY.isoformat()} == set(CLIPS[:4])
    assert len(logged(cfg)) == 2


def test_dry_run_writes_the_upload_sheet(cfg, fakes):
    run(TODAY, cfg, dry_run=True)

    sheet = (cfg.output_dir / "2026-10-01" / UPLOAD_SHEET_NAME).read_text(encoding="utf-8")
    main_part, punch_part = sheet.split("PUNCH")

    for part, title, words in [
        (main_part, "Gravity Is a Slingshot", "Words for main."),
        (punch_part, "Use the Pull", "Words for punch."),
    ]:
        assert f"TITLE\n{title}\n" in part
        assert f"DESCRIPTION\n{words}\n\n{CREDIT_BLOCK}\n\n#Shorts\n" in part
        assert "TAGS\nspace, discipline\n" in part
        assert 'Altered or synthetic content: Yes' in part
    assert sheet.count(SYNTHETIC_REMINDER) == 2


def test_upload_sheet_lists_main_before_punch(tmp_path):
    built = [
        run_daily.Built(fmt, package({"id": "t"}, None, TODAY)["videos"][i], tmp_path / f"{fmt}.mp4", 30.0, 10)
        for i, fmt in enumerate(["main", "punch"])
    ]

    sheet = upload_sheet(built)

    assert sheet.index("MAIN") < sheet.index("PUNCH")


def test_dry_run_still_fails_closed(cfg, fakes):
    fakes.fail["render:main"] = RuntimeError("ffmpeg")

    records = run(TODAY, cfg, dry_run=True)

    assert {r.status for r in records} == {FAILED}
    assert not (cfg.output_dir / "2026-10-01" / UPLOAD_SHEET_NAME).exists()


# --- CLI -------------------------------------------------------------------


def test_cli_passes_date_and_dry_run(cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(run_daily, "load_config", lambda: cfg)
    monkeypatch.setattr(
        run_daily, "run",
        lambda today, cfg, dry_run: calls.append((today, dry_run)) or [
            RunRecord("d", "p", "main", None, None, 1.0, 1, DRY_RUN, "")
        ],
    )

    assert main(["--dry-run", "--date", "2026-10-05"]) == 0
    assert calls == [(date(2026, 10, 5), True)]


def test_cli_exits_non_zero_on_failure(cfg, monkeypatch):
    monkeypatch.setattr(run_daily, "load_config", lambda: cfg)
    monkeypatch.setattr(
        run_daily, "run",
        lambda today, cfg, dry_run: [RunRecord("d", None, "main", None, None, None, None, FAILED, "x")],
    )

    assert main([]) == 1


def test_cli_reports_bad_config(monkeypatch, capsys):
    from pipeline.config import ConfigError

    def broken():
        raise ConfigError("missing: FONT_PATH")

    monkeypatch.setattr(run_daily, "load_config", broken)

    assert main([]) == 2
    assert "FONT_PATH" in capsys.readouterr().err


# --- runlog ----------------------------------------------------------------


def record(**overrides) -> RunRecord:
    base = dict(
        date="2026-10-01", package_id="2026-10-01", format="main", youtube_id="abc",
        visibility="unlisted", duration_sec=92.4, snippet_count=31, status=UPLOADED, notes="",
    )
    return RunRecord(**(base | overrides))


def test_append_run_adds_one_line_per_call(tmp_path):
    cfg = make_cfg(tmp_path)

    append_run(record(), cfg)
    append_run(record(format="punch"), cfg)

    assert [line["format"] for line in logged(cfg)] == ["main", "punch"]


def test_notify_prints_without_a_webhook(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(runlog.requests, "post", lambda *a, **k: pytest.fail("posted"))

    notify([record()], make_cfg(tmp_path))

    out = capsys.readouterr().out
    assert "main: uploaded https://youtu.be/abc (unlisted), 92.4s, 31 clips" in out


def test_notify_posts_to_the_webhook(tmp_path, monkeypatch, capsys):
    sent = {}

    class Ok:
        def raise_for_status(self):
            pass

    def post(url, json, timeout):
        sent.update(url=url, json=json)
        return Ok()

    monkeypatch.setattr(runlog.requests, "post", post)
    cfg = make_cfg(tmp_path, notify_webhook_url="https://hooks.example.invalid/x")

    notify([record(), record(format="punch", status=FAILED, youtube_id=None, notes="upload: 403")], cfg)

    assert sent["url"] == "https://hooks.example.invalid/x"
    assert len(sent["json"]["records"]) == 2
    assert "punch: failed" in sent["json"]["text"]


def test_a_broken_webhook_does_not_raise(tmp_path, monkeypatch, caplog):
    def post(*args, **kwargs):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(runlog.requests, "post", post)
    cfg = make_cfg(tmp_path, notify_webhook_url="https://hooks.example.invalid/x")

    notify([record()], cfg)

    assert "webhook failed" in caplog.text
