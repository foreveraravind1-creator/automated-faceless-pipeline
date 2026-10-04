"""Pass 3: supervised publish. Mocked HTTP only. Never posts."""
from __future__ import annotations

import ast
import inspect
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from agents.agent4_publisher import PublishRefused, compose_caption, publish
from core.config import Settings, settings
from core.models import ScriptPayload
from main import main

ROOT = Path(__file__).resolve().parents[1]
WEBHOOK = ROOT / "webhook" / "server.py"
IG = "17841400000000000"
TOKEN = "test-token"
VIDEO = b"fake-mp4-bytes"
FIXED = datetime(2026, 10, 4, 16, 0, tzinfo=timezone.utc)


def _script(approved: bool, **overrides) -> dict:
    payload = {
        "title": "Why the sky looks blue",
        "hook": "The sky is not a painted ceiling.",
        "on_screen_hook": "The sky is not blue paint",
        "narration": "Sunlight looks white, but air scatters the blue waves.",
        "pexels_query": "blue sky clouds",
        "caption": "A clear sky is scattered sunlight.",
        "hashtags": ["#science", "#sciencefacts"],
        "claims_to_verify": ["Air scatters blue light more than red light."],
        "approved": approved,
        "topic": "Why is the sky blue?",
        "niche": "everyday-science",
        "language": "en",
        "duration_seconds": 45,
        "voice": "af_heart",
        "kokoro_lang": "a",
    }
    payload.update(overrides)
    return payload


def _write_job(directory: Path, job_id: str, *, approved: bool, reel: bool = True) -> Path:
    job = directory / job_id
    job.mkdir(parents=True, exist_ok=True)
    (job / "script.json").write_text(json.dumps(_script(approved)), encoding="utf-8")
    if reel:
        (job / "final_reel.mp4").write_bytes(VIDEO)
    return job


def _prepare(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "output_dir", tmp_path)
    monkeypatch.setattr(settings, "instagram_access_token", TOKEN)
    monkeypatch.setattr(settings, "instagram_account_id", IG)
    monkeypatch.setattr(settings, "graph_api_version", "v26.0")
    monkeypatch.setattr(settings, "publish_poll_interval_seconds", 60)
    monkeypatch.setattr(settings, "publish_max_wait_seconds", 600)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _quota(usage: int, total: int = 50) -> dict:
    return {
        "data": [
            {
                "quota_usage": usage,
                "config": {"quota_total": total, "quota_duration": 86400},
            }
        ]
    }


def _path(request: httpx.Request) -> str:
    return request.url.path


def test_publish_defaults_match_current_graph_version():
    assert Settings.model_fields["graph_api_version"].default == "v26.0"
    assert Settings.model_fields["publish_poll_interval_seconds"].default == 60
    assert Settings.model_fields["publish_max_wait_seconds"].default == 600


def test_caption_appends_hashtags_not_already_in_the_body():
    script = ScriptPayload.model_validate(
        _script(True, caption="A clear sky is scattered sunlight. #science")
    )
    assert compose_caption(script) == "A clear sky is scattered sunlight. #science\n\n#sciencefacts"
    overlap = ScriptPayload.model_validate(
        _script(True, caption="See #sciencefacts", hashtags=["#science", "#sciencefacts"])
    )
    assert compose_caption(overlap) == "See #sciencefacts\n\n#science"


def test_refuses_when_approved_is_false(tmp_path: Path, monkeypatch):
    _prepare(monkeypatch, tmp_path)
    _write_job(tmp_path, "job-no", approved=False)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(_path(request))
        raise AssertionError("approved false must not call Meta")

    with pytest.raises(PublishRefused, match="approved is false"):
        publish("job-no", confirm=True, client=_client(handler))
    assert calls == []


