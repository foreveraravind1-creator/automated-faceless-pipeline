"""
webhook/server.py
FastAPI application.

Endpoints:
    GET  /health        Liveness probe.
    POST /run           Generate one unapproved script and return.
    POST /run-async     Same script generation. No background pipeline.

Neither endpoint renders or publishes.
"""
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from core.config import settings
from core.logger import get_logger
from main import run_script

logger = get_logger("webhook")

app = FastAPI(
    title="Faceless Reels Pipeline",
    description="Script stage: one Gemini call, unapproved script.json, no publish.",
    version="1.0.0",
)


class RunRequest(BaseModel):
    topic: str = Field(..., min_length=1)
    niche: str = Field(..., min_length=1, description="Path to a niche JSON profile")
    job_id: Optional[str] = None


class RunResponse(BaseModel):
    job_id: str
    status: str
    approved: bool
    script_path: str
    message: str


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "Unhandled exception",
        extra={"path": str(request.url), "error": str(exc)},
        exc_info=True,
    )
    return JSONResponse(status_code=500, content={"detail": str(exc)})


@app.get("/health", tags=["ops"])
def health():
    """Liveness / readiness probe."""
    return {"status": "ok"}


@app.post("/run", response_model=RunResponse, tags=["pipeline"])
def run_sync(req: RunRequest):
    """Generate a script and stop. Does not render or publish."""
    return _generate_script(req)


@app.post("/run-async", response_model=RunResponse, tags=["pipeline"])
def run_async_endpoint(req: RunRequest):
    """
    Generate a script and return it.

    Background full-pipeline execution has been removed. This route does
    not queue render or publish work.
    """
    return _generate_script(req)


def _generate_script(req: RunRequest) -> RunResponse:
    logger.info("Script run requested", extra={"topic": req.topic, "niche": req.niche})
    try:
        state = run_script(topic=req.topic, niche_path=req.niche, job_id=req.job_id)
    except ValidationError:
        # Do not echo the parsed file; a bad profile can contain unrelated JSON.
        raise HTTPException(status_code=400, detail="niche profile failed validation") from None
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    script_path = settings.output_dir / state.job_id / "script.json"
    return RunResponse(
        job_id=state.job_id,
        status="script_ready",
        approved=False,
        script_path=str(script_path),
        message="Script written with approved=false. Nothing was rendered or published.",
    )
