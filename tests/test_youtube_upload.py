"""T6: YouTube upload against a mocked service; no network, no real OAuth."""

import json
from pathlib import Path

import httplib2
import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from pipeline import youtube_upload as yt
from pipeline.config import Config
from pipeline.youtube_upload import (
    MAX_RETRIES,
    MAX_TAGS_CHARS,
    SHORTS_TAG,
    UploadError,
    fit_tags,
    get_credentials,
    request_body,
    shorts_description,
    upload,
)

VIDEO = {
    "format": "punch",
    "title": "Gravity is not the enemy",
    "description": "Use the pull.\n\nVisuals: NASA public-domain mission footage.",
    "tags": ["space", "motivation", "discipline"],
    "hook_text": "Gravity is not the enemy",
    "script_blocks": [],
}


def make_cfg(tmp_path: Path) -> Config:
    return Config(
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
    )


@pytest.fixture
def cfg(tmp_path):
    return make_cfg(tmp_path)


@pytest.fixture
def video_file(tmp_path):
    path = tmp_path / "punch.mp4"
    path.write_bytes(b"\x00" * 1024)
    return path


# --- fakes -----------------------------------------------------------------


def http_error(status: int) -> HttpError:
    return HttpError(resp=httplib2.Response({"status": status}), content=b"{}")


class Progress:
    def __init__(self, fraction):
        self.fraction = fraction

    def progress(self):
        return self.fraction


