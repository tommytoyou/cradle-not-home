"""T1a: load_config reads .env, applies defaults, and reports every gap at once."""

import pytest

from pipeline.config import ROOT, Config, ConfigError, load_config

FULL_ENV = {
    "LLM_API_KEY": "llm-key",
    "LLM_BASE_URL": "https://example.invalid/v1",
    "LLM_MODEL": "some-model",
    "ELEVENLABS_API_KEY": "eleven-key",
    "ELEVENLABS_VOICE_ID": "voice-123",
    "YOUTUBE_CLIENT_SECRETS": "client_secret.json",
    "YOUTUBE_TOKEN": "token.json",
    "LIBRARY_DIR": "library",
    "MUSIC_DIR": "music",
    "OUTPUT_DIR": "output",
    "DATA_DIR": "data",
    "FONT_PATH": "/fonts/bold.ttf",
}


def test_loads_every_field_and_applies_defaults(tmp_path):
    cfg = load_config(env_file=tmp_path / "absent.env", environ=FULL_ENV)

    assert isinstance(cfg, Config)
    assert cfg.llm_model == "some-model"
    assert cfg.elevenlabs_voice_id == "voice-123"
    assert cfg.min_library_clips == 200
    assert cfg.clip_cooldown_days == 3
    assert cfg.notify_webhook_url is None


def test_relative_paths_resolve_against_repo_root_absolute_ones_do_not(tmp_path):
    absolute_font = tmp_path / "bold.ttf"
    assert absolute_font.is_absolute()  # guard: the point of the test

    environ = FULL_ENV | {"FONT_PATH": str(absolute_font)}
    cfg = load_config(env_file=tmp_path / "absent.env", environ=environ)

    assert cfg.library_dir == ROOT / "library"
    assert cfg.data_dir == ROOT / "data"
    assert cfg.youtube_token == ROOT / "token.json"
    assert cfg.font_path == absolute_font


def test_config_is_frozen(tmp_path):
    cfg = load_config(env_file=tmp_path / "absent.env", environ=FULL_ENV)

    with pytest.raises(Exception):
        cfg.llm_api_key = "other"  # type: ignore[misc]


def test_optional_values_override_defaults(tmp_path):
    environ = FULL_ENV | {
        "MIN_LIBRARY_CLIPS": "250",
        "CLIP_COOLDOWN_DAYS": "7",
        "NOTIFY_WEBHOOK_URL": "https://hooks.example.invalid/abc",
    }

    cfg = load_config(env_file=tmp_path / "absent.env", environ=environ)

    assert cfg.min_library_clips == 250
    assert cfg.clip_cooldown_days == 7
    assert cfg.notify_webhook_url == "https://hooks.example.invalid/abc"


def test_env_file_supplies_values_and_environ_wins(tmp_path):
    env_file = tmp_path / ".env"
    lines = [f"{k}={v}" for k, v in FULL_ENV.items()]
    lines.append("MIN_LIBRARY_CLIPS=111")
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    cfg = load_config(env_file=env_file, environ={"ELEVENLABS_VOICE_ID": "from-environ"})

    assert cfg.llm_api_key == "llm-key"
    assert cfg.min_library_clips == 111
    assert cfg.elevenlabs_voice_id == "from-environ"


def test_error_lists_every_missing_var(tmp_path):
    environ = {k: v for k, v in FULL_ENV.items() if k not in {"LLM_MODEL", "FONT_PATH"}}
    environ["ELEVENLABS_API_KEY"] = "   "  # blank counts as missing

    with pytest.raises(ConfigError) as excinfo:
        load_config(env_file=tmp_path / "absent.env", environ=environ)

    message = str(excinfo.value)
    for key in ("LLM_MODEL", "FONT_PATH", "ELEVENLABS_API_KEY"):
        assert key in message
    assert "LLM_API_KEY" not in message.split("missing or empty:")[1]


def test_error_on_non_numeric_optional_int(tmp_path):
    environ = FULL_ENV | {"CLIP_COOLDOWN_DAYS": "three"}

    with pytest.raises(ConfigError, match="CLIP_COOLDOWN_DAYS"):
        load_config(env_file=tmp_path / "absent.env", environ=environ)


def test_env_example_documents_every_variable():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    documented = {
        line.split("=", 1)[0]
        for line in text.splitlines()
        if line and not line.startswith("#") and "=" in line
    }

    expected = set(FULL_ENV) | {
        "MIN_LIBRARY_CLIPS",
        "CLIP_COOLDOWN_DAYS",
        "NOTIFY_WEBHOOK_URL",
    }
    assert documented == expected
