"""
core/models.py
Shared Pydantic v2 data models for the script stage and later pipeline stages.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# job_id is used as a single path segment under output/.
JOB_ID_PATTERN = re.compile(r"^[a-z0-9-]{1,32}$")
_HASHTAG_PATTERN = re.compile(r"^#[A-Za-z0-9_]+$")


def validate_job_id(job_id: str) -> str:
    """Reject anything that is not a single safe path segment."""
    if not isinstance(job_id, str) or JOB_ID_PATTERN.fullmatch(job_id) is None:
        raise ValueError("job_id must match ^[a-z0-9-]{1,32}$")
    return job_id


def new_job_id() -> str:
    """8-char hex id; always matches JOB_ID_PATTERN."""
    return uuid.uuid4().hex[:8]


class NicheStyle(BaseModel):
    """Do / don't rules shipped inside a niche profile."""

    model_config = ConfigDict(extra="forbid")

    do: list[str] = Field(default_factory=list)
    dont: list[str] = Field(default_factory=list)


class NicheProfile(BaseModel):
    """
    Niche JSON profile passed in with the topic.

    This is configuration, not code and not a series. The script prompt
    receives the whole profile so later stages do not need another model call
    to recover tone, length, or hashtags.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=80)
    audience: str = Field(..., min_length=1)
    tone: str = Field(..., min_length=1)
    language: str = Field(default="en", min_length=2, max_length=16)
    duration_seconds: int = Field(..., ge=15, le=90)
    style: NicheStyle = Field(default_factory=NicheStyle)
    hashtag_pool: list[str] = Field(..., min_length=1)
    # Empty means "use KOKORO_VOICE / KOKORO_LANG" when the script is written.
    voice: str = ""
    kokoro_lang: str = ""

    @field_validator("language")
    @classmethod
    def _language(cls, value: str) -> str:
        cleaned = value.strip()
        if re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*", cleaned) is None:
            raise ValueError("language must look like 'en' or 'pt-BR'")
        return cleaned

    @field_validator("hashtag_pool")
    @classmethod
    def _hashtag_pool(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for tag in value:
            normalized = _normalize_hashtag(tag)
            if normalized not in cleaned:
                cleaned.append(normalized)
        if not cleaned:
            raise ValueError("hashtag_pool must contain at least one hashtag")
        return cleaned

    @field_validator("voice")
    @classmethod
    def _voice(cls, value: str) -> str:
        cleaned = value.strip()
        if cleaned and re.fullmatch(r"[A-Za-z0-9_]+", cleaned) is None:
            raise ValueError("voice must look like af_heart")
        return cleaned

    @field_validator("kokoro_lang")
    @classmethod
    def _kokoro_lang(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned and re.fullmatch(r"[a-z0-9-]{1,8}", cleaned) is None:
            raise ValueError("kokoro_lang must be a Kokoro code such as 'a'")
        return cleaned


class ScriptDraft(BaseModel):
    """
    JSON object returned by the single Gemini call.

    `approved` is intentionally absent: the model must not be able to approve
    its own script. The human reviewer is the fact-check.
    """

    model_config = ConfigDict(extra="ignore")

    title: str = Field(..., min_length=1, max_length=100)
    hook: str = Field(..., min_length=1, max_length=300, description="Spoken opening line")
    on_screen_hook: str = Field(
        ...,
        min_length=1,
        max_length=80,
        description="Short on-screen text; not a copy of the spoken hook",
    )
    narration: str = Field(..., min_length=1, description="Full spoken narration")
    pexels_query: str = Field(
        ...,
        min_length=1,
        max_length=80,
        description="2-5 word stock-footage search query",
    )
    caption: str = Field(..., min_length=1, max_length=2200)
    hashtags: list[str] = Field(..., min_length=1, max_length=12)
    claims_to_verify: list[str] = Field(
        ...,
        min_length=1,
        max_length=12,
        description="Factual claims a human must check before approval",
    )

    @field_validator("title", "hook", "on_screen_hook", "narration", "caption")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("text fields must not be blank")
        return cleaned

    @field_validator("pexels_query")
    @classmethod
    def _pexels_query(cls, value: str) -> str:
        cleaned = " ".join(value.replace('"', "").replace("'", "").split())
        if not cleaned:
            raise ValueError("pexels_query is required")
        return cleaned

    @field_validator("claims_to_verify")
    @classmethod
    def _claims(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        if not cleaned:
            raise ValueError("claims_to_verify must list at least one claim")
        return cleaned


class ScriptPayload(ScriptDraft):
    """
    script.json written for the human reviewer.

    Model fields come from ScriptDraft. topic, niche, language, and
    duration_seconds are copied from the request so later stages can run
    from this file alone. The writer stamps approved false. A reviewer
    can set it true in script.json and load that file.
    """

    approved: bool = False
    topic: str = Field(..., min_length=1)
    niche: str = Field(..., min_length=1)
    language: str = Field(..., min_length=2)
    duration_seconds: int = Field(..., ge=15, le=90)
    voice: str = "af_heart"
    kokoro_lang: str = "a"

    @field_validator("voice")
    @classmethod
    def _voice(cls, value: str) -> str:
        cleaned = value.strip()
        if cleaned and re.fullmatch(r"[A-Za-z0-9_]+", cleaned) is None:
            raise ValueError("voice must look like af_heart")
        return cleaned or "af_heart"

    @field_validator("kokoro_lang")
    @classmethod
    def _kokoro_lang(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned and re.fullmatch(r"[a-z0-9-]{1,8}", cleaned) is None:
            raise ValueError("kokoro_lang must be a Kokoro code such as 'a'")
        return cleaned or "a"

    @property
    def visual_prompt(self) -> str:
        """Alias kept so untouched media code can still read a search query."""
        return self.pexels_query


class CaptionCue(BaseModel):
    """One burned-in caption span, timed from the speech track."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1)
    start: float = Field(..., ge=0)
    end: float = Field(..., gt=0)

    @model_validator(mode="after")
    def _span(self) -> "CaptionCue":
        self.text = self.text.strip()
        if not self.text:
            raise ValueError("caption cue text is empty")
        if self.end <= self.start:
            raise ValueError("caption cue end must be after start")
        return self


class JobState(BaseModel):
    """
    Mutable state object passed through each stage.
    Pass 1 fills topic, niche, and script, then stops.
    """

    job_id: str
    topic: str
    niche: Optional[NicheProfile] = None

    # Populated by the script stage
    script: Optional[ScriptPayload] = None

    # Populated by --dry-run (voice, stock footage, ffmpeg). Not by the script command.
    audio_path: Optional[Path] = None
    audio_duration: Optional[float] = None
    background_path: Optional[Path] = None
    caption_cues: list[CaptionCue] = Field(default_factory=list)
    final_reel_path: Optional[Path] = None
    gcs_public_url: Optional[str] = None
    instagram_post_id: Optional[str] = None

    model_config = ConfigDict(arbitrary_types_allowed=True)


def _normalize_hashtag(tag: str) -> str:
    text = tag.strip()
    if not text.startswith("#"):
        text = "#" + text.lstrip("#")
    if _HASHTAG_PATTERN.fullmatch(text) is None:
        raise ValueError(
            f"hashtags must look like #science (letters, digits, underscore); got {tag!r}"
        )
    return text
