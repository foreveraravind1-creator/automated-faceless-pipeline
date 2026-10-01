"""
agents/agent4_publisher.py
Agent 4 - Publisher
Model: gemini-2.5-flash  -  used ONLY for caption generation (legitimate LLM work)
APIs : Google Cloud Storage (V4 Signed URL), Meta Graph API v20.0

GCS strategy: V4 Signed URL (30-minute TTL)
  - Works under Uniform Bucket-Level Access (UBLA) - no object ACLs needed.
  - Local dev  : GOOGLE_APPLICATION_CREDENTIALS -> service-account key JSON
                 (key's private key signs directly).
  - Cloud Run  : Workload Identity -> metadata server provides SA email + token;
                 generate_signed_url calls IAM SignBlob API automatically.
                 Requirement: Cloud Run SA needs roles/iam.serviceAccountTokenCreator
                 granted on itself (see README.md).

Input  : JobState.final_reel_path, JobState.script
Output : JobState.gcs_public_url (signed URL), JobState.instagram_post_id
"""
import datetime
import time
import urllib.request
from pathlib import Path

import google.auth
import google.auth.transport.requests
import google.generativeai as genai
import httpx
from google.api_core import exceptions as gcp_exceptions
from google.cloud import storage

from core.config import settings
from core.logger import get_logger
from core.models import JobState

logger = get_logger(__name__)

META_BASE = "https://graph.facebook.com/v20.0"
SIGNED_URL_TTL_MINUTES = 30  # Meta fetches the video within seconds; 30 min is ample


# ---------------------------------------------------------------------------
# GCS helpers - V4 Signed URL (UBLA-compatible)
# ---------------------------------------------------------------------------

def _get_sa_email_from_metadata() -> str:
    """Fetch the default service account email from the GCE metadata server."""
    url = (
        "http://metadata.google.internal/computeMetadata/v1"
        "/instance/service-accounts/default/email"
    )
    req = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.read().decode("utf-8")


def _upload_to_gcs_signed(file_path: Path) -> str:
    """
    Upload the final reel to GCS.
    Returns a V4 Signed URL valid for SIGNED_URL_TTL_MINUTES minutes.

    This approach is UBLA-compatible because it uses signed URLs rather than
    object-level ACLs. The bucket can remain fully private; Meta's servers
    download the video via the time-limited signed URL.
    """
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    auth_request = google.auth.transport.requests.Request()
    credentials.refresh(auth_request)

    gcs = storage.Client(credentials=credentials)
    bucket_name = settings.gcs_bucket_name

    try:
        bucket = gcs.get_bucket(bucket_name)
    except gcp_exceptions.NotFound:
        # Create bucket - UBLA is on by default; we do NOT disable it
        bucket = gcs.create_bucket(bucket_name, location=settings.gcs_region)
        logger.info("GCS bucket created", extra={"bucket": bucket_name})

    blob_name = f"reels/{file_path.stem}_{int(time.time())}{file_path.suffix}"
    blob = bucket.blob(blob_name)
    blob.upload_from_filename(str(file_path), content_type="video/mp4")
    logger.info("File uploaded to GCS", extra={"blob": blob_name})

    expiration = datetime.timedelta(minutes=SIGNED_URL_TTL_MINUTES)

    # Service account key credentials (signer available) - sign directly
    if hasattr(credentials, "_signer") or hasattr(credentials, "signer"):
        signed_url = blob.generate_signed_url(
            expiration=expiration,
            method="GET",
            version="v4",
        )
    else:
        # Compute Engine / Workload Identity: use IAM SignBlob via access token
        try:
            sa_email = _get_sa_email_from_metadata()
        except Exception:
            # Fallback: google.auth may expose it directly on some credential types
            sa_email = getattr(credentials, "service_account_email", None)
            if not sa_email:
                raise RuntimeError(
                    "Cannot determine service account email for signing. "
                    "Set GOOGLE_APPLICATION_CREDENTIALS or use Workload Identity."
                )

        signed_url = blob.generate_signed_url(
            expiration=expiration,
            method="GET",
            version="v4",
            service_account_email=sa_email,
            access_token=credentials.token,
        )

    logger.info(
        "V4 Signed URL generated",
        extra={"ttl_min": SIGNED_URL_TTL_MINUTES, "blob": blob_name},
    )
    return signed_url


