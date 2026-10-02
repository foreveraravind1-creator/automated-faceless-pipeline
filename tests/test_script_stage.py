"""Pass 1: one mocked Gemini call, unapproved script, no render or publish."""
from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import webhook.server as server
from agents import agent1_researcher
from core.config import Settings, settings
from core.models import ScriptPayload, validate_job_id
from core.niche import load_niche
from main import run_script

ROOT = Path(__file__).resolve().parents[1]
NICHE = ROOT / "niches" / "example.json"

SAMPLE = {
    "title": "Why the sky looks blue",
    "hook": "The sky is not a painted ceiling. It is sunlight bouncing off air.",
    "on_screen_hook": "The sky is not blue paint",
    "narration": (
        "Sunlight looks white, but it is a mix of colors. "
        "Air molecules scatter the shorter blue waves more than the red ones, "
        "so the light that reaches your eye from every direction is mostly blue. "
        "That is why a clear daytime sky looks blue."
    ),
    "pexels_query": "blue sky clouds",
    "caption": "A clear sky is scattered sunlight. Save this for the next sunny walk.",
    "hashtags": ["#science", "#sciencefacts", "#didyouknow", "#everydayScience"],
    "claims_to_verify": [
        "Air molecules scatter shorter blue wavelengths more than longer red ones."
    ],
}


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeModels:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return _Response(json.dumps(SAMPLE))


class FakeClient:
    def __init__(self) -> None:
        self.models = FakeModels()
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


def test_gemini_model_default_and_optional_later_settings():
    assert Settings.model_fields["gemini_model"].default == "gemini-3.8-flash"
    for name in (
        "google_api_key",
        "pexels_api_key",
        "instagram_access_token",
        "instagram_account_id",
    ):
        assert Settings.model_fields[name].default == ""


def test_example_niche_loads():
    niche = load_niche(NICHE)
    assert niche.name == "everyday-science"
    assert niche.duration_seconds == 45
    assert niche.hashtag_pool[0].startswith("#")


def test_niche_rejects_unknown_field(tmp_path: Path):
    payload = json.loads(NICHE.read_text(encoding="utf-8"))
    payload["series"] = "not-a-profile-field"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_niche(path)


def test_script_stage_one_call_and_unapproved(tmp_path: Path, fake_client: FakeClient, monkeypatch):
    monkeypatch.setattr(settings, "output_dir", tmp_path)
    monkeypatch.setattr(settings, "gemini_model", "gemini-3.8-flash")

    state = run_script(
        topic="Why is the sky blue?",
        niche_path=NICHE,
        job_id="sky-001",
        client=fake_client,
    )

    assert len(fake_client.models.calls) == 1
    call = fake_client.models.calls[0]
    assert call["model"] == "gemini-3.8-flash"
    assert "Why is the sky blue?" in call["contents"]
    assert "everyday-science" in call["contents"]
    assert call["config"]["response_mime_type"] == "application/json"
    schema_props = call["config"]["response_json_schema"]["properties"]
    assert "approved" not in schema_props
    for field in (
        "title",
        "hook",
        "on_screen_hook",
        "narration",
        "pexels_query",
        "caption",
        "hashtags",
        "claims_to_verify",
    ):
        assert field in schema_props
    assert fake_client.closed is False

    script_path = tmp_path / "sky-001" / "script.json"
    saved = json.loads(script_path.read_text(encoding="utf-8"))
    assert saved["approved"] is False
    assert saved["pexels_query"] == "blue sky clouds"
    assert saved["caption"].startswith("A clear sky")
    assert saved["niche"] == "everyday-science"
    assert saved["topic"] == "Why is the sky blue?"
    assert saved["voice"] == "af_heart"
    assert saved["kokoro_lang"] == "a"
    loaded = ScriptPayload.model_validate(saved)
    assert loaded.approved is False
    assert state.script is not None
    assert state.script.approved is False


def _saved_script(**overrides) -> dict:
    payload = {
        **SAMPLE,
        "topic": "Why is the sky blue?",
        "niche": "everyday-science",
        "language": "en",
        "duration_seconds": 45,
        "approved": False,
    }
    payload.update(overrides)
    return payload


def test_script_json_approved_true_loads(tmp_path: Path):
    path = tmp_path / "script.json"
    path.write_text(json.dumps(_saved_script(approved=True)), encoding="utf-8")
    loaded = ScriptPayload.model_validate_json(path.read_text(encoding="utf-8"))
    assert loaded.approved is True


