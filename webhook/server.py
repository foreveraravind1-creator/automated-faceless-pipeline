"""
webhook/server.py
FastAPI application - the Cloud Run entry-point.

Endpoints:
    GET  /health        Liveness probe (no auth required).
    POST /run           Synchronous pipeline run (blocks until complete).
    POST /run-async     Async pipeline dispatch (returns immediately).
"""
import uuid
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.logger import get_logger
from main import run_pipeline

logger = get_logger("webhook")

app = FastAPI(
    title="Faceless Reels Pipeline",
    description="Multi-agent pipeline for automated Instagram Reels production.",
    version="1.0.0",
)


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class RunRequest(BaseModel):
    topic: str
    job_id: Optional[str] = None


class RunResponse(BaseModel):
    job_id: str
    status: str                         # "completed" | "queued" | "error"
    instagram_post_id: Optional[str] = None
    gcs_url: Optional[str] = None
    message: str


# ---------------------------------------------------------------------------
# Exception handler
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled exception", extra={"path": str(request.url), "error": str(exc)}, exc_info=True)
    return JSONResponse(status_code=500, content={"detail": str(exc)})


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health", tags=["ops"])
def health():
    """Liveness / readiness probe for Cloud Run."""
    return {"status": "ok"}


@app.post("/run", response_model=RunResponse, tags=["pipeline"])
def run_sync(req: RunRequest):
    """
    Synchronously execute the full four-agent pipeline.
    Best for testing and low-frequency cron jobs.
    Note: Cloud Run has a max request timeout of 3600s.
    """
    job_id = req.job_id or str(uuid.uuid4())[:8]
    logger.info("Sync run requested", extra={"job_id": job_id, "topic": req.topic})

    try:
        state = run_pipeline(topic=req.topic, job_id=job_id)
    except Exception as exc:
        logger.error("Sync run failed", extra={"job_id": job_id, "error": str(exc)}, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    return RunResponse(
        job_id=job_id,
        status="completed",
        instagram_post_id=state.instagram_post_id,
        gcs_url=state.gcs_public_url,
        message="Pipeline completed successfully.",
    )


def _run_pipeline_background(topic: str, job_id: str) -> None:
    """Background task wrapper with error capture."""
    try:
        run_pipeline(topic=topic, job_id=job_id)
    except Exception as exc:
        logger.error(
            "Background pipeline failed",
            extra={"job_id": job_id, "error": str(exc)},
            exc_info=True,
        )


@app.post("/run-async", response_model=RunResponse, tags=["pipeline"])
def run_async_endpoint(req: RunRequest, background_tasks: BackgroundTasks):
    """
    Dispatch the pipeline as a FastAPI BackgroundTask.
    Returns a 202-style response immediately with the job_id.
    Use the job_id to correlate logs in Cloud Logging.
    """
    job_id = req.job_id or str(uuid.uuid4())[:8]
    logger.info("Async run queued", extra={"job_id": job_id, "topic": req.topic})
    background_tasks.add_task(_run_pipeline_background, req.topic, job_id)
    return RunResponse(
        job_id=job_id,
        status="queued",
        message=f"Pipeline queued. Track progress via Cloud Logging with job_id={job_id}.",
    )