class FakeRequest:
    """Plays back ``next_chunk`` outcomes: exceptions are raised, tuples returned."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def next_chunk(self):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeService:
    def __init__(self, request):
        self.request = request
        self.insert_kwargs = None

    def videos(self):
        return self

    def insert(self, **kwargs):
        self.insert_kwargs = kwargs
        return self.request


DONE = (None, {"id": "abc123XYZ"})


@pytest.fixture
def sleeps(monkeypatch):
    waits = []
    monkeypatch.setattr(yt, "_sleep", waits.append)
    return waits


def serve(monkeypatch, outcomes) -> FakeService:
    service = FakeService(FakeRequest(outcomes))
    monkeypatch.setattr(yt, "build_service", lambda cfg: service)
    return service


# --- body ------------------------------------------------------------------


def test_every_upload_declares_synthetic_media(cfg, video_file, monkeypatch, sleeps):
    service = serve(monkeypatch, [DONE])

    upload(video_file, VIDEO, cfg)

    status = service.insert_kwargs["body"]["status"]
    assert status["containsSyntheticMedia"] is True
    assert status["privacyStatus"] == "unlisted"
    assert status["selfDeclaredMadeForKids"] is False
    assert service.insert_kwargs["part"] == "snippet,status"


def test_description_ends_with_shorts_tag(cfg, video_file, monkeypatch, sleeps):
    service = serve(monkeypatch, [DONE])

    upload(video_file, VIDEO, cfg)

    snippet = service.insert_kwargs["body"]["snippet"]
    assert snippet["description"].startswith(VIDEO["description"])
    assert snippet["description"].endswith(f"\n\n{SHORTS_TAG}")
    assert snippet["title"] == VIDEO["title"]
    assert snippet["tags"] == VIDEO["tags"]


def test_shorts_tag_is_not_doubled():
    once = shorts_description("Use the pull.")

    assert shorts_description(once) == once
    assert shorts_description(once + "\n  ") == once
    assert shorts_description("") == SHORTS_TAG


def test_request_body_leaves_the_package_untouched():
    video = json.loads(json.dumps(VIDEO))

    request_body(video, "unlisted")

    assert video == VIDEO


def test_fit_tags_cleans_and_dedupes():
    assert fit_tags(["space", " Space ", "", "deep  space", "<b>rocket</b>"]) == [
        "space", "deep space", "brocket/b",
    ]


def test_fit_tags_stays_inside_the_character_budget():
    tags = [f"tag number {i:03d}" for i in range(60)]

    kept = fit_tags(tags)

    cost = sum(len(t) + 2 for t in kept) + len(kept) - 1
    assert cost <= MAX_TAGS_CHARS
    assert 0 < len(kept) < len(tags)
    assert kept == tags[: len(kept)]


# --- privacy ---------------------------------------------------------------


def test_privacy_defaults_to_unlisted_in_the_cli(tmp_path, monkeypatch):
    package = tmp_path / "package.json"
    package.write_text(json.dumps({"videos": [VIDEO]}), encoding="utf-8")
    seen = {}
    monkeypatch.setattr(yt, "load_config", lambda: make_cfg(tmp_path))
    monkeypatch.setattr(
        yt, "upload",
        lambda path, video, cfg, privacy: seen.update(video=video, privacy=privacy) or "id1",
    )

    assert yt.main(["upload", str(tmp_path / "punch.mp4"), str(package), "--format", "punch"]) == 0
    assert seen == {"video": VIDEO, "privacy": "unlisted"}

    yt.main(["upload", "x.mp4", str(package), "--format", "punch", "--privacy", "public"])
    assert seen["privacy"] == "public"


def test_cli_rejects_an_unknown_privacy(tmp_path):
    with pytest.raises(SystemExit):
        yt.main(["upload", "x.mp4", "p.json", "--format", "main", "--privacy", "secret"])


def test_cli_fails_when_the_package_lacks_the_format(tmp_path, monkeypatch):
    package = tmp_path / "package.json"
    package.write_text(json.dumps({"videos": [VIDEO]}), encoding="utf-8")
    monkeypatch.setattr(yt, "load_config", lambda: make_cfg(tmp_path))

    with pytest.raises(UploadError, match="no 'main' video"):
        yt.main(["upload", "x.mp4", str(package), "--format", "main"])


def test_upload_rejects_an_unknown_privacy(cfg, video_file):
    with pytest.raises(UploadError, match="privacy"):
        upload(video_file, VIDEO, cfg, privacy="friends")


def test_upload_rejects_a_missing_file(cfg, tmp_path):
    with pytest.raises(UploadError, match="missing"):
        upload(tmp_path / "nope.mp4", VIDEO, cfg)


# --- resumable upload and retries ------------------------------------------


def test_upload_returns_the_video_id_after_several_chunks(cfg, video_file, monkeypatch, sleeps):
    service = serve(monkeypatch, [(Progress(0.3), None), (Progress(0.7), None), DONE])

    assert upload(video_file, VIDEO, cfg) == "abc123XYZ"
    assert service.request.calls == 3
    assert sleeps == []


def test_media_is_resumable(cfg, video_file, monkeypatch, sleeps):
    service = serve(monkeypatch, [DONE])

    upload(video_file, VIDEO, cfg)

    media = service.insert_kwargs["media_body"]
    assert media.resumable()
    assert media.mimetype() == "video/mp4"


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_is_retried(cfg, video_file, monkeypatch, sleeps, status):
    service = serve(monkeypatch, [http_error(status), http_error(status), DONE])

    assert upload(video_file, VIDEO, cfg) == "abc123XYZ"
    assert service.request.calls == 3
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0] - 1


def test_dropped_connection_is_retried(cfg, video_file, monkeypatch, sleeps):
    serve(monkeypatch, [ConnectionResetError("reset"), httplib2.HttpLib2Error("gone"), DONE])

    assert upload(video_file, VIDEO, cfg) == "abc123XYZ"
    assert len(sleeps) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_4xx_is_not_retried(cfg, video_file, monkeypatch, sleeps, status):
    service = serve(monkeypatch, [http_error(status), DONE])

    with pytest.raises(UploadError, match=f"HTTP {status}"):
        upload(video_file, VIDEO, cfg)
    assert service.request.calls == 1
    assert sleeps == []


def test_retries_give_up_eventually(cfg, video_file, monkeypatch, sleeps):
    service = serve(monkeypatch, [http_error(503)] * (MAX_RETRIES + 1))

    with pytest.raises(UploadError, match="retries"):
        upload(video_file, VIDEO, cfg)
    assert service.request.calls == MAX_RETRIES + 1
    assert len(sleeps) == MAX_RETRIES


def test_progress_resets_the_retry_budget(cfg, video_file, monkeypatch, sleeps):
    flaky = [http_error(503)] * MAX_RETRIES
    serve(monkeypatch, flaky + [(Progress(0.5), None)] + flaky + [DONE])

    assert upload(video_file, VIDEO, cfg) == "abc123XYZ"


def test_a_response_without_an_id_fails(cfg, video_file, monkeypatch, sleeps):
    serve(monkeypatch, [(None, {"kind": "youtube#video"})])

    with pytest.raises(UploadError, match="no video id"):
        upload(video_file, VIDEO, cfg)


# --- credentials -----------------------------------------------------------


class FakeCreds:
    def __init__(self, *, valid=True, expired=False, refresh_token="r", refresh_fails=False):
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.refresh_fails = refresh_fails
        self.refreshed = False

    def refresh(self, request):
        if self.refresh_fails:
            raise RefreshError("revoked")
        self.refreshed = True
        self.valid = True

    def to_json(self):
        return json.dumps({"token": "fresh"})


class FakeFlow:
    def __init__(self, creds):
        self.creds = creds
        self.ran = False

    def run_local_server(self, port):
        self.ran = True
        return self.creds


@pytest.fixture
def cached(cfg, monkeypatch):
    """Put a token file on disk and control what loading it yields."""
    cfg.youtube_token.write_text("{}", encoding="utf-8")

    def install(creds):
        monkeypatch.setattr(
            yt.Credentials, "from_authorized_user_file",
            staticmethod(lambda path, scopes: creds),
        )
        return creds

    return install


@pytest.fixture
def flow(cfg, monkeypatch):
    made = FakeFlow(FakeCreds())
    monkeypatch.setattr(
        yt.InstalledAppFlow, "from_client_secrets_file",
        staticmethod(lambda path, scopes: made),
    )
    return made


def test_a_valid_cached_token_is_used_as_is(cfg, cached, flow):
    creds = cached(FakeCreds())

    assert get_credentials(cfg) is creds
    assert not flow.ran
    assert cfg.youtube_token.read_text() == "{}"


def test_an_expired_token_is_refreshed_and_recached(cfg, cached, flow):
    creds = cached(FakeCreds(valid=False, expired=True))

    assert get_credentials(cfg) is creds
    assert creds.refreshed
    assert json.loads(cfg.youtube_token.read_text()) == {"token": "fresh"}
    assert not flow.ran


def test_the_daily_run_never_opens_a_browser(cfg, flow):
    with pytest.raises(UploadError, match="pipeline.youtube_upload auth"):
        get_credentials(cfg)
    assert not flow.ran


def test_a_revoked_token_fails_the_daily_run(cfg, cached, flow):
    cached(FakeCreds(valid=False, expired=True, refresh_fails=True))

    with pytest.raises(UploadError, match="could not be refreshed"):
        get_credentials(cfg)
    assert not flow.ran


def test_interactive_auth_runs_consent_and_caches_the_token(cfg, flow):
    cfg.youtube_client_secrets.write_text("{}", encoding="utf-8")

    assert get_credentials(cfg, interactive=True) is flow.creds
    assert flow.ran
    assert json.loads(cfg.youtube_token.read_text()) == {"token": "fresh"}


def test_interactive_auth_recovers_from_a_revoked_token(cfg, cached, flow):
    cfg.youtube_client_secrets.write_text("{}", encoding="utf-8")
    cached(FakeCreds(valid=False, expired=True, refresh_fails=True))

    assert get_credentials(cfg, interactive=True) is flow.creds


def test_interactive_auth_needs_the_client_file(cfg, flow):
    with pytest.raises(UploadError, match="OAuth client file missing"):
        get_credentials(cfg, interactive=True)


def test_upload_scope_only():
    assert yt.SCOPES == ["https://www.googleapis.com/auth/youtube.upload"]