def test_refuses_when_final_reel_is_missing(tmp_path: Path, monkeypatch):
    _prepare(monkeypatch, tmp_path)
    _write_job(tmp_path, "job-miss", approved=True, reel=False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("missing reel must not call Meta")

    with pytest.raises(PublishRefused, match="final_reel.mp4 is missing"):
        publish("job-miss", confirm=True, client=_client(handler))


def test_preview_prints_caption_and_path_with_zero_http_calls(
    tmp_path: Path, monkeypatch, capsys
):
    _prepare(monkeypatch, tmp_path)
    _write_job(tmp_path, "job-prev", approved=True)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("preview must not call Meta")

    result = publish("job-prev", confirm=False, client=_client(handler))
    printed = capsys.readouterr().out
    assert result is None
    assert "A clear sky is scattered sunlight." in printed
    assert "#science #sciencefacts" in printed
    assert str(tmp_path / "job-prev" / "final_reel.mp4") in printed
    assert "Not published" in printed
    assert not (tmp_path / "job-prev" / "publish.json").exists()


def test_quota_reached_does_not_create_a_container(tmp_path: Path, monkeypatch):
    _prepare(monkeypatch, tmp_path)
    _write_job(tmp_path, "job-quota", approved=True)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(_path(request))
        assert request.method == "GET"
        assert request.url.params["fields"] == "quota_usage,config"
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(200, json=_quota(usage=49, total=50))

    with pytest.raises(PublishRefused, match="quota_usage=49, quota_total=50"):
        publish("job-quota", confirm=True, client=_client(handler), now=lambda: FIXED)
    assert calls == [f"/v26.0/{IG}/content_publishing_limit"]
    assert not (tmp_path / "job-quota" / "publish.json").exists()


def test_quota_already_over_total_does_not_create_a_container(tmp_path: Path, monkeypatch):
    _prepare(monkeypatch, tmp_path)
    _write_job(tmp_path, "job-over", approved=True)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(_path(request))
        return httpx.Response(200, json=_quota(usage=50, total=50))

    with pytest.raises(PublishRefused, match="quota_total=50"):
        publish("job-over", confirm=True, client=_client(handler))
    assert len(calls) == 1
    assert calls[0].endswith("/content_publishing_limit")


def test_existing_post_id_refuses_before_any_http(tmp_path: Path, monkeypatch):
    _prepare(monkeypatch, tmp_path)
    job = _write_job(tmp_path, "job-done", approved=True)
    (job / "publish.json").write_text(
        json.dumps(
            {
                "container_id": "cont-old",
                "post_id": "17890000000000001",
                "timestamps": {"published_at": "2026-10-01T00:00:00+00:00"},
            }
        ),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("existing post_id must not call Meta")

    with pytest.raises(PublishRefused, match="17890000000000001"):
        publish("job-done", confirm=True, client=_client(handler))


def test_container_error_status_surfaces_status_text(tmp_path: Path, monkeypatch):
    _prepare(monkeypatch, tmp_path)
    _write_job(tmp_path, "job-err", approved=True)
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, _path(request)))
        path = _path(request)
        if path.endswith("/content_publishing_limit"):
            return httpx.Response(200, json=_quota(0))
        if path.endswith("/media"):
            return httpx.Response(
                200,
                json={
                    "id": "cont-err",
                    "uri": "https://rupload.facebook.com/ig-api-upload/v26.0/cont-err",
                },
            )
        if request.url.host == "rupload.facebook.com":
            return httpx.Response(200, json={"success": True, "message": "Upload successful."})
        if path.endswith("/cont-err"):
            assert request.url.params["fields"] == "status_code,status"
            return httpx.Response(
                200,
                json={
                    "status_code": "ERROR",
                    "status": "Video codec is not supported",
                },
            )
        raise AssertionError(f"publish must not run after ERROR: {request.method} {path}")

    with pytest.raises(PublishRefused, match="Video codec is not supported"):
        publish("job-err", confirm=True, client=_client(handler), now=lambda: FIXED)
    assert [path for _, path in calls][-1].endswith("/cont-err")
    assert not any(path.endswith("/media_publish") for _, path in calls)
    assert not (tmp_path / "job-err" / "publish.json").exists()