def test_writer_stamps_false_when_model_returns_approved_true(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(settings, "output_dir", tmp_path)
    client = FakeClient()
    client.models.generate_content = _returning_approved_true(client.models)

    state = run_script(
        topic="Why is the sky blue?",
        niche_path=NICHE,
        job_id="sky-true",
        client=client,
    )

    assert len(client.models.calls) == 1
    assert "approved" not in client.models.calls[0]["config"]["response_json_schema"]["properties"]
    saved = json.loads((tmp_path / "sky-true" / "script.json").read_text(encoding="utf-8"))
    assert saved["approved"] is False
    assert state.script is not None
    assert state.script.approved is False


def _returning_approved_true(models: FakeModels):
    def generate_content(**kwargs):
        models.calls.append(kwargs)
        return _Response(json.dumps({**SAMPLE, "approved": True}))

    return generate_content


def test_missing_gemini_key_raises_before_network(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "output_dir", tmp_path)
    monkeypatch.setattr(settings, "gemini_api_key", "")
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        run_script(topic="Why is the sky blue?", niche_path=NICHE, job_id="sky-002")


@pytest.mark.parametrize(
    "job_id",
    ["", "A", "has space", "under_score", "../etc", "..", "a/b", "a" * 33, "job.id"],
)
def test_job_id_validation(job_id: str):
    with pytest.raises(ValueError, match=r"\^\[a-z0-9-\]\{1,32\}\$"):
        validate_job_id(job_id)


@pytest.mark.parametrize("job_id", ["a", "sky-001", "a" * 32])
def test_job_id_accepts_safe_ids(job_id: str):
    assert validate_job_id(job_id) == job_id


def test_bad_job_id_does_not_escape_output_dir(tmp_path: Path, fake_client: FakeClient, monkeypatch):
    monkeypatch.setattr(settings, "output_dir", tmp_path)
    with pytest.raises(ValueError):
        run_script(
            topic="Why is the sky blue?",
            niche_path=NICHE,
            job_id="../etc",
            client=fake_client,
        )
    assert fake_client.models.calls == []
    assert not (tmp_path.parent / "etc").exists()
    assert list(tmp_path.iterdir()) == []


def test_run_endpoints_do_not_import_or_call_render_or_publish(tmp_path: Path, monkeypatch):
    legacy_editor = "movie" + "py"
    for name in (
        "agents.agent2_media",
        "agents.agent3_editor",
        "agents.agent4_publisher",
        legacy_editor,
        legacy_editor + ".editor",
    ):
        sys.modules.pop(name, None)
        assert name not in sys.modules

    source = inspect.getsource(server)
    main_source = inspect.getsource(sys.modules["main"])
    for forbidden in ("BackgroundTasks", "agent2_media", "agent3_editor", "agent4_publisher"):
        assert forbidden not in source
    assert "agent4_publisher" not in main_source
    assert "BackgroundTasks" not in main_source

    fake = FakeClient()
    monkeypatch.setattr(settings, "output_dir", tmp_path)
    monkeypatch.setattr(agent1_researcher, "_default_client", lambda: fake)

    client = TestClient(server.app)
    for path, job_id in (("/run", "job-1"), ("/run-async", "job-2")):
        before = len(fake.models.calls)
        response = client.post(
            path,
            json={"topic": "Why is the sky blue?", "niche": str(NICHE), "job_id": job_id},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "script_ready"
        assert body["approved"] is False
        assert body["job_id"] == job_id
        assert len(fake.models.calls) == before + 1
        saved = json.loads((tmp_path / job_id / "script.json").read_text(encoding="utf-8"))
        assert saved["approved"] is False

    refused = client.post(
        "/run",
        json={"topic": "Why is the sky blue?", "niche": str(NICHE), "job_id": "../etc"},
    )
    assert refused.status_code == 400
    assert len(fake.models.calls) == 2

    legacy_editor = "movie" + "py"
    for name in (
        "agents.agent2_media",
        "agents.agent3_editor",
        "agents.agent4_publisher",
        legacy_editor,
        legacy_editor + ".editor",
    ):
        assert name not in sys.modules


def test_cli_refuses_publish():
    result = subprocess.run(
        [sys.executable, "main.py", "--topic", "sky", "--niche", str(NICHE), "--publish"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "not implemented" in result.stderr.lower()
