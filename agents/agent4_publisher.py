"""
agents/agent4_publisher.py
Supervised Instagram Reel publish. Plain Python. No model call.

Graph API version comes from settings (default v26.0). Host is
graph.facebook.com, which is the Facebook Login for Business host.
Resumable upload is documented only for that login type.

Caption convention
    The script stage stores ``caption`` as the body without hashtags and
    keeps tags in ``hashtags``. The published caption is that body, then a
    blank line, then the hashtags that are not already in the body. The
    previous publisher sent ``script.caption`` alone; this pass appends the
    stored tags so the Reel matches the split the script stage already writes.

Nothing here is public. The video is posted to the resumable ``uri`` Meta
returns. There is no GCS upload and no public URL.

This module is not imported by the webhook. ``main.py`` imports it only
when ``--publish`` runs.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from core.config import settings
from core.logger import get_logger
from core.models import ScriptPayload, validate_job_id

logger = get_logger(__name__)

GRAPH_HOST = "https://graph.facebook.com"
RUPLOAD_PREFIX = "https://rupload.facebook.com/"
CAPTION_LIMIT = 2200
_TERMINAL_FAILURES = frozenset({"ERROR", "EXPIRED"})


class PublishRefused(RuntimeError):
    """A local gate or Meta response stopped the publish. Nothing to retry blindly."""


def compose_caption(script: ScriptPayload) -> str:
    """Join script.caption with hashtags that are not already in the body."""
    body = script.caption.strip()
    present = {tag.lower() for tag in re.findall(r"#[A-Za-z0-9_]+", body)}
    extra = [tag for tag in script.hashtags if tag.lower() not in present]
    caption = body if not extra else f"{body}\n\n{' '.join(extra)}"
    if len(caption) > CAPTION_LIMIT:
        raise PublishRefused(
            f"Refusing to publish: caption is {len(caption)} characters "
            f"and Instagram allows {CAPTION_LIMIT}."
        )
    return caption


def publish(
    job_id: str,
    *,
    confirm: bool,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any] | None:
    """
    Load one job and either preview it or post it.

    Without confirm, print the caption and the video path and return None.
    No HTTP client is used on that path. With confirm, post once and return
    the publish.json object. A second call is refused when that file already
    has a post_id.
    """
    validated = validate_job_id(job_id)
    script = _load_script(validated)
    if script.approved is not True:
        raise PublishRefused(
            f"Refusing to publish job {validated}: approved is false in script.json. "
            "Set approved to true after review. Nothing was uploaded or published."
        )

    reel_path = _reel_path(validated)
    if not reel_path.is_file():
        raise PublishRefused(
            f"Refusing to publish job {validated}: final_reel.mp4 is missing "
            f"at {reel_path}. Render with --dry-run first. Nothing was published."
        )

    existing = _existing_post_id(validated)
    if existing:
        raise PublishRefused(
            f"Refusing to publish job {validated}: publish.json already has "
            f"post_id {existing}. This job is one-shot and was not posted again."
        )

    caption = compose_caption(script)
    if not confirm:
        print("Caption:")
        print(caption)
        print()
        print("Video:")
        print(reel_path)
        print()
        print("Not published. No request was sent. Re-run with --publish --confirm-publish to post.")
        return None

    if reel_path.stat().st_size <= 0:
        raise PublishRefused(
            f"Refusing to publish job {validated}: final_reel.mp4 at {reel_path} is empty. "
            "Nothing was published."
        )

    _require_credentials()
    clock = now or (lambda: datetime.now(timezone.utc))
    owns_client = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(60.0, read=300.0))
    try:
        return _post(validated, caption, reel_path, http, sleep, clock)
    finally:
        if owns_client:
            http.close()


def _post(
    job_id: str,
    caption: str,
    reel_path: Path,
    client: httpx.Client,
    sleep: Callable[[float], None],
    now: Callable[[], datetime],
) -> dict[str, Any]:
    version = settings.graph_api_version
    ig_user_id = settings.instagram_account_id.strip()
    token = settings.instagram_access_token
    base = f"{GRAPH_HOST}/{version}"
    stamps: dict[str, str] = {}

    usage, total = _read_quota(client, base, ig_user_id, token)
    stamps["quota_checked_at"] = _stamp(now)
    if usage + 1 >= total:
        raise PublishRefused(
            "Refusing to publish: the next post would hit or exceed the "
            f"content publishing quota (quota_usage={usage}, quota_total={total}). "
            "No container was created."
        )

    container_id, upload_uri = _create_container(
        client, base, ig_user_id, token, caption
    )
    stamps["container_created_at"] = _stamp(now)
    _upload_reel(client, upload_uri, token, reel_path)
    stamps["uploaded_at"] = _stamp(now)
    _wait_until_finished(client, base, container_id, token, sleep)
    stamps["container_finished_at"] = _stamp(now)
    post_id = _publish_container(client, base, ig_user_id, token, container_id)
    stamps["published_at"] = _stamp(now)

    record = {
        "container_id": container_id,
        "post_id": post_id,
        "timestamps": stamps,
    }
    try:
        _write_record(job_id, record)
    except OSError as exc:
        raise PublishRefused(
            f"Meta published post_id {post_id} (container_id {container_id}) but "
            f"publish.json could not be written ({exc}). Do not confirm again until "
            "that post_id is recorded in publish.json."
        ) from exc
    logger.info(
        "Reel published",
        extra={"job_id": job_id, "container_id": container_id, "post_id": post_id},
    )
    return record


def _load_script(job_id: str) -> ScriptPayload:
    script_path = settings.output_dir / job_id / "script.json"
    if not script_path.is_file():
        raise FileNotFoundError(
            f"No script.json for job {job_id} under {settings.output_dir}"
        )
    return ScriptPayload.model_validate_json(script_path.read_text(encoding="utf-8"))


def _reel_path(job_id: str) -> Path:
    return settings.output_dir / job_id / "final_reel.mp4"


def _record_path(job_id: str) -> Path:
    return settings.output_dir / job_id / "publish.json"


def _existing_post_id(job_id: str) -> str:
    path = _record_path(job_id)
    if not path.is_file():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PublishRefused(
            f"Refusing to publish job {job_id}: publish.json is not valid JSON ({exc})."
        ) from exc
    if not isinstance(payload, dict):
        raise PublishRefused(
            f"Refusing to publish job {job_id}: publish.json is not an object."
        )
    post_id = str(payload.get("post_id") or "").strip()
    return post_id


def _write_record(job_id: str, record: dict[str, Any]) -> None:
    path = _record_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _require_credentials() -> None:
    if not settings.instagram_access_token.strip():
        raise PublishRefused(
            "Refusing to publish: INSTAGRAM_ACCESS_TOKEN is not set. Nothing was posted."
        )
    if not settings.instagram_account_id.strip():
        raise PublishRefused(
            "Refusing to publish: INSTAGRAM_ACCOUNT_ID is not set. Nothing was posted."
        )
    if "/" in settings.instagram_account_id or settings.instagram_account_id.strip() != settings.instagram_account_id:
        raise PublishRefused(
            "Refusing to publish: INSTAGRAM_ACCOUNT_ID must be a single IG user id."
        )


def _auth_headers(token: str, *, scheme: str) -> dict[str, str]:
    return {"Authorization": f"{scheme} {token}"}


def _read_quota(
    client: httpx.Client,
    base: str,
    ig_user_id: str,
    token: str,
) -> tuple[int, int]:
    response = client.get(
        f"{base}/{ig_user_id}/content_publishing_limit",
        params={"fields": "quota_usage,config"},
        headers=_auth_headers(token, scheme="Bearer"),
    )
    payload = _json_or_error(response, "content publishing limit")
    return _parse_quota(payload)


def _parse_quota(payload: Any) -> tuple[int, int]:
    row: Any = None
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list) and data:
            row = data[0]
        elif "quota_usage" in payload:
            row = payload
    if not isinstance(row, dict):
        raise PublishRefused(
            "Refusing to publish: content_publishing_limit did not return "
            "quota_usage and config. No container was created."
        )
    config = row.get("config")
    usage = row.get("quota_usage")
    total = config.get("quota_total") if isinstance(config, dict) else None
    if usage is None or total is None:
        raise PublishRefused(
            "Refusing to publish: content_publishing_limit omitted quota_usage "
            "or config.quota_total. The limit is not assumed. No container was created."
        )
    try:
        usage_n = int(usage)
        total_n = int(total)
    except (TypeError, ValueError) as exc:
        raise PublishRefused(
            "Refusing to publish: content_publishing_limit returned a non-numeric quota. "
            "No container was created."
        ) from exc
    return usage_n, total_n


def _create_container(
    client: httpx.Client,
    base: str,
    ig_user_id: str,
    token: str,
    caption: str,
) -> tuple[str, str]:
    response = client.post(
        f"{base}/{ig_user_id}/media",
        headers={
            **_auth_headers(token, scheme="Bearer"),
            "Content-Type": "application/json",
        },
        json={
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": caption,
            "share_to_feed": True,
            "is_ai_generated": True,
        },
    )
    payload = _json_or_error(response, "create Reel container")
    if not isinstance(payload, dict):
        raise PublishRefused("Refusing to continue: Reel container response was not an object.")
    container_id = str(payload.get("id") or "").strip()
    uri = str(payload.get("uri") or "").strip()
    if not container_id or not uri:
        raise PublishRefused(
            "Refusing to continue: Reel container response did not include id and uri."
        )
    if not uri.startswith(RUPLOAD_PREFIX):
        raise PublishRefused(
            "Refusing to continue: container uri is not on rupload.facebook.com. "
            "The video was not uploaded."
        )
    return container_id, uri


def _upload_reel(
    client: httpx.Client,
    uri: str,
    token: str,
    reel_path: Path,
) -> None:
    video = reel_path.read_bytes()
    response = client.post(
        uri,
        headers={
            "Authorization": f"OAuth {token}",
            "offset": "0",
            "file_size": str(len(video)),
        },
        content=video,
    )
    payload = _json_or_error(response, "rupload video")
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise PublishRefused(
            "Refusing to continue: rupload did not report success. The Reel was not published. "
            f"Response: {_public_body(payload)}"
        )


def _wait_until_finished(
    client: httpx.Client,
    base: str,
    container_id: str,
    token: str,
    sleep: Callable[[float], None],
) -> None:
    interval = float(settings.publish_poll_interval_seconds)
    max_wait = float(settings.publish_max_wait_seconds)
    elapsed = 0.0
    last_code = "UNKNOWN"
    last_status: Any = None
    while True:
        response = client.get(
            f"{base}/{container_id}",
            params={"fields": "status_code,status"},
            headers=_auth_headers(token, scheme="Bearer"),
        )
        payload = _json_or_error(response, "container status")
        if not isinstance(payload, dict):
            raise PublishRefused(
                f"Instagram container {container_id} status response was not an object."
            )
        last_code = str(payload.get("status_code") or "UNKNOWN")
        last_status = payload.get("status")
        logger.info(
            "Container status poll",
            extra={
                "container_id": container_id,
                "status_code": last_code,
                "elapsed_s": elapsed,
            },
        )
        if last_code == "FINISHED":
            return
        if last_code in _TERMINAL_FAILURES:
            detail = last_status if isinstance(last_status, str) and last_status.strip() else "(no status text)"
            raise PublishRefused(
                f"Instagram container {container_id} failed with status_code={last_code}: {detail}"
            )
        if elapsed + interval > max_wait:
            raise PublishRefused(
                f"Instagram container {container_id} was not FINISHED within {max_wait}s "
                f"(last status_code={last_code}, status={last_status})."
            )
        sleep(interval)
        elapsed += interval


def _publish_container(
    client: httpx.Client,
    base: str,
    ig_user_id: str,
    token: str,
    container_id: str,
) -> str:
    response = client.post(
        f"{base}/{ig_user_id}/media_publish",
        headers={
            **_auth_headers(token, scheme="Bearer"),
            "Content-Type": "application/json",
        },
        json={"creation_id": container_id},
    )
    payload = _json_or_error(response, "media_publish")
    if not isinstance(payload, dict):
        raise PublishRefused("media_publish response was not an object.")
    post_id = str(payload.get("id") or "").strip()
    if not post_id:
        raise PublishRefused("media_publish response did not include an id. publish.json was not written.")
    return post_id


def _json_or_error(response: httpx.Response, action: str) -> Any:
    body = _decode_body(response)
    error = _error_object(body)
    if response.status_code >= 400 or error:
        raise PublishRefused(_format_graph_error(action, response.status_code, error, body))
    return body


def _decode_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def _error_object(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {}
    error = body.get("error")
    if isinstance(error, dict):
        return error
    debug = body.get("debug_info")
    if isinstance(debug, dict) and isinstance(debug.get("message"), str):
        try:
            nested = json.loads(debug["message"])
        except json.JSONDecodeError:
            nested = None
        if isinstance(nested, dict) and isinstance(nested.get("error"), dict):
            return nested["error"]
    return {}


def _format_graph_error(
    action: str,
    status_code: int,
    error: dict[str, Any],
    body: Any,
) -> str:
    if error:
        return (
            f"Meta API error during {action} (HTTP {status_code}): "
            f"message={error.get('message')!r}; type={error.get('type')!r}; "
            f"code={error.get('code')!r}; error_subcode={error.get('error_subcode')!r}; "
            f"fbtrace_id={error.get('fbtrace_id')!r}"
        )
    return (
        f"Meta API error during {action} (HTTP {status_code}): "
        f"response had no error object. Body: {_public_body(body)}"
    )


def _public_body(body: Any) -> str:
    try:
        text = json.dumps(body, default=str)
    except TypeError:
        text = str(body)
    return text[:500]


def _stamp(now: Callable[[], datetime]) -> str:
    moment = now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.isoformat()