# ---------------------------------------------------------------------------
# Meta Graph API helpers (3-step Reels publish flow)
# ---------------------------------------------------------------------------

def _create_reels_container(video_url: str, caption: str) -> str:
    """
    Step 1/3: POST /{ig-user-id}/media
    Creates an upload container; returns the container ID.
    """
    endpoint = f"{META_BASE}/{settings.instagram_account_id}/media"
    payload = {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
        "share_to_feed": "true",
        "access_token": settings.instagram_access_token,
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(endpoint, data=payload)
        resp.raise_for_status()

    container_id = resp.json()["id"]
    logger.info("Reels container created", extra={"container_id": container_id})
    return container_id


def _poll_container_status(
    container_id: str,
    max_wait_s: int = 360,
    poll_interval_s: int = 10,
) -> None:
    """
    Step 2/3: GET /{container-id}?fields=status_code
    Polls until status_code == FINISHED. Raises on ERROR / EXPIRED / timeout.
    max_wait_s=360 requires Cloud Run --timeout >= 900 to avoid HTTP 504.
    """
    endpoint = f"{META_BASE}/{container_id}"
    params = {
        "fields": "status_code,status",
        "access_token": settings.instagram_access_token,
    }
    elapsed = 0
    with httpx.Client(timeout=30.0) as client:
        while elapsed < max_wait_s:
            resp = client.get(endpoint, params=params)
            resp.raise_for_status()
            data = resp.json()
            status_code = data.get("status_code", "UNKNOWN")

            logger.info(
                "Container status poll",
                extra={
                    "container_id": container_id,
                    "status_code": status_code,
                    "elapsed_s": elapsed,
                },
            )

            if status_code == "FINISHED":
                return
            if status_code in ("ERROR", "EXPIRED"):
                raise RuntimeError(
                    f"Instagram container failed. status_code={status_code} | "
                    f"details={data.get('status')}"
                )

            time.sleep(poll_interval_s)
            elapsed += poll_interval_s

    raise TimeoutError(
        f"Container {container_id} did not reach FINISHED within {max_wait_s}s."
    )


def _publish_reel(container_id: str) -> str:
    """
    Step 3/3: POST /{ig-user-id}/media_publish
    Goes live and returns the published post ID.
    """
    endpoint = f"{META_BASE}/{settings.instagram_account_id}/media_publish"
    payload = {
        "creation_id": container_id,
        "access_token": settings.instagram_access_token,
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(endpoint, data=payload)
        resp.raise_for_status()

    post_id = resp.json()["id"]
    logger.info("Reel published!", extra={"post_id": post_id})
    return post_id


# ---------------------------------------------------------------------------
# Agent entry-point
# ---------------------------------------------------------------------------

def run(state: JobState) -> JobState:
    """Run Agent 4: generate caption, upload to GCS, publish to Instagram."""
    logger.info("Agent 4 starting", extra={"job_id": state.job_id})

    # Caption generation is a legitimate LLM task - Gemini drafts a polished
    # Instagram caption with hashtags from the script context.
    genai.configure(api_key=settings.google_api_key)
    model = genai.GenerativeModel("gemini-2.5-flash")

    caption_resp = model.generate_content(
        f"""Write an Instagram Reel caption for this educational short video.

Title    : {state.script.title}
Hook     : {state.script.hook}
Narration: {state.script.narration[:300]}...

Requirements:
- Maximum 2200 characters total
- Do NOT start the first line with an emoji (harms algorithmic reach)
- Second line onwards: conversational, warm, curiosity-driven tone
- Include a clear call-to-action (e.g. "Follow for more", "Save this for later")
- End with 6-8 tightly relevant hashtags on their own line
- Return ONLY the caption text, no preamble or explanation"""
    )
    caption = caption_resp.text.strip()
    logger.info("Caption generated", extra={"length": len(caption)})

    # Upload reel to GCS, get a 30-minute V4 Signed URL (UBLA-compatible)
    signed_url = _upload_to_gcs_signed(state.final_reel_path)
    state.gcs_public_url = signed_url

    # Instagram 3-step Reels publish flow
    container_id = _create_reels_container(signed_url, caption)
    _poll_container_status(container_id)
    post_id = _publish_reel(container_id)

    state.instagram_post_id = post_id
    logger.info("Agent 4 complete", extra={"job_id": state.job_id, "post_id": post_id})
    return state
