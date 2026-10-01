"""
core/models.py
Shared Pydantic v2 data models that flow through the multi-agent pipeline.
"""
from __future__ import annotations
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field


class ScriptPayload(BaseModel):
    """
    JSON contract produced by Agent 1 and consumed by all downstream agents.
    Persisted to output/{job_id}/script.json.
    """
    title: str = Field(..., description="Short video title, <=100 chars")
    hook: str = Field(..., description="Opening attention-grabber line, <=150 chars")
    narration: str = Field(..., description="Full 60-second narration text, ~130-150 words")
    visual_prompt: str = Field(
        ..., description="3-5 word Pexels search keyword for background footage"
    )
    duration_hint: int = Field(default=60, description="Target duration in seconds")


class JobState(BaseModel):
    """
    Mutable state object passed sequentially through each agent.
    Fields are populated progressively as the pipeline executes.
    """
    job_id: str
    topic: str

    # Populated by Agent 1
    script: Optional[ScriptPayload] = None

    # Populated by Agent 2
    audio_path: Optional[Path] = None
    background_path: Optional[Path] = None

    # Populated by Agent 3
    final_reel_path: Optional[Path] = None

    # Populated by Agent 4
    gcs_public_url: Optional[str] = None
    instagram_post_id: Optional[str] = None

    model_config = {"arbitrary_types_allowed": True}