def test_graph_error_json_is_surfaced(tmp_path: Path, monkeypatch):
    _prepare(monkeypatch, tmp_path)
    _write_job(tmp_path, "job-graph", approved=True)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(_path(request))
        if _path(request).endswith("/content_publishing_limit"):
            return httpx.Response(200, json=_quota(0))
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "Invalid OAuth access token.",
                    "type": "OAuthException",
                    "code": 190,
                    "error_subcode": 463,
                    "fbtrace_id": "AbCdEf123",
                }
            },
        )

    with pytest.raises(PublishRefused) as caught:
        publish("job-graph", confirm=True, client=_client(handler))
    text = str(caught.value)
    assert "Invalid OAuth access token." in text
    assert "OAuthException" in text
    assert "190" in text
    assert "463" in text
    assert "AbCdEf123" in text
    assert "error_subcode" in text
    assert "fbtrace_id" in text
    assert calls == [
        f"/v26.0/{IG}/content_publishing_limit",
        f"/v26.0/{IG}/media",
    ]


def test_webhook_does_not_import_publisher():
    source = WEBHOOK.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported.append(module)
            imported.extend(f"{module}.{alias.name}" for alias in node.names)
    assert "agents.agent4_publisher" not in imported
    assert "agent4_publisher" not in source
    import webhook.server as server

    assert "agent4_publisher" not in inspect.getsource(server)
    assert all(
        getattr(value, "__name__", "") != "agents.agent4_publisher"
        for value in vars(server).values()
    )


def test_happy_path_order_and_ai_disclosure(tmp_path: Path, monkeypatch):
    _prepare(monkeypatch, tmp_path)
    _write_job(tmp_path, "job-ok", approved=True)
    calls: list[httpx.Request] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = _path(request)
        if path.endswith("/content_publishing_limit"):
            assert request.method == "GET"
            assert request.url.params["fields"] == "quota_usage,config"
            assert request.headers["authorization"] == f"Bearer {TOKEN}"
            return httpx.Response(200, json=_quota(usage=48, total=50))
        if path.endswith("/media"):
            body = json.loads(request.content)
            assert body == {
                "media_type": "REELS",
                "upload_type": "resumable",
                "caption": "A clear sky is scattered sunlight.\n\n#science #sciencefacts",
                "share_to_feed": True,
                "is_ai_generated": True,
            }
            assert "video_url" not in body
            assert request.headers["authorization"] == f"Bearer {TOKEN}"
            return httpx.Response(
                200,
                json={
                    "id": "cont-1",
                    "uri": "https://rupload.facebook.com/ig-api-upload/v26.0/cont-1",
                },
            )
        if request.url.host == "rupload.facebook.com":
            assert request.method == "POST"
            assert request.url.path == "/ig-api-upload/v26.0/cont-1"
            assert request.headers["authorization"] == f"OAuth {TOKEN}"
            assert request.headers["offset"] == "0"
            assert request.headers["file_size"] == str(len(VIDEO))
            assert request.content == VIDEO
            return httpx.Response(200, json={"success": True, "message": "Upload successful."})
        if path.endswith("/cont-1"):
            assert request.method == "GET"
            assert request.url.params["fields"] == "status_code,status"
            return httpx.Response(
                200,
                json={"id": "cont-1", "status_code": "FINISHED", "status": "Finished."},
            )
        if path.endswith("/media_publish"):
            assert json.loads(request.content) == {"creation_id": "cont-1"}
            assert request.headers["authorization"] == f"Bearer {TOKEN}"
            return httpx.Response(200, json={"id": "post-1"})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    record = publish(
        "job-ok",
        confirm=True,
        client=_client(handler),
        sleep=lambda seconds: sleeps.append(seconds),
        now=lambda: FIXED,
    )
    assert sleeps == []
    assert [(request.method, request.url.host, _path(request)) for request in calls] == [
        ("GET", "graph.facebook.com", f"/v26.0/{IG}/content_publishing_limit"),
        ("POST", "graph.facebook.com", f"/v26.0/{IG}/media"),
        ("POST", "rupload.facebook.com", "/ig-api-upload/v26.0/cont-1"),
        ("GET", "graph.facebook.com", "/v26.0/cont-1"),
        ("POST", "graph.facebook.com", f"/v26.0/{IG}/media_publish"),
    ]
    saved = json.loads((tmp_path / "job-ok" / "publish.json").read_text(encoding="utf-8"))
    assert saved == record
    assert saved["container_id"] == "cont-1"
    assert saved["post_id"] == "post-1"
    assert saved["timestamps"]["published_at"] == FIXED.isoformat()
    assert set(saved["timestamps"]) == {
        "quota_checked_at",
        "container_created_at",
        "uploaded_at",
        "container_finished_at",
        "published_at",
    }

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("second publish must not call Meta")

    with pytest.raises(PublishRefused, match="post-1"):
        publish("job-ok", confirm=True, client=_client(refuse))


def test_poll_uses_configured_interval_and_stops_on_expired(tmp_path: Path, monkeypatch):
    _prepare(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "publish_poll_interval_seconds", 60)
    monkeypatch.setattr(settings, "publish_max_wait_seconds", 180)
    _write_job(tmp_path, "job-poll", approved=True)
    sleeps: list[float] = []
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = _path(request)
        if path.endswith("/content_publishing_limit"):
            return httpx.Response(200, json=_quota(0))
        if path.endswith("/media"):
            return httpx.Response(
                200,
                json={
                    "id": "cont-poll",
                    "uri": "https://rupload.facebook.com/ig-api-upload/v26.0/cont-poll",
                },
            )
        if request.url.host == "rupload.facebook.com":
            return httpx.Response(200, json={"success": True, "message": "Upload successful."})
        polls["n"] += 1
        if polls["n"] == 1:
            return httpx.Response(200, json={"status_code": "IN_PROGRESS", "status": "Processing."})
        return httpx.Response(
            200,
            json={"status_code": "EXPIRED", "status": "Container expired before publish."},
        )

    with pytest.raises(PublishRefused, match="Container expired before publish."):
        publish(
            "job-poll",
            confirm=True,
            client=_client(handler),
            sleep=lambda seconds: sleeps.append(seconds),
        )
    assert sleeps == [60]
    assert not (tmp_path / "job-poll" / "publish.json").exists()


def test_cli_preview_and_approved_false(tmp_path: Path):
    _write_job(tmp_path, "cli-ok", approved=True)
    _write_job(tmp_path, "cli-no", approved=False)
    env = os.environ.copy()
    env["OUTPUT_DIR"] = str(tmp_path)
    env["INSTAGRAM_ACCESS_TOKEN"] = ""
    env["INSTAGRAM_ACCOUNT_ID"] = ""

    preview = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--publish", "--job-id", "cli-ok"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert preview.returncode == 0
    assert "A clear sky is scattered sunlight." in preview.stdout
    assert str(tmp_path / "cli-ok" / "final_reel.mp4") in preview.stdout
    assert not (tmp_path / "cli-ok" / "publish.json").exists()

    refused = subprocess.run(
        [
            sys.executable,
            str(ROOT / "main.py"),
            "--publish",
            "--job-id",
            "cli-no",
            "--confirm-publish",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert refused.returncode == 2
    assert "approved is false" in refused.stderr


def test_cli_preview_loads_publisher_only_for_publish(tmp_path: Path, monkeypatch):
    _prepare(monkeypatch, tmp_path)
    _write_job(tmp_path, "job-cli", approved=True)
    sys.modules.pop("agents.agent4_publisher", None)
    code = main(["--publish", "--job-id", "job-cli"])
    assert code == 0
    assert "agents.agent4_publisher" in sys.modules
